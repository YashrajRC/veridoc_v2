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

from hl_verifier import config
from hl_verifier import extraction
from hl_verifier.storage import vectorstore
from hl_verifier.pipeline.evaluate import discover_documents


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


# Collapse concurrent build requests for the same case onto a single task. The
# frontend auto-indexes on case open and a user may retry, so several POST /index
# calls can arrive together; without this each would run the full (slow)
# transcription pass at once and race on clear_case/add_passages. The shared task
# is also shielded from the caller, so a dropped/cancelled request lets the build
# finish and populate the index instead of abandoning it half-done.
_INFLIGHT: dict[str, "asyncio.Task"] = {}


async def build_index(case_id: str, force: bool = False) -> dict:
    task = _INFLIGHT.get(case_id)
    if task is None or task.done():
        task = asyncio.ensure_future(_build_index_impl(case_id, force))
        _INFLIGHT[case_id] = task

        def _done(t: "asyncio.Task", _cid: str = case_id) -> None:
            if _INFLIGHT.get(_cid) is t:
                _INFLIGHT.pop(_cid, None)

        task.add_done_callback(_done)
    # A second concurrent caller for the same case awaits the in-flight build
    # (its `force` is honoured by that build); shield keeps the build alive even
    # if this request is cancelled.
    return await asyncio.shield(task)


async def _build_index_impl(case_id: str, force: bool = False) -> dict:
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

    rows = [{**p, "vec": v} for p, v in zip(passages, vecs) if v and len(v) > 0]
    if not rows:
        # Embeddings came back empty/degraded (right count, no usable values).
        # Do NOT clear the existing index — keep whatever is already there rather
        # than wiping search to empty. Re-run once embeddings are healthy.
        return {"indexed": vectorstore.count(case_id), "skipped": False,
                "error": "embeddings returned no usable vectors; kept existing index",
                "errors": errors}
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
    """Score one passage against the query: 1.0 per exact word, 0.6 for a
    substring/prefix hit, partial credit for a close fuzzy match (handles OCR and
    typos like 'aadhar'/'aahaar'), plus a phrase bonus when the full query string
    appears. Higher is a better keyword match; 0 means no keyword overlap."""
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
        if len(qt) >= 4:  # fuzzy only for longer tokens, to avoid noise
            best = 0.0
            for tt in t_tokens:
                if abs(len(tt) - len(qt)) > 2:
                    continue
                r = difflib.SequenceMatcher(None, qt, tt).ratio()
                if r > best:
                    best = r
            if best >= 0.82:
                score += 0.5 * best
    base = score / len(q_tokens)
    if len(q_str.strip()) >= 4 and q_str.strip().lower() in t_low:
        base += 0.5
    return base


def _key(d: dict) -> tuple:
    return (d.get("doc_key"), d.get("page"), d.get("text", "")[:80])


async def search(case_id: str, query: str, k: int = 8) -> dict:
    """Keyword-first hybrid search. Passages that actually contain the query
    words are returned FIRST, best keyword match on top (semantic score only
    breaks ties); semantic-only matches follow underneath for recall on
    paraphrase. This guarantees exact word matches lead, while still surfacing
    related passages. Falls back to keyword-only when embeddings are unavailable."""
    n = vectorstore.count(case_id)
    passages = vectorstore.all_passages(case_id)
    if not passages:
        return {"results": [], "indexed": n}

    q_tokens = _tokenize(query)
    pool = max(30, k * 4)

    # Keyword scores for every passage.
    lex_by_key: dict[tuple, tuple] = {}
    for p in passages:
        sc = _lexical_score(q_tokens, query, p.get("text", ""))
        if sc > 0:
            lex_by_key[_key(p)] = (sc, p)

    # Semantic scores (best-effort; empty if embeddings are unavailable).
    qv = await asyncio.to_thread(extraction.embed_texts, [query])
    sem_avail = bool(qv)
    sem_by_key: dict[tuple, tuple] = {}
    if sem_avail:
        for r in vectorstore.search(case_id, qv[0], k=pool):
            sem_by_key[_key(r)] = (r.get("score", 0.0), r)

    def row(meta, score, kind):
        return {"doc_key": meta.get("doc_key", ""), "page": meta.get("page"),
                "text": meta.get("text", ""), "item_id": meta.get("item_id", ""),
                "status": meta.get("status", ""), "score": round(float(score), 4),
                "match": kind}

    # Tier 1 — keyword hits, best keyword score first (semantic breaks ties).
    tier1 = [(sc, sem_by_key.get(key, (0.0, None))[0], p)
             for key, (sc, p) in lex_by_key.items()]
    tier1.sort(key=lambda x: (-x[0], -x[1]))
    results = [row(p, sc, "exact" if sc >= 1.0 else "partial")
               for sc, _, p in tier1]

    # Tier 2 — semantic-only matches (no keyword overlap) for recall.
    if sem_avail:
        sem_only = [(score, r) for key, (score, r) in sem_by_key.items()
                    if key not in lex_by_key and score > 0]
        sem_only.sort(key=lambda x: -x[0])
        results += [row(r, score, "related") for score, r in sem_only]

    out = {"results": results[:max(1, k)], "indexed": n}
    if not sem_avail:
        out["mode"] = "keyword"
    return out
