"""Stop-and-move regressions: persisted references survive a new data root."""
import csv
import json
from datetime import datetime
from pathlib import Path

import pytest

from app.config import settings


@pytest.fixture
def movable_root(tmp_path, monkeypatch):
    active = [tmp_path / "original-data"]
    active[0].mkdir()
    monkeypatch.setattr(type(settings), "data_dir", property(lambda self: active[0]))
    # Exercise the consumer contract independently of Settings' migration policy.
    monkeypatch.setattr(type(settings), "issuer_uploads_dir", property(lambda self: active[0] / "issuer_uploads"))
    monkeypatch.setattr(type(settings), "kline_learning_dir", property(lambda self: active[0] / "kline_learning"))

    def move():
        previous = active[0]
        destination = tmp_path / "renamed-portable-library"
        previous.rename(destination)
        active[0] = destination
        return previous, destination

    return active, move


def test_research_manifest_and_offline_history_survive_directory_move(movable_root):
    import asyncio
    from app.services.research_data import ResearchData
    active, move = movable_root
    data = ResearchData()
    candles = [{"t": int(datetime.fromisoformat(day + "T00:00:00+08:00").timestamp()),
                "o": 10, "h": 12, "l": 9, "c": 11, "v": 10000}
               for day in ("2025-01-06", "2025-01-07")]
    bundle = {"candles": candles, "kline_source": "fixture", "price_basis": "raw", "volume_unit": "shares"}
    snapshot = data.import_snapshot(7, {"600000.SS": bundle}, metadata={})
    expected = data.read_snapshot(7, snapshot["id"])
    data.store.set_state(7, "migration-fixture", {"snapshot_id": snapshot["id"]})
    job = data.store.queue_job(7, "evaluate", {"snapshot_id": snapshot["id"]}, "fixture-before-move")
    previous, destination = move()

    reopened = ResearchData()
    assert reopened.store.root == destination
    assert reopened.read_snapshot(7, snapshot["id"]) == expected
    assert reopened.get_snapshot(7, snapshot["id"])["content_hash"] == snapshot["content_hash"]
    assert reopened.store.get_state(7, "migration-fixture") == {"snapshot_id": snapshot["id"]}
    assert reopened.store.get_job(7, job["id"])["payload"] == {"snapshot_id": snapshot["id"]}
    assert not previous.exists()
    for manifest in (destination / "research" / "batches").glob("*/manifest.json"):
        payload = json.loads(manifest.read_text())
        assert set(payload["files"]) == {"raw.json", "bars.parquet"}
        assert str(previous) not in manifest.read_text()
    offline = asyncio.run(reopened.fetch_history(7, "600000.SS", offline=True))
    assert offline["row_count"] == 2


def test_uploaded_documents_and_rag_reindex_follow_moved_root(movable_root, monkeypatch):
    from app.services import issuer_rag_service as rag
    from app.services import issuer_uploads_service as uploads
    from app.services import kline_learning_service as learning
    monkeypatch.setattr(rag, "embed_texts_all", lambda texts: (None, "fixture offline"))
    monkeypatch.setattr(rag, "_embed_batch", lambda texts: (None, "fixture offline"))
    body = ("营业收入增长来自公司新增产能，年度经营资料保存在本地并可用于全文检索。" * 10).encode()
    document = uploads.add_document(body, "公司资料.txt", "600000.SS", "knowledge_base", "公司资料")
    material = learning.add_material(body, "策略笔记.txt", "策略笔记")
    assert document["rag_chunks"] > 0 and document["rag_lexical_only"]
    previous, destination = movable_root[1]()

    assert uploads.list_documents("600000.SS")[0]["id"] == document["id"]
    assert "营业收入增长" in uploads.build_context_for_symbol("600000.SS")
    assert learning.list_materials()[0]["id"] == material["id"]
    assert "营业收入增长" in learning.build_learning_context()
    assert rag.search_chunks("600000.SS", "营业收入")[0]["doc_id"] == document["id"]
    rebuilt = rag.reindex_document(document["id"])
    assert rebuilt["ok"] and rebuilt["chunks"] > 0
    assert not previous.exists()
    for index in (destination / "issuer_uploads" / "materials.json", destination / "kline_learning" / "materials.json"):
        assert str(previous) not in index.read_text()
        assert all(Path(item["stored_name"]).name == item["stored_name"] for item in json.loads(index.read_text())["items"])


DATASETS = [
    ("basic_info", "import_basic_csv", {"Symbol": "000001", "EndDate": "2025-12-31", "FullName": "公司旧名"}, "FullName"),
    ("financial", "import_financial_csv", {"FI_T1.Stkcd": "000001", "FI_T1.Accper": "2025-12-31", "FI_T1.Typrep": "A", "FI_T1.F010101A": "1.2"}, "FI_T1.F010101A"),
    ("mda", "import_mda_csv", {"Symbol": "000001", "Enddate": "2025-12-31", "ManaDiscAnal": "经营旧文本"}, "ManaDiscAnal"),
]


def write_csv(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)


@pytest.mark.parametrize("dataset,importer,row,field", DATASETS)
def test_fundamentals_relative_sources_and_reimport_after_move(movable_root, dataset, importer, row, field):
    from app.storage import company_fundamentals_store as fundamentals
    active, move = movable_root
    relative = Path("imports") / "company_fundamentals" / f"{dataset}.csv"
    path = active[0] / relative
    write_csv(path, row)
    ingest = getattr(fundamentals, importer)
    assert ingest(path) == 1
    revisions = fundamentals.list_revisions("000001.SZ", dataset=dataset)
    assert revisions[0]["source_path"] == relative.as_posix()
    original_revision = revisions[0]
    _, destination = move()
    assert ingest(destination / relative) == 0
    assert fundamentals.list_revisions("000001.SZ", dataset=dataset) == [original_revision]
    assert fundamentals.status()["source_files"][0]["path"] == relative.as_posix()
    write_csv(destination / relative, {**row, field: "2.4" if dataset == "financial" else "已修订内容"})
    assert ingest(destination / relative) == 1
    assert len(fundamentals.list_revisions("000001.SZ", dataset=dataset)) == 2


def test_legacy_external_source_keeps_audit_and_dedups_managed_copy(movable_root, tmp_path):
    from app.storage import company_fundamentals_store as fundamentals
    external = tmp_path / "external" / "basic.csv"
    write_csv(external, DATASETS[0][2])
    assert fundamentals.import_basic_csv(external) == 1
    revision = fundamentals.list_revisions("000001.SZ")[0]
    assert revision["source_path"] == str(external.resolve())
    previous, destination = movable_root[1]()
    managed = destination / "imports" / "company_fundamentals" / "basic.csv"
    managed.parent.mkdir(parents=True)
    managed.write_bytes(external.read_bytes())
    external.unlink()
    assert fundamentals.import_basic_csv(managed) == 0
    assert fundamentals.list_revisions("000001.SZ") == [revision]
    assert "imports/company_fundamentals/basic.csv" in {row["path"] for row in fundamentals.status()["source_files"]}
    assert fundamentals.latest_for_symbol("000001.SZ")["basic_info"]["full_name"] == "公司旧名"
    assert not previous.exists()


def test_default_fundamental_import_uses_managed_import_directory(movable_root):
    from app.storage import company_fundamentals_store as fundamentals
    root = movable_root[0][0] / "imports" / "company_fundamentals"
    names = {"basic_info": "上市公司基本信息 2025.csv", "financial": "财务指标 2025.csv", "mda": "管理层经营讨论与分析 2025.csv"}
    for dataset, _, row, _ in DATASETS:
        write_csv(root / names[dataset], row)
    result = fundamentals.import_local_2025_files()
    assert result["basic_rows"] == result["financial_rows"] == result["mda_rows"] == 1
    assert fundamentals.status()["recent_runs"][0]["source_dir"] == "imports/company_fundamentals"


def test_bundled_strategy_notes_do_not_depend_on_data_parent(movable_root):
    from app.services import strategy_knowledge_service as knowledge
    from app.storage import strategy_knowledge_store
    result = knowledge.ingest_local_transaction_agent_kb()
    assert result["items_upserted"] > 0
    sources = strategy_knowledge_store.list_sources()
    local = next(row for row in sources if row["id"] == "transaction_agent_local_notes")
    assert local["url"] == "resource://stockviewagent/transaction_agent/notes/strategy_kb_notes.md"
    previous, _ = movable_root[1]()
    again = knowledge.ingest_local_transaction_agent_kb()
    assert again["items_upserted"] == result["items_upserted"]
    assert not previous.exists()
