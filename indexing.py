"""
Search index builder.

For each document we transcribe it to text once (cached), split it into page-
aware chunks, embed the chunks, and store them in the vector store. This is
deliberately separate from the verification pass: verification does not depend
on the index, and an embedding/transcription failure leaves verification intact
and simply yields an empty (or partial) search index.
"""
from __future__ import annotations

import asyncio
import re

import config
import extraction
import vectorstore
from evaluate import discover_documents


def _parse_pages(text: str) -> list[tuple[int, str]]:
    """Split transcription on '=== PAGE n ===' markers into (page, body)."""
    if not text or not text.strip():
        return []
    parts = re.split(r"=== PAGE (\d+) ===", text)
    if len(parts) == 1:
        return [(1, text.strip())]
    pages: list[tuple[int, str]] = []
    i = 1
    while i + 1 < len(parts):
        try:
            n = int(parts[i])
        except (ValueError, TypeError):
            i += 2
            continue
        pages.append((n, (parts[i + 1] or "").strip()))
        i += 2
    return pages


def _chunk(text: str, size: int = 700, overlap: int = 100) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(n, start + size)
        if end < n:  # prefer to break on a space in the back half of the window
            sp = text.rfind(" ", start + size // 2, end)
            if sp != -1:
                end = sp
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


async def build_index(case_id: str, force: bool = False) -> dict:
    existing = vectorstore.count(case_id)
    if not force and existing > 0:
        return {"indexed": existing, "skipped": True, "errors": {}}

    docs, _ = discover_documents(case_id)
    if not docs:
        return {"indexed": 0, "skipped": False, "errors": {}}

    sem = asyncio.Semaphore(config.MAX_CONCURRENCY)
    items = list(docs.items())
    results = await asyncio.gather(
        *[extraction.transcribe_document(k, p, sem) for k, p in items]
    )

    passages: list[dict] = []
    errors: dict[str, str] = {}
    for (doc_key, _), (text, err) in zip(items, results):
        if err:
            errors[doc_key] = err
            continue
        for page, body in _parse_pages(text):
            for ch in _chunk(body):
                passages.append({"doc_key": doc_key, "page": page, "text": ch,
                                 "item_id": "", "status": ""})

    if not passages:
        return {"indexed": 0, "skipped": False, "errors": errors}

    vecs = await asyncio.to_thread(extraction.embed_texts,
                                   [p["text"] for p in passages])
    if not vecs or len(vecs) != len(passages):
        return {"indexed": 0, "skipped": False,
                "error": "embeddings unavailable", "errors": errors}

    rows = [{**p, "vec": v} for p, v in zip(passages, vecs)]
    vectorstore.clear_case(case_id)
    n = vectorstore.add_passages(case_id, rows)
    return {"indexed": n, "skipped": False, "errors": errors}


async def search(case_id: str, query: str, k: int = 8) -> dict:
    n = vectorstore.count(case_id)
    qv = await asyncio.to_thread(extraction.embed_texts, [query])
    if not qv:
        return {"results": [], "indexed": n, "error": "embeddings unavailable"}
    return {"results": vectorstore.search(case_id, qv[0], k), "indexed": n}
