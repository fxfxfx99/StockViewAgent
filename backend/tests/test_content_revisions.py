"""Content history is durable without turning local imports into PIT evidence."""
import csv
import json
import sqlite3

import pytest

from app.storage import company_fundamentals_store as fundamentals
from app.storage import news_store


def news_item(content, fetched=20000):
    return {"title": "公司公布经营情况", "url": "https://example.test/company/notice",
            "summary": "来源摘要", "content": content, "source": "fixture",
            "published_ts": 10000, "fetched_at": fetched}


def test_news_revisions_keep_full_content_and_original_observation():
    first = news_item("旧内容" + "字" * 11000)
    news_store.upsert_many([first])
    aid = first["id"]
    news_store.upsert_many([news_item(first["content"], 21000)])
    assert len(news_store.list_content_revisions(aid)) == 1
    # This change is beyond the model's bounded 10k analysis excerpt.
    revised = news_item(first["content"] + "更正", 22000)
    news_store.upsert_many([revised])
    revisions = news_store.list_content_revisions(aid)
    assert len(revisions) == 2
    assert [r["first_observed_at"] for r in revisions] == [22000, 20000]
    assert revisions[0]["snapshot"]["content"].endswith("更正")
    assert revisions[1]["snapshot"]["content"] == first["content"]
    assert revisions[0]["published_ts"] == 10000
    assert not revisions[0]["historical_availability_verified"]
    assert len(news_store.list_content_revisions(aid, observed_before=21500)) == 1
    with news_store._connect() as conn:
        assert conn.execute("SELECT content FROM articles WHERE id=?", (aid,)).fetchone()[0].endswith("更正")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM article_content_revisions")


def test_news_legacy_backfill_precedes_rewrite_and_is_idempotent():
    item = news_item("旧稿")
    news_store.upsert_many([item])
    with news_store._connect() as conn:
        conn.execute("DROP TABLE article_content_revisions")
        conn.execute("DELETE FROM news_schema_meta WHERE key='content_revisions_v1'")
    news_store.init_db()
    news_store.init_db()
    initial = news_store.list_content_revisions(item["id"])
    assert len(initial) == 1
    assert initial[0]["origin"] == "legacy_backfill"
    news_store.upsert_many([news_item("更正稿", 30000)])
    assert {r["snapshot"]["content"] for r in news_store.list_content_revisions(item["id"])} == {"旧稿", "更正稿"}


def test_news_revision_and_latest_update_roll_back_together():
    item = news_item("旧稿")
    news_store.upsert_many([item])
    with news_store._connect() as conn:
        conn.execute("""CREATE TRIGGER fail_revision BEFORE INSERT ON article_content_revisions
          WHEN NEW.snapshot_json LIKE '%更正%' BEGIN SELECT RAISE(ABORT,'fixture failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
        news_store.upsert_many([news_item("更正稿", 30000)])
    with news_store._connect() as conn:
        assert conn.execute("SELECT content FROM articles WHERE id=?", (item["id"],)).fetchone()[0] == "旧稿"
    assert len(news_store.list_content_revisions(item["id"])) == 1


DATASETS = [
    ("basic_info", fundamentals.import_basic_csv, {"Symbol": "000001", "EndDate": "2024-12-31", "FullName": "旧公司名"}, "FullName"),
    ("financial", fundamentals.import_financial_csv, {"FI_T1.Stkcd": "000001", "FI_T1.Accper": "2024-12-31", "FI_T1.Typrep": "A", "FI_T1.F010101A": "1.2"}, "FI_T1.F010101A"),
    ("mda", fundamentals.import_mda_csv, {"Symbol": "000001", "Enddate": "2024-12-31", "ManaDiscAnal": "旧经营讨论"}, "ManaDiscAnal"),
]


def write_csv(path, row):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)


@pytest.mark.parametrize("dataset,importer,raw,changed_field", DATASETS)
def test_fundamental_reimport_dedup_and_correction_history(tmp_path, monkeypatch, dataset, importer, raw, changed_field):
    clock = {"now": "2025-02-01T12:00:00+00:00"}
    monkeypatch.setattr(fundamentals, "_now_iso", lambda: clock["now"])
    path = tmp_path / f"{dataset}.csv"
    write_csv(path, raw)
    assert importer(path) == 1
    assert importer(path) == 0
    assert importer(path, force=True) == 1
    initial = fundamentals.list_revisions("000001.SZ", dataset=dataset)
    assert len(initial) == 1
    assert initial[0]["published_at"] is None
    assert initial[0]["available_at"] is None
    assert initial[0]["report_date"] == "2024-12-31"
    assert initial[0]["raw"][changed_field] == raw[changed_field]

    clock["now"] = "2025-03-01T12:00:00+00:00"
    changed = {**raw, changed_field: "修订内容", "published_at": "2025-02-28T16:00:00+08:00"}
    write_csv(path, changed)
    assert importer(path) == 1
    revisions = fundamentals.list_revisions("000001", dataset=dataset)
    assert len(revisions) == 2
    assert revisions[0]["raw"][changed_field] == "修订内容"
    assert revisions[1]["raw"][changed_field] == raw[changed_field]
    assert revisions[0]["published_at"] == "2025-02-28T08:00:00+00:00"
    assert revisions[0]["available_at"] is None
    assert revisions[0]["observed_at"] == clock["now"]
    assert revisions[0]["source_path"].endswith(f"{dataset}.csv")
    assert not revisions[0]["historical_availability_verified"]
    assert len(fundamentals.list_revisions("000001", observed_before="2025-02-15T00:00:00Z")) == 1
    assert fundamentals.status()["counts"][dataset] == 1
    # Header ordering has no effect on revision identity.
    write_csv(path, dict(reversed(list(changed.items()))))
    importer(path, force=True)
    assert len(fundamentals.list_revisions("000001", dataset=dataset)) == 2


def test_fundamental_legacy_mda_keeps_full_text_and_explicit_publication(tmp_path):
    path = tmp_path / "mda.csv"
    raw = {"Symbol": "000001", "Enddate": "2024-12-31", "ManaDiscAnal": "旧长文不能丢",
           "AnnouncementDate": "2025-01-20"}
    write_csv(path, raw)
    fundamentals.import_mda_csv(path)
    with fundamentals.connect() as conn:
        conn.execute("DROP TABLE company_record_revisions")
        conn.execute("DELETE FROM fundamentals_schema_meta WHERE key='content_revisions_v1'")
    fundamentals.init_db()
    fundamentals.init_db()
    original = fundamentals.list_revisions("000001")[0]
    assert original["origin"] == "legacy_backfill"
    assert original["published_at"] == "2025-01-20"
    assert original["publication_field"] == "AnnouncementDate"
    assert original["available_at"] is None
    assert original["raw"]["ManaDiscAnal"] == "旧长文不能丢"
    write_csv(path, {**raw, "ManaDiscAnal": "新内容", "available_at": "2025-02-01T10:00:00+08:00"})
    fundamentals.import_mda_csv(path)
    revisions = fundamentals.list_revisions("000001")
    assert len(revisions) == 2
    assert revisions[0]["available_at"] == "2025-02-01T02:00:00+00:00"
    assert revisions[1]["raw"]["ManaDiscAnal"] == "旧长文不能丢"
    with fundamentals.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE company_record_revisions SET raw_json='{}'")


def test_fundamental_failed_update_rolls_back_revision(tmp_path):
    path = tmp_path / "basic.csv"
    row = {"Symbol": "000001", "EndDate": "2024-12-31", "FullName": "旧名称"}
    write_csv(path, row)
    fundamentals.import_basic_csv(path)
    with fundamentals.connect() as conn:
        conn.execute("""CREATE TRIGGER fail_latest BEFORE UPDATE ON company_basic_records
          BEGIN SELECT RAISE(ABORT,'fixture failure'); END""")
    write_csv(path, {**row, "FullName": "新名称"})
    with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
        fundamentals.import_basic_csv(path)
    assert len(fundamentals.list_revisions("000001")) == 1
    assert fundamentals.latest_for_symbol("000001")["basic_info"]["full_name"] == "旧名称"
