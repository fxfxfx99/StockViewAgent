"""财报/公告类：优先本地上传 RAG 片段；无则说明缺口。"""
from __future__ import annotations

from typing import Any


def analyze_filings(bundle: dict[str, Any]) -> dict[str, Any]:
    chunks = bundle.get("issuer_chunks") or []
    if not chunks:
        return {
            "narrative": "未检索到本地上传的财报/公告切片；SEC/交易所原文接口为预留能力，当前未接入实时抓取。",
            "chunks": [],
        }

    lines = [f"本地上传资料检索命中 {len(chunks)} 段（摘要如下，原文以库内文件为准）:"]
    for i, c in enumerate(chunks[:6], 1):
        name = c.get("original_name") or c.get("doc_id")
        lines.append(f"{i}. 《{name}》 chunk#{c.get('chunk_idx')}: {c.get('text', '')[:320]}…")

    return {"narrative": "\n".join(lines), "chunks": chunks}
