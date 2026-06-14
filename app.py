"""
Thin entrypoint.

The application now lives in the `hl_verifier` package; this shim keeps the
familiar Workbench run command working unchanged:

    uvicorn app:app --host 0.0.0.0 --port 8080

It simply re-exports the FastAPI app from hl_verifier.api.app.
"""
from hl_verifier.api.app import app  # noqa: F401
