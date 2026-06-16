"""
Grounded Q&A assistant (Tier-1 RAG).

A loan officer asks a natural-language question about ONE case ("what is the
sanctioned amount?", "compare the sanction amount across the documents", "is the
property address consistent?"). We answer it by:

  1. Retrieving the most relevant document passages via the existing hybrid
     search index (the OCR/transcription text).
  2. Reading the case's already-computed VERIFIED findings + reconciliation
     values from evaluate_case — the same structured facts the checklist shows,
     including the cross-document amount/name/address comparisons.
  3. Asking Gemini to answer using ONLY that context, with document + page
     citations, and to say so plainly when the answer is not present.

This is deliberately additive and read-only: it calls the existing extraction,
indexing and evaluation code and never modifies the verification logic. It
degrades gracefully — if the search index is empty it still answers from the
structured findings; if Gemini is unavailable it returns a clear message rather
than raising.
"""
from __future__ import annotations

import asyncio

from hl_verifier import config
from hl_verifier import extraction
from hl_verifier.pipeline import indexing
from hl_verifier.pipeline.evaluate import evaluate_case


_SYSTEM = (
    "You are an assistant helping a credit/operations officer review an Indian "
    "home-loan file before disbursement. Answer the QUESTION using ONLY the "
    "CONTEXT below — it has two parts: VERIFIED FINDINGS (produced by the "
    "verification engine, already cross-checked) and DOCUMENT EXCERPTS "
    "(retrieved snippets of the actual document text).\n"
    "Rules:\n"
    "- Use only the context. Do NOT use outside knowledge or guess. If the "
    "answer is not in the context, say: \"I couldn't find that in this case's "
    "documents.\"\n"
    "- Quote amounts, names, dates and numbers exactly as written.\n"
    "- Cite the document (and page when shown), e.g. \"(Sanction letter, p.1)\".\n"
    "- For a comparison, state each document's value and whether they match.\n"
    "- Be concise and factual. Prefer the VERIFIED FINDINGS for figures and "
    "decisions; use the excerpts for supporting detail.\n"
)


def _label(doc_key: str) -> str:
    """Human label for a document key (strips any '__N' instance suffix)."""
    if not doc_key:
        return "document"
    base = doc_key.split("__", 1)[0]
    return config.DOC_LABELS.get(base, base)


def _render_recon(recon: dict | None) -> str:
    """Flatten reconciliation values ({doc_key: cell}) into 'doc=value' pairs so
    the model can compare them. A cell is either flat (has its own 'display') or
    nests sub-fields (e.g. survey/address) each with a 'display'."""
    if not isinstance(recon, dict):
        return ""
    pairs: list[str] = []
    for doc_key, cell in recon.items():
        if not isinstance(cell, dict):
            continue
        if "display" in cell:
            disp = str(cell.get("display") or "").strip()
            if disp:
                pairs.append(f"{_label(doc_key)}={disp}")
        else:
            for sub_k, sub in cell.items():
                if isinstance(sub, dict) and str(sub.get("display") or "").strip():
                    pairs.append(f"{_label(doc_key)}.{sub_k}={str(sub['display']).strip()}")
    return ", ".join(pairs)


async def _structured_facts(case_id: str) -> list[dict]:
    """The case's verified findings (+ reconciliation values), best-effort.
    Reuses evaluate_case (cache-backed), so it adds no new model calls when the
    case is already extracted. Never raises — returns [] on any failure."""
    try:
        ev = await evaluate_case(case_id)
    except Exception:
        return []
    facts: list[dict] = []
    for it in ev.get("items", []):
        finding = (it.get("finding") or "").strip()
        if not finding:
            continue
        facts.append({
            "text": (it.get("text") or "").strip(),
            "doc": it.get("source_doc") or "",
            "status": it.get("status") or "",
            "finding": finding,
            "recon": (it.get("extra") or {}).get("recon_values"),
        })
    return facts


def _build_context(facts: list[dict], passages: list[dict]) -> str:
    blocks: list[str] = []
    if facts:
        lines = []
        for f in facts:
            doc = _label(f["doc"]) if f["doc"] else "case"
            line = f"- [{doc}] {f['text']}: {f['finding']}"
            recon = _render_recon(f.get("recon"))
            if recon:
                line += f" (values: {recon})"
            lines.append(line)
        blocks.append("VERIFIED FINDINGS (from the verification engine):\n"
                      + "\n".join(lines))
    if passages:
        plines = []
        for i, p in enumerate(passages, 1):
            page = p.get("page")
            cite = f"{_label(p.get('doc_key', ''))}, p.{page}" if page else _label(p.get("doc_key", ""))
            text = " ".join(str(p.get("text", "")).split())
            plines.append(f"[{i}] ({cite}) {text}")
        blocks.append("DOCUMENT EXCERPTS (retrieved):\n" + "\n".join(plines))
    return "\n\n".join(blocks)


def _sources(passages: list[dict]) -> list[dict]:
    """Deduplicated (doc, page) citations from the retrieved passages, in order,
    so the UI can render 'view in document' links beneath the answer."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for p in passages:
        key = (p.get("doc_key", ""), p.get("page"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"doc_key": p.get("doc_key", ""),
                    "label": _label(p.get("doc_key", "")), "page": p.get("page")})
    return out


async def ask(case_id: str, question: str, k: int = 6) -> dict:
    """Answer one question about a case, grounded in its passages + verified
    findings. Returns {answer, sources, passages, indexed, used_facts, error?}."""
    question = (question or "").strip()
    if not question:
        return {"answer": "", "error": "empty question", "sources": [],
                "passages": [], "indexed": 0, "used_facts": False}
    if not extraction.GEMINI_AVAILABLE:
        return {"answer": "", "sources": [], "passages": [], "indexed": 0,
                "used_facts": False,
                "error": f"assistant unavailable ({extraction._GEMINI_INIT_ERROR})"}

    # Retrieval (passages) and structured facts run concurrently.
    search_res, facts = await asyncio.gather(
        indexing.search(case_id, question, k=k),
        _structured_facts(case_id),
    )
    passages = search_res.get("results", []) if isinstance(search_res, dict) else []
    indexed = search_res.get("indexed", 0) if isinstance(search_res, dict) else 0

    context = _build_context(facts, passages)
    if not context.strip():
        return {"answer": "I couldn't find any indexed content for this case yet. "
                          "Build the search index (open the case) and try again.",
                "sources": [], "passages": [], "indexed": indexed,
                "used_facts": False}

    prompt = (f"{_SYSTEM}\nCONTEXT:\n{context}\n\nQUESTION: {question}\n\nANSWER:")

    last_err = "unknown error"
    for attempt in range(config.GEMINI_MAX_RETRIES + 1):
        try:
            answer = await asyncio.wait_for(
                asyncio.to_thread(extraction._gen, [prompt], False),
                timeout=config.GEMINI_TIMEOUT_S,
            )
            return {"answer": (answer or "").strip(), "sources": _sources(passages),
                    "passages": passages, "indexed": indexed,
                    "used_facts": bool(facts)}
        except asyncio.TimeoutError:
            last_err = f"timeout after {config.GEMINI_TIMEOUT_S}s"
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        if attempt < config.GEMINI_MAX_RETRIES:
            await asyncio.sleep(1.0 * (attempt + 1))

    return {"answer": "", "sources": _sources(passages), "passages": passages,
            "indexed": indexed, "used_facts": bool(facts),
            "error": f"the assistant could not answer ({last_err})"}
