"""
Thin entrypoint for the one-off pre-warm script, so the familiar command still
works from the project root:

    python warm.py

The implementation lives in hl_verifier.warm.
"""
import asyncio

from hl_verifier.warm import main

if __name__ == "__main__":
    asyncio.run(main())
