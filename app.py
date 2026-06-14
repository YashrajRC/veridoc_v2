"""
FastAPI surface for the verification console.

Security notes:
- `case_id` is validated against the known case list before any path is built,
  which blocks traversal via the path segment.
- Documents are served only from the map that discovery already validated as
  living inside the case directory, with a belt-and-suspenders containment check.
- Every reviewer action is re-validated against the line's CURRENT AI status at
  record time, and the AI's verdict + evidence are snapshotted into the audit row.

Failures in extraction degrade to manual-review lines upstream, so request
handlers here do not 500 on a Gemini outage.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import extraction
import indexing
import store
import vectorstore
from evaluate import discover_documents, evaluate_case, list_cases
from models import (BULK_ACCEPTABLE, Decision, ReviewAction, VerificationStatus,
                    allowed_actions_for, is_action_allowed)

STATIC_DIR = config.BASE_DIR / "static"

app = FastAPI(title="HL Document Verification Console")


@app.on_event("startup")
def _startup() -> None:
    store.init_db()
    vectorstore.init_vs()


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# --- Request bodies ---------------------------------------------------------
class DecisionIn(BaseModel):
    item_id: str
    action: str
    reviewer: str
    note: Optional[str] = None


class BulkIn(BaseModel):
    reviewer: str


# --- Helpers ----------------------------------------------------------------
def _require_case(case_id: str) -> None:
    if case_id not in set(list_cases()):
        raise HTTPException(status_code=404, detail="unknown case")


def _merge_decisions(ev: dict) -> dict:
    latest = store.latest_decision_per_item(ev["case_id"])
    for it in ev["items"]:
        it["decision"] = latest.get(it["id"])
    reviewed = sum(1 for it in ev["items"] if it["decision"])
    ev["summary"]["reviewed"] = reviewed
    ev["summary"]["open"] = ev["summary"]["total"] - reviewed
    return ev


def _evidence_head(item: dict) -> dict:
    ev = item.get("evidence") or []
    return ev[0] if ev else {}


# --- Routes -----------------------------------------------------------------
@app.get("/")
def root():
    idx = STATIC_DIR / "index.html"
    if idx.is_file():
        return FileResponse(str(idx))
    return HTMLResponse(
        "<h1>HL Document Verification Console</h1>"
        "<p>API is running. The review console (static/index.html) is not yet "
        "installed. Available endpoints: <code>/api/cases</code>, "
        "<code>/api/cases/{case_id}</code>.</p>"
    )


@app.get("/api/cases")
def get_cases():
    return {"cases": list_cases()}


@app.get("/api/doc-types")
def get_doc_types():
    return {"doc_types": [{"key": k, "label": config.DOC_LABELS.get(k, k)}
                          for k in config.DOC_KEYS]}


@app.post("/api/cases")
def create_case():
    """Create a new, empty case folder and return its id. The id is generated
    server-side (timestamp + random suffix) so it is safe as a path segment."""
    cid = ("case_" + _dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
           + "_" + uuid.uuid4().hex[:6])
    (config.DATA_DIR / cid).mkdir(parents=True, exist_ok=True)
    return {"case_id": cid}


@app.post("/api/cases/{case_id}/files/{doc_key}")
async def upload_document(case_id: str, doc_key: str, file: UploadFile = File(...)):
    """Receive one document as a multipart file upload and store it as
    <doc_key>.pdf. Uses python-multipart (installed in the environment)."""
    _require_case(case_id)
    if doc_key not in set(config.DOC_KEYS):
        raise HTTPException(status_code=400, detail="unknown document type")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(data) > config.INLINE_MAX_BYTES:
        raise HTTPException(status_code=413,
                            detail="file exceeds inline extraction size limit")
    if data[:1024].find(b"%PDF") == -1:
        raise HTTPException(status_code=400, detail="file does not look like a PDF")
    case_dir = (config.DATA_DIR / case_id).resolve()
    # A case may legitimately hold several documents of the same type (e.g. a main
    # and a supplementary loan agreement). Give each a unique on-disk name so none
    # overwrites another: first "<type>.pdf", then "<type>__2.pdf", "__3", ...
    if not (case_dir / (doc_key + ".pdf")).exists():
        doc_id = doc_key
    else:
        n = 2
        while (case_dir / f"{doc_key}__{n}.pdf").exists():
            n += 1
        doc_id = f"{doc_key}__{n}"
    dest = (case_dir / (doc_id + ".pdf")).resolve()
    try:
        contained = dest.is_relative_to(case_dir)
    except AttributeError:
        contained = str(dest).startswith(str(case_dir))
    if not contained:
        raise HTTPException(status_code=400, detail="invalid destination path")
    dest.write_bytes(data)
    return {"doc_key": doc_key, "doc_id": doc_id, "size": len(data),
            "filename": file.filename}


@app.post("/api/classify")
async def classify_document(file: UploadFile = File(...)):
    """Auto-detect a document's type from its content so the upload UI can
    pre-fill it for the reviewer to confirm/override. Stateless: the bytes are
    classified and discarded here; the file is committed later via the
    /files/{doc_key} endpoint under the (possibly corrected) type."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(data) > config.INLINE_MAX_BYTES:
        raise HTTPException(status_code=413,
                            detail="file exceeds inline extraction size limit")
    if data[:1024].find(b"%PDF") == -1:
        raise HTTPException(status_code=400, detail="file does not look like a PDF")
    result = await asyncio.to_thread(extraction.classify_pdf_sync, data)
    key = result.get("doc_key", "unknown")
    return {
        "filename": file.filename,
        "doc_key": key,
        "label": config.DOC_LABELS.get(key, key),
        "confidence": result.get("confidence", "low"),
        "reason": result.get("reason", ""),
    }


@app.get("/api/cases/{case_id}")
async def get_case(case_id: str):
    _require_case(case_id)
    return await evaluate_case(case_id)


@app.get("/api/cases/{case_id}/document/{doc_key}")
def get_document(case_id: str, doc_key: str):
    _require_case(case_id)
    docs, _ = discover_documents(case_id)
    path = docs.get(doc_key)
    if path is None:
        raise HTTPException(status_code=404, detail="document not in case")
    rp = path.resolve()
    case_dir = (config.DATA_DIR / case_id).resolve()
    try:
        contained = rp.is_relative_to(case_dir)
    except AttributeError:  # Python < 3.9 fallback
        contained = str(rp).startswith(str(case_dir))
    if not contained or not rp.is_file():
        raise HTTPException(status_code=400, detail="invalid document path")
    resp = FileResponse(str(rp), media_type="application/pdf")
    # Inline so the browser PDF viewer opens it (page jump via URL #fragment).
    resp.headers["Content-Disposition"] = f'inline; filename="{doc_key}.pdf"'
    return resp


@app.post("/api/cases/{case_id}/decision")
async def post_decision(case_id: str, body: DecisionIn):
    _require_case(case_id)
    reviewer = (body.reviewer or "").strip()
    if not reviewer:
        raise HTTPException(status_code=400, detail="reviewer is required")
    try:
        action = ReviewAction(body.action)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"unknown action '{body.action}'")

    ev = await evaluate_case(case_id)
    item = next((it for it in ev["items"] if it["id"] == body.item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="unknown checklist item")

    status = VerificationStatus(item["status"])
    if not is_action_allowed(status, action):
        raise HTTPException(
            status_code=400,
            detail={"error": "action not valid for status",
                    "status": item["status"],
                    "allowed": allowed_actions_for(status)},
        )

    head = _evidence_head(item)
    store.record_decision(Decision(
        case_id=case_id, item_id=body.item_id, action=action, reviewer=reviewer,
        created_at=Decision.now_iso(), note=body.note,
        ai_status_at_decision=item["status"], ai_finding_at_decision=item["finding"],
        evidence_doc=head.get("doc_key"), evidence_page=head.get("page"),
        evidence_snippet=head.get("snippet"),
    ))

    ev = _merge_decisions(ev)
    updated = next(it for it in ev["items"] if it["id"] == body.item_id)
    return {"item": updated, "summary": ev["summary"]}


@app.post("/api/cases/{case_id}/accept-auto-verified")
async def accept_auto_verified(case_id: str, body: BulkIn):
    _require_case(case_id)
    reviewer = (body.reviewer or "").strip()
    if not reviewer:
        raise HTTPException(status_code=400, detail="reviewer is required")

    ev = await evaluate_case(case_id)
    accepted: list[str] = []
    for it in ev["items"]:
        if it["decision"] is not None:
            continue
        if VerificationStatus(it["status"]) not in BULK_ACCEPTABLE:
            continue
        head = _evidence_head(it)
        store.record_decision(Decision(
            case_id=case_id, item_id=it["id"], action=ReviewAction.ACCEPT,
            reviewer=reviewer, created_at=Decision.now_iso(),
            note="bulk: accept all auto-verified",
            ai_status_at_decision=it["status"], ai_finding_at_decision=it["finding"],
            evidence_doc=head.get("doc_key"), evidence_page=head.get("page"),
            evidence_snippet=head.get("snippet"),
        ))
        accepted.append(it["id"])

    ev = _merge_decisions(ev)
    return {"accepted": accepted, "summary": ev["summary"]}


@app.get("/api/cases/{case_id}/audit")
def get_audit(case_id: str, item_id: Optional[str] = None):
    _require_case(case_id)
    return {"history": store.history_for_case(case_id, item_id)}


@app.post("/api/cases/{case_id}/index")
async def build_search_index(case_id: str, force: bool = False):
    """Build (or rebuild) the semantic search index for a case. Cheap if already
    built and force is false."""
    _require_case(case_id)
    return await indexing.build_index(case_id, force=force)


@app.get("/api/cases/{case_id}/search")
async def search_case(case_id: str, q: str = "", k: int = 8):
    _require_case(case_id)
    if not q.strip():
        return {"results": [], "indexed": vectorstore.count(case_id)}
    return await indexing.search(case_id, q.strip(), k=max(1, min(k, 25)))
