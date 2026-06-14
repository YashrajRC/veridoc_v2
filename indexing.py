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
import difflib
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


# ---------------------------------------------------------------------------
# Hybrid search: lexical (exact/partial/fuzzy term overlap on the raw text) +
# semantic (embedding cosine). Pure semantic search alone misses exact terms
# (names, numbers, "Aadhaar") and is thrown by typos; lexical alone misses
# paraphrase. Blending the two — and falling back to lexical when embeddings are
# unavailable — is markedly better on a loan file and needs no extra services.
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(s: str) -> list[str]:
    return _TOKEN_RE.findall(str(s).lower())


def _lexical_score(q_tokens: list[str], q_str: str, text: str) -> float:
    """Score one passage against the query: 1.0 per exact token, 0.6 for a
    substring/prefix hit, a partial credit for a close fuzzy match (handles OCR
    and typos like 'aadhar'/'aahaar'), plus a phrase bonus for a full-string hit.
    Normalised to roughly [0, ~1.5]."""
    if not q_tokens:
        return 0.0
    t_low = text.lower()
    t_tokens = set(_tokenize(text))
    if not t_tokens:
        return 0.0
    score = 0.0
    for qt in q_tokens:
        if qt in t_tokens:
            score += 1.0
            continue
        if len(qt) >= 3 and any(
                (qt in tt or tt in qt) for tt in t_tokens if abs(len(tt) - len(qt)) <= 3):
            score += 0.6
            continue
        best = 0.0
        for tt in t_tokens:
            if abs(len(tt) - len(qt)) > 2:
                continue
            r = difflib.SequenceMatcher(None, qt, tt).ratio()
            if r > best:
                best = r
        if best >= 0.78:
            score += 0.5 * best
    base = score / len(q_tokens)
    if len(q_str.strip()) >= 4 and q_str.strip().lower() in t_low:
        base += 0.5
    return base


def _key(d: dict) -> tuple:
    return (d.get("doc_key"), d.get("page"), d.get("text", "")[:80])


async def search(case_id: str, query: str, k: int = 8) -> dict:
    n = vectorstore.count(case_id)
    passages = vectorstore.all_passages(case_id)
    if not passages:
        return {"results": [], "indexed": n}

    q_tokens = _tokenize(query)
    pool = max(25, k * 3)

    # Lexical half (always available).
    lex = []
    for p in passages:
        sc = _lexical_score(q_tokens, query, p.get("text", ""))
        if sc > 0:
            lex.append((sc, p))
    lex.sort(key=lambda x: -x[0])
    lex_top = lex[:pool]

    cand: dict[tuple, dict] = {}
    for sc, p in lex_top:
        cand[_key(p)] = {"meta": p, "lex": sc, "sem": 0.0}

    # Semantic half (best-effort; absent if embeddings are unavailable).
    qv = await asyncio.to_thread(extraction.embed_texts, [query])
    sem_avail = bool(qv)
    if sem_avail:
        for r in vectorstore.search(case_id, qv[0], k=pool):
            key = _key(r)
            if key in cand:
                cand[key]["sem"] = r.get("score", 0.0)
            else:
                cand[key] = {"meta": r, "lex": 0.0, "sem": r.get("score", 0.0)}

    if not cand:
        return {"results": [], "indexed": n,
                **({} if sem_avail else {"mode": "keyword"})}

    lex_max = max((c["lex"] for c in cand.values()), default=0.0) or 1.0
    sem_max = max((c["sem"] for c in cand.values()), default=0.0)

    results = []
    for c in cand.values():
        ln = c["lex"] / lex_max
        sn = (max(0.0, c["sem"]) / sem_max) if (sem_avail and sem_max > 0) else 0.0
        final = (0.55 * ln + 0.45 * sn) if sem_avail else ln
        m = c["meta"]
        results.append({
            "doc_key": m.get("doc_key", ""), "page": m.get("page"),
            "text": m.get("text", ""), "item_id": m.get("item_id", ""),
            "status": m.get("status", ""), "score": round(final, 4),
        })
    results.sort(key=lambda x: -x["score"])
    out = {"results": results[:max(1, k)], "indexed": n}
    if not sem_avail:
        out["mode"] = "keyword"  # embeddings unavailable -> lexical-only
    return out
