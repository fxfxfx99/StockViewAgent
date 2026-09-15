"""
本地上传：上市公司基本信息、财务信息、管理层讨论与分析、知识库。
目录：项目根 issuer_uploads/（与 kline_learning 并列）
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.services.issuer_document_extract import extract_text_for_issuer
from app.services.kline_learning_service import _fb_sum, _safe_name, llm_short_summary

INDEX_NAME = "materials.json"
FILES_SUB = "files"
MAX_UPLOAD_BYTES = 500 * 1024 * 1024
ALLOWED_EXT = {".txt", ".md", ".markdown", ".pdf", ".csv", ".xlsx", ".xlsm"}

CATEGORIES: tuple[str, ...] = ("basic_info", "financial", "mda", "knowledge_base")
CATEGORY_LABELS: dict[str, str] = {
    "basic_info": "上市公司基本信息",
    "financial": "财务指标",
    "mda": "管理层经营讨论与分析",
    "knowledge_base": "知识库",
}

_SYMBOL_OK = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,18}\.[A-Z]{1,4}$")


def _root() -> Path:
    base = settings.issuer_uploads_dir
    (base / FILES_SUB).mkdir(parents=True, exist_ok=True)
    return base


def _ensure_index() -> dict[str, Any]:
    p = _root() / INDEX_NAME
    if not p.exists():
        d = {"items": []}
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        return d
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"items": []}


def _save_index(data: dict[str, Any]) -> None:
    (_root() / INDEX_NAME).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def categories_meta() -> list[dict[str, str]]:
    return [{"id": k, "label": CATEGORY_LABELS[k]} for k in CATEGORIES]


def extract_text_issuer(path: Path) -> str:
    """抽取正文供摘要、全文拼接与 RAG 索引。"""
    return extract_text_for_issuer(path)


def _normalize_symbol(symbol: str) -> str:
    return (symbol or "").strip().upper()


def validate_symbol(symbol: str) -> str:
    s = _normalize_symbol(symbol)
    if not s or not _SYMBOL_OK.match(s):
        raise ValueError("股票代码格式无效（需如 600519.SS）")
    return s


def validate_category(category: str) -> str:
    c = (category or "").strip().lower()
    if c not in CATEGORY_LABELS:
        raise ValueError(f"分类无效，可选：{', '.join(CATEGORIES)}")
    return c


def list_documents(symbol: str | None = None, category: str | None = None) -> list[dict[str, Any]]:
    items = list(_ensure_index().get("items") or [])
    if symbol:
        sym = _normalize_symbol(symbol)
        items = [x for x in items if _normalize_symbol(str(x.get("symbol") or "")) == sym]
    if category:
        cat = category.strip().lower()
        items = [x for x in items if str(x.get("category") or "") == cat]
    items.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    out: list[dict[str, Any]] = []
    for x in items:
        row = dict(x)
        cat = str(row.get("category") or "")
        if cat in CATEGORY_LABELS:
            row["category_label"] = CATEGORY_LABELS[cat]
        out.append(row)
    return out


def add_document(
    data: bytes,
    original_name: str,
    symbol: str,
    category: str,
    summary_override: str | None,
) -> dict[str, Any]:
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"文件过大（单文件上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB）")
    sym = validate_symbol(symbol)
    cat = validate_category(category)
    on = _safe_name(original_name)
    ext = Path(on).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ValueError("仅支持 txt、md、markdown、pdf、csv、xlsx、xlsm")
    uid = str(uuid.uuid4())
    sn = f"{uid}_{on}"
    path = _root() / FILES_SUB / sn
    path.write_bytes(data)
    txt = extract_text_issuer(path)
    sm = (summary_override or "").strip()
    if sm and len(sm) > 80:
        sm = sm[:50] + "…"
    if not sm:
        sm = llm_short_summary(txt) or _fb_sum(txt)
    item: dict[str, Any] = {
        "id": uid,
        "symbol": sym,
        "category": cat,
        "category_label": CATEGORY_LABELS[cat],
        "original_name": on,
        "stored_name": sn,
        "summary": sm,
        "size": len(data),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "rag_indexed": False,
        "rag_chunks": 0,
        "rag_lexical_only": False,
        "rag_embedding_error": None,
    }
    d = _ensure_index()
    d["items"] = [item] + [x for x in d.get("items") or []]
    _save_index(d)
    try:
        from app.services import issuer_rag_service as irag

        meta = irag.index_document(uid, sym, path, cat)
        n = int(meta.get("chunks") or 0)
        item["rag_indexed"] = n > 0
        item["rag_chunks"] = n
        item["rag_lexical_only"] = bool(meta.get("lexical_only"))
        item["rag_embedding_error"] = meta.get("embedding_error")
    except Exception:
        item["rag_indexed"] = False
        item["rag_chunks"] = 0
        item["rag_lexical_only"] = False
        item["rag_embedding_error"] = None
    for i, x in enumerate(d.get("items") or []):
        if str(x.get("id")) == uid:
            d["items"][i] = {
                **x,
                "rag_indexed": item["rag_indexed"],
                "rag_chunks": item["rag_chunks"],
                "rag_lexical_only": item.get("rag_lexical_only", False),
                "rag_embedding_error": item.get("rag_embedding_error"),
            }
            break
    _save_index(d)
    if cat == "basic_info" and on.lower().endswith(".csv") and "上市公司基本信息" in on:
        from app.services import issuer_basic_info_universe as ibu

        def _rebuild_universe() -> None:
            try:
                ibu.rebuild_universe_from_upload_blocking()
            except Exception:
                pass

        threading.Thread(target=_rebuild_universe, daemon=True).start()
    return item


def update_document_rag_stats(doc_id: str, chunk_count: int, **extra: Any) -> None:
    """更新 materials.json 中某文档的 RAG 统计（与向量库一致）；extra 可含 rag_lexical_only、rag_embedding_error 等。"""
    did = (doc_id or "").strip()
    if not did:
        return
    d = _ensure_index()
    items = list(d.get("items") or [])
    changed = False
    for i, x in enumerate(items):
        if str(x.get("id")) == did:
            items[i] = {
                **dict(x),
                "rag_indexed": int(chunk_count) > 0,
                "rag_chunks": int(chunk_count),
                **extra,
            }
            changed = True
            break
    if changed:
        d["items"] = items
        _save_index(d)


def delete_document(doc_id: str) -> bool:
    did = (doc_id or "").strip()
    if not did:
        return False
    try:
        from app.services import issuer_rag_service as irag

        irag.delete_chunks_for_doc(did)
    except Exception:
        pass
    d = _ensure_index()
    items = d.get("items") or []
    hit = next((x for x in items if str(x.get("id")) == did), None)
    if not hit:
        return False
    p = _root() / FILES_SUB / str(hit.get("stored_name") or "")
    if p.is_file():
        try:
            p.unlink()
        except OSError:
            pass
    d["items"] = [x for x in items if str(x.get("id")) != did]
    _save_index(d)
    return True


def build_context_for_symbol(symbol: str, max_total: int = 24_000) -> str:
    """按标的聚合四类文本，供后续 RAG/LLM 使用（预留）。"""
    sym = _normalize_symbol(symbol)
    parts, n = [], 0
    for it in _ensure_index().get("items") or []:
        if _normalize_symbol(str(it.get("symbol") or "")) != sym:
            continue
        p = _root() / FILES_SUB / str(it.get("stored_name") or "")
        if not p.is_file():
            continue
        t = extract_text_issuer(p)
        if not t:
            continue
        label = CATEGORY_LABELS.get(str(it.get("category") or ""), it.get("category") or "")
        head = f"【{label}】{it.get('original_name')}\n"
        chunk = head + t
        if n + len(chunk) > max_total:
            chunk = chunk[: max(0, max_total - n)]
        if chunk.strip():
            parts.append(chunk)
        n += len(chunk)
        if n >= max_total:
            break
    return "\n\n---\n\n".join(parts) if parts else ""
