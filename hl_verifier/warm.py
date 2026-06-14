"""
Pre-warm the extraction cache.

Run this once before a demo so the live evaluation is served from cache and is
effectively instant:

    python warm.py

It also initialises the decision database. It does NOT start the server.
"""
from __future__ import annotations

import asyncio

from hl_verifier import config
from hl_verifier.storage import store
from hl_verifier.storage import vectorstore
from hl_verifier.pipeline.evaluate import discover_documents, list_cases
from hl_verifier.extraction import (GEMINI_AVAILABLE, _GEMINI_INIT_ERROR,
                                    extract_documents)
from hl_verifier.pipeline.indexing import build_index


async def main() -> None:
    store.init_db()
    vectorstore.init_vs()

    if not GEMINI_AVAILABLE:
        print(f"WARNING: Gemini is not available ({_GEMINI_INIT_ERROR}).")
        print("Extraction will fail and lines will fall back to manual review.")

    cases = list_cases()
    if not cases:
        print(f"No case folders found under {config.DATA_DIR}.")
        print("Expected layout: DATA_DIR/<case_id>/<document>.pdf")
        return

    # Resumable by design: extraction and transcription are cached by file
    # content, so re-running only does the work that is new. We print a per-case
    # progress line and isolate failures so one bad case never aborts the run.
    total = len(cases)
    ok_docs = failed_docs = total_passages = cases_failed = 0
    for n, case_id in enumerate(cases, 1):
        try:
            docs, unknown = discover_documents(case_id)
            print(f"\n[{n}/{total}] {case_id}: {len(docs)} recognised document(s): "
                  f"{', '.join(sorted(docs)) or '(none)'}")
            if unknown:
                print(f"   unrecognised files (ignored): {', '.join(unknown)}")
            missing = [k for k in config.DOC_KEYS if k not in docs]
            if missing:
                print(f"   missing expected documents: {', '.join(missing)}")
            if not docs:
                continue

            ext = await extract_documents(docs, use_cache=True)
            for key in sorted(ext):
                de = ext[key]
                if de.ok:
                    ok_docs += 1
                    filled = sum(1 for f in de.fields.values() if f.value is not None)
                    print(f"   {key}: ok ({filled}/{len(de.fields)} fields populated)")
                else:
                    failed_docs += 1
                    print(f"   {key}: FAILED - {de.error}")

            idx = await build_index(case_id, force=True)
            total_passages += idx.get("indexed", 0)
            msg = f"   search index: {idx.get('indexed', 0)} passage(s)"
            if idx.get("error"):
                msg += f" ({idx['error']})"
            if idx.get("errors"):
                msg += f"; transcription errors: {idx['errors']}"
            print(msg)
        except Exception as exc:  # isolate: keep warming the remaining cases
            cases_failed += 1
            print(f"   ERROR warming {case_id}: {type(exc).__name__}: {exc}")

    print(f"\nDone. {total} case(s): {ok_docs} document(s) extracted, "
          f"{failed_docs} failed, {total_passages} search passage(s) indexed"
          + (f", {cases_failed} case(s) errored" if cases_failed else "") + ".")
    print("Cache:", config.CACHE_DIR)


if __name__ == "__main__":
    asyncio.run(main())
