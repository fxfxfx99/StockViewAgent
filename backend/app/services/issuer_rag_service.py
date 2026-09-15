"""
本地上传资料的 RAG：分块 → OpenAI 兼容 embeddings → SQLite；检索与可选 LLM 回答。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.services.issuer_document_extract import extract_text_for_issuer
from app.storage.llm_runtime import load_runtime

CHUNK_SIZE = 800
CHUNK_OVERLAP = 120
MAX_INDEX_CHARS = 450_000
MAX_CHUNKS_PER_DOC = 256
MIN_CHUNK_CHARS = 24
EMBED_BATCH = 24
DEFAULT_EMBED_MODEL = "text-embedding-3-small"


def _rag_db_path() -> Path:
    p = settings.issuer_uploads_dir / "rag"
    p.mkdir(parents=True, exist_ok=True)
    return p / "chunks.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_rag_db_path()))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chunks (
          id TEXT PRIMARY KEY,
          doc_id TEXT NOT NULL,
          symbol TEXT NOT NULL,
          category TEXT,
          chunk_idx INTEGER NOT NULL,
          text TEXT NOT NULL,
          embedding TEXT NOT NULL,
          created_at REAL NOT NULL
        )
        """
    )
    cur = conn.execute("PRAGMA table_info(chunks)")
    col_names = {row[1] for row in cur.fetchall()}
    if "lexical_only" not in col_names:
        conn.execute("ALTER TABLE chunks ADD COLUMN lexical_only INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_symbol ON chunks(symbol)")
    conn.commit()
    return conn


def delete_chunks_for_doc(doc_id: str) -> None:
    did = (doc_id or "").strip()
    if not did:
        return
    conn = _connect()
    conn.execute("DELETE FROM chunks WHERE doc_id = ?", (did,))
    conn.commit()
    conn.close()


def _chunk_text(text: str) -> list[str]:
    t = re.sub(r"\s+", " ", text).strip()
    if not t:
        return []
    t = t[:MAX_INDEX_CHARS]
    chunks: list[str] = []
    i = 0
    while i < len(t) and len(chunks) < MAX_CHUNKS_PER_DOC:
        end = min(i + CHUNK_SIZE, len(t))
        chunk = t[i:end].strip()
        if len(chunk) >= MIN_CHUNK_CHARS:
            chunks.append(chunk)
        if end >= len(t):
            break
        i = max(end - CHUNK_OVERLAP, i + 1)
    return chunks


def _embedding_model() -> str:
    rt = load_runtime()
    m = (rt.get("embedding_model") or "").strip()
    return m or DEFAULT_EMBED_MODEL


def get_embedding_model() -> str:
    return _embedding_model()


def _normalize_embeddings_api_root() -> str:
    """
    使用 kline_embedding_api_base（可单独配置）；剥掉误填的 .../chat/completions 后缀。
    """
    b = settings.kline_embedding_api_base.rstrip("/")
    for suf in ("/v1/chat/completions", "/chat/completions"):
        if b.lower().endswith(suf.lower()):
            b = b[: -len(suf)].rstrip("/")
            break
    return b


def _add_host_root_embed_candidates(root: str, add: Callable[[str], None]) -> None:
    """
    Base 带子路径时，部分网关把向量挂在主机根下：/v1/embeddings 或 /embeddings。
    """
    if "://" not in root:
        return
    try:
        p = urlparse(root)
        if not p.scheme or not p.netloc:
            return
        path = (p.path or "").rstrip("/")
        if not path:
            return
        for tail in ("v1/embeddings", "embeddings"):
            add(f"{p.scheme}://{p.netloc}/{tail}")
    except Exception:
        return


def _embedding_request_urls() -> list[str]:
    """
    Embeddings 与 Chat 为同级：{root}/embeddings。
    - 若配置里已是完整向量 URL（路径以 /embeddings 结尾），则不再追加 /embeddings（避免 .../embeddings/embeddings → 404）。
    - root 无 /v1：先试 {root}/embeddings，404 再试 {root}/v1/embeddings（官方 OpenAI）。
    - root 以 /v1 结尾：先试 {root}/embeddings；若 404 再试去掉 /v1 后的 {parent}/embeddings（部分中转向量挂在域名根下）。
    - 若 Base 带非空 path，再试 {host}/v1/embeddings 与 {host}/embeddings（向量在主机根、对话在子路径）。
    若仍 404，可在 K 线助手填写「Embedding API Base」。
    """
    root = _normalize_embeddings_api_root().rstrip("/")
    root_low = root.lower()
    if root_low.endswith("/embeddings"):
        return [root]
    out: list[str] = []
    seen: set[str] = set()

    def add(u: str) -> None:
        u = u.rstrip("/")
        if u not in seen:
            seen.add(u)
            out.append(u)

    add(f"{root}/embeddings")
    rest = root.split("://", 1)[-1] if "://" in root else root
    has_v1 = bool(re.search(r"/v1(/|$)", rest))
    if has_v1 and root.lower().endswith("/v1"):
        parent = root[:-3].rstrip("/")
        if parent:
            add(f"{parent}/embeddings")
    if not has_v1:
        add(f"{root}/v1/embeddings")
    _add_host_root_embed_candidates(root, add)
    return out


def _embed_error_user_hint(msg: str) -> str:
    if "404" in msg or "url.not_found" in msg.lower():
        return (
            msg
            + " 可在「K 线助手」填写「Embedding API Base」，指向网关在文档中提供的、与 /embeddings 同级的 API 根地址。"
        )
    return msg


def _embed_keys_try_order() -> list[str]:
    keys: list[str] = []
    for k in (settings.kline_llm_key, settings.kline_llm_key_backup):
        k = (k or "").strip()
        if k and k not in keys:
            keys.append(k)
    return keys


def _embed_batch(texts: list[str]) -> tuple[list[list[float]] | None, str | None]:
    if not texts:
        return [], None
    keys = _embed_keys_try_order()
    if not keys:
        return None, "未配置 API Key（请在控制台保存主密钥或备用密钥，或使用环境变量）"
    model = _embedding_model()
    urls = _embedding_request_urls()
    last_msg = "Embeddings 无可用 URL"

    def try_one_key(key: str) -> tuple[list[list[float]] | None, str | None]:
        try:
            with httpx.Client(timeout=120.0) as c:
                r = None
                for idx, url in enumerate(urls):
                    r = c.post(
                        url,
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={"model": model, "input": texts},
                    )
                    if r.status_code < 400:
                        break
                    try:
                        body = r.json()
                        err = body.get("error", body)
                        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    except Exception:
                        msg = (r.text or "")[:500]
                    err_s = f"Embeddings HTTP {r.status_code}: {msg}"
                    if r.status_code == 404 and idx + 1 < len(urls):
                        continue
                    return None, err_s
                else:
                    return None, _embed_error_user_hint(last_msg)
                assert r is not None
                data = r.json()
                arr = data.get("data") or []
                arr.sort(key=lambda x: int(x.get("index", 0)))
                out = [x["embedding"] for x in arr if isinstance(x.get("embedding"), list)]
                if len(out) != len(texts):
                    return None, "Embeddings 返回向量条数与请求不一致"
                return out, None
        except httpx.HTTPError as e:
            return None, f"Embeddings 网络错误: {e!s}"
        except Exception as e:  # noqa: BLE001
            return None, f"Embeddings 失败: {e!s}"

    for key in keys:
        out, err = try_one_key(key)
        if err is None and out is not None:
            return out, None
        if err:
            last_msg = err
    return None, _embed_error_user_hint(last_msg)


def embed_texts_all(texts: list[str]) -> tuple[list[list[float]] | None, str | None]:
    all_emb: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i : i + EMBED_BATCH]
        part, err = _embed_batch(batch)
        if part is None:
            return None, err
        all_emb.extend(part)
    return all_emb, None


def _lexical_score(query: str, text: str) -> float:
    """无向量时的关键词命中（中英数字片段）。"""
    q = (query or "").strip()
    if not q or not text:
        return 0.0
    terms = set(
        re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}|\d+(?:\.\d+)?%?", q.lower())
    )
    if not terms:
        terms = set(re.findall(r"[\u4e00-\u9fff]", q))
    if not terms:
        return 0.0
    tl = text.lower()
    hits = sum(1 for t in terms if t in tl)
    return hits / len(terms)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def index_document(doc_id: str, symbol: str, path: Path, category: str | None) -> dict[str, Any]:
    """
    抽取文本、分块、写入检索库。
    优先写入向量；若 Embeddings 不可用则仍写入文本块（lexical_only），保证上传文件可检索。
    返回 {"chunks": int, "lexical_only": bool, "embedding_error": str | None}
    """
    did = (doc_id or "").strip()
    sym = (symbol or "").strip().upper()
    if not did or not sym or not path.is_file():
        return {"chunks": 0, "lexical_only": False, "embedding_error": None}
    delete_chunks_for_doc(did)
    text = extract_text_for_issuer(path)
    chunks = _chunk_text(text)
    if not chunks:
        return {"chunks": 0, "lexical_only": False, "embedding_error": "正文过短或无法解析（检查 PDF/表格编码）"}
    embs, emb_err = embed_texts_all(chunks)
    lexical_only = embs is None
    if lexical_only:
        embs = [[] for _ in chunks]
    cat = (category or "").strip().lower() or None
    now = time.time()
    conn = _connect()
    for idx, (ch, emb) in enumerate(zip(chunks, embs or [])):
        cid = f"{did}_{idx}"
        payload = json.dumps(emb if emb else [])
        lo = 1 if lexical_only else 0
        conn.execute(
            """
            INSERT INTO chunks (id, doc_id, symbol, category, chunk_idx, text, embedding, created_at, lexical_only)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (cid, did, sym, cat, idx, ch, payload, now, lo),
        )
    conn.commit()
    conn.close()
    return {
        "chunks": len(chunks),
        "lexical_only": lexical_only,
        "embedding_error": emb_err if lexical_only else None,
    }


def reindex_document(doc_id: str) -> dict[str, Any]:
    """根据 materials 索引找到文件并重建 RAG。"""
    from app.services import issuer_uploads_service as ius

    d = ius._ensure_index()
    hit = next((x for x in (d.get("items") or []) if str(x.get("id")) == doc_id), None)
    if not hit:
        return {"ok": False, "detail": "未找到文档", "chunks": 0}
    sym = str(hit.get("symbol") or "").strip().upper()
    cat = str(hit.get("category") or "")
    p = ius._root() / ius.FILES_SUB / str(hit.get("stored_name") or "")
    meta = index_document(doc_id, sym, p, cat)
    n = int(meta.get("chunks") or 0)
    try:
        ius.update_document_rag_stats(
            doc_id,
            n,
            rag_lexical_only=bool(meta.get("lexical_only")),
            rag_embedding_error=meta.get("embedding_error"),
        )
    except Exception:
        pass
    return {
        "ok": True,
        "chunks": n,
        "rag_indexed": n > 0,
        "lexical_only": bool(meta.get("lexical_only")),
        "embedding_error": meta.get("embedding_error"),
    }


def reindex_all_for_symbols(symbols: list[str]) -> dict[str, Any]:
    """
    对给定标的下所有本地上传文件重新从磁盘抽取正文并重建 RAG 向量。
    用于「刷新公司信息与要闻」时同步更新表格/PDF 等资料的可检索内容。
    """
    from app.services import issuer_uploads_service as ius

    try:
        seen: set[str] = set()
        syms: list[str] = []
        for s in symbols:
            t = (s or "").strip().upper()
            if t and t not in seen:
                seen.add(t)
                syms.append(t)
        details: list[dict[str, Any]] = []
        for sym in syms:
            for it in ius.list_documents(symbol=sym):
                did = str(it.get("id") or "")
                if not did:
                    continue
                try:
                    r = reindex_document(did)
                except Exception as ex:  # noqa: BLE001
                    details.append(
                        {"doc_id": did, "symbol": sym, "ok": False, "detail": str(ex), "chunks": 0}
                    )
                    continue
                details.append({"doc_id": did, "symbol": sym, **r})
        total_chunks = sum(int(x.get("chunks") or 0) for x in details if x.get("ok"))
        ok_with_chunks = sum(1 for x in details if x.get("ok") and int(x.get("chunks") or 0) > 0)
        zero = sum(1 for x in details if x.get("ok") and int(x.get("chunks") or 0) == 0)
        lexical_docs = sum(
            1 for x in details if x.get("ok") and x.get("lexical_only") and int(x.get("chunks") or 0) > 0
        )
        emb_errs = [
            str(x.get("embedding_error"))
            for x in details
            if x.get("embedding_error") and str(x.get("embedding_error")).strip()
        ]
        emb_err_sample = emb_errs[0][:300] if emb_errs else None
        return {
            "status": "ok",
            "issuer_docs_touched": len(details),
            "issuer_rag_chunks_total": total_chunks,
            "issuer_rag_indexed_ok": ok_with_chunks,
            "issuer_rag_zero_chunks": zero,
            "issuer_rag_lexical_docs": lexical_docs,
            "embedding_error_sample": emb_err_sample,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "status": "error",
            "detail": str(e),
            "issuer_docs_touched": 0,
            "issuer_rag_chunks_total": 0,
            "issuer_rag_indexed_ok": 0,
            "issuer_rag_zero_chunks": 0,
            "issuer_rag_lexical_docs": 0,
            "embedding_error_sample": None,
        }


def search_chunks(
    symbol: str,
    query: str,
    top_k: int = 8,
    categories: list[str] | None = None,
) -> list[dict[str, Any]]:
    sym = (symbol or "").strip().upper()
    q = (query or "").strip()
    if not sym or not q:
        return []
    top_k = max(1, min(int(top_k), 24))
    q_emb, _q_err = _embed_batch([q])
    qv: list[float] | None = q_emb[0] if q_emb and len(q_emb) == 1 else None
    conn = _connect()
    cur = conn.execute(
        """
        SELECT id, doc_id, symbol, category, chunk_idx, text, embedding, COALESCE(lexical_only, 0)
        FROM chunks WHERE symbol = ?
        """,
        (sym,),
    )
    rows = cur.fetchall()
    conn.close()
    cats = None
    if categories:
        cats = {c.strip().lower() for c in categories if c and str(c).strip()}
    scored: list[tuple[float, str, tuple]] = []
    for row in rows:
        _, doc_id, _, cat, _, text, emb_s, lex_flag = row
        if cats is not None and (cat or "").lower() not in cats:
            continue
        try:
            emb = json.loads(emb_s)
        except (json.JSONDecodeError, TypeError):
            emb = []
        if not isinstance(emb, list):
            emb = []
        has_vec = bool(emb) and len(emb) > 16 and not int(lex_flag or 0)
        mode = "lexical"
        if qv and has_vec:
            s = _cosine(qv, emb)
            mode = "vector"
        else:
            s = _lexical_score(q, text)
        scored.append((s, mode, row))
    scored.sort(key=lambda x: -x[0])
    out: list[dict[str, Any]] = []
    for s, mode, row in scored:
        if len(out) >= top_k:
            break
        cid, doc_id, _, cat, cidx, text, _emb, _lf = row
        out.append(
            {
                "chunk_id": cid,
                "doc_id": doc_id,
                "category": cat,
                "chunk_idx": cidx,
                "score": round(float(s), 4),
                "match": mode,
                "text": text[:2000] + ("…" if len(text) > 2000 else ""),
            }
        )
    return out


def _doc_names_for_ids(doc_ids: list[str]) -> dict[str, str]:
    from app.services import issuer_uploads_service as ius

    names: dict[str, str] = {}
    for it in ius._ensure_index().get("items") or []:
        did = str(it.get("id") or "")
        if did in doc_ids:
            names[did] = str(it.get("original_name") or did)
    return names


def search_with_names(
    symbol: str,
    query: str,
    top_k: int = 8,
    categories: list[str] | None = None,
) -> list[dict[str, Any]]:
    chunks = search_chunks(symbol, query, top_k=top_k, categories=categories)
    ids = list({c["doc_id"] for c in chunks})
    names = _doc_names_for_ids(ids)
    for c in chunks:
        c["original_name"] = names.get(c["doc_id"], c["doc_id"])
    return chunks


def rag_answer(
    symbol: str,
    query: str,
    top_k: int = 6,
    categories: list[str] | None = None,
) -> tuple[str | None, list[dict[str, Any]], str | None]:
    """检索 + LLM 生成回答。返回 (answer, chunks_used, error)。"""
    chunks = search_with_names(symbol, query, top_k=top_k, categories=categories)
    if not chunks:
        return (
            None,
            [],
            "无检索结果（请确认该标的下已有上传且刷新过；若仅有表格请检查是否解析出正文）",
        )
    ctx = "\n\n".join(
        f"[{c.get('original_name')}]（相关度 {c.get('score')}）\n{c.get('text', '')}" for c in chunks
    )
    if not settings.any_llm_key_configured:
        return None, chunks, "未配置 API Key，无法生成回答"
    sys_msg = (
        "你是证券与财务分析助手。仅根据用户提供的「检索片段」作答；若片段不足以回答，请明确说明。"
        "使用中文，条理清晰；结尾注明：以上内容基于用户上传资料检索，不构成投资建议。"
    )
    user_msg = f"问题：{query}\n\n检索片段：\n{ctx[:28000]}"
    from app.services import llm_client  # noqa: PLC0415

    text, err = llm_client.chat_completion_sync(
        [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.35,
        timeout=120.0,
    )
    if err or not text:
        return None, chunks, err or "空响应"
    return text.strip(), chunks, None
