"""
Pre-warm the extraction cache.

Run this once before a demo so the live evaluation is served from cache and is
effectively instant:

    python warm.py

It also initialises the decision database. It does NOT start the server.
"""
from __future__ import annotations

import asyncio

import config
import store
import vectorstore
from evaluate import discover_documents, list_cases
from extraction import GEMINI_AVAILABLE, _GEMINI_INIT_ERROR, extract_documents
from indexing import build_index


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

    for case_id in cases:
        docs, unknown = discover_documents(case_id)
        print(f"\n[{case_id}] {len(docs)} recognised document(s): "
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
                filled = sum(1 for f in de.fields.values() if f.value is not None)
                print(f"   {key}: ok ({filled}/{len(de.fields)} fields populated)")
            else:
                print(f"   {key}: FAILED - {de.error}")

        idx = await build_index(case_id, force=True)
        msg = f"   search index: {idx.get('indexed', 0)} passage(s)"
        if idx.get("error"):
            msg += f" ({idx['error']})"
        if idx.get("errors"):
            msg += f"; transcription errors: {idx['errors']}"
        print(msg)

    print("\nDone. Cache:", config.CACHE_DIR)


if __name__ == "__main__":
    asyncio.run(main())
