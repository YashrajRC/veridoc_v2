"""
Document extraction via Gemini (multimodal), substituting for Document AI.

Strategy for speed and cost: ONE call per document (not per checklist line),
extracting every field that document can supply. Calls fan out concurrently,
bounded by a semaphore, and each result is cached by file content hash so a
pre-warm run makes the live demo instant.

Grounding: every field comes back as {value, page, snippet, confidence}. The
verbatim snippet and page are the audit hook for "view in document"; a null
value with an empty snippet is the signal that the model did not find the field
(it is instructed never to guess), which downstream forces human review.

Robustness: malformed model output, safety blocks, quota errors, timeouts and
oversized files are all caught and converted into an explicit failed extraction
rather than an exception that takes down the request.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from hl_verifier import config

# --- SDK import is guarded so the rest of the app works without live Gemini ---
GEMINI_AVAILABLE = False
_GEMINI_INIT_ERROR: Optional[str] = None
_client = None
try:
    from google import genai
    from google.genai import types

    if config.GCP_PROJECT:
        _client = genai.Client(vertexai=True, project=config.GCP_PROJECT,
                               location=config.GCP_LOCATION)
    else:
        # may still succeed via ambient project/location env vars on Workbench.
        _client = genai.Client(vertexai=True, location=config.GCP_LOCATION)
    GEMINI_AVAILABLE = True
except Exception as exc:  # broad on purpose: any import/init issue -> degrade
    _GEMINI_INIT_ERROR = f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Per-document field specifications. Each entry maps a field name to a short
# extraction hint. "conditions" is handled specially (it is a list of objects).
# ---------------------------------------------------------------------------
DOC_FIELDS: dict[str, dict[str, str]] = {
    "technical": {
        "applicant_name": "name of the applicant/borrower",
        "property_address": "full address of the property valued",
        "survey_or_plot_no": "survey number / plot number / khasra of the property",
        "market_value": "assessed market/fair value, with currency as written",
        "construction_stage": "stage of construction or completion",
        "valuer_signature_present": "true if a valuer signature is visible (yes/no)",
        "valuer_seal_present": "true if a valuer stamp/seal is visible (yes/no)",
    },
    "legal": {
        "applicant_name": "name of the applicant/borrower",
        "property_address": "address of the property in the search report",
        "survey_or_plot_no": "survey/plot number of the property",
        "title_status": "the report's conclusion on title (e.g. clear and marketable)",
        "encumbrances": "any encumbrance/charge reported, else state none",
        "advocate_signature_present": "true if advocate signature visible (yes/no)",
        "advocate_seal_present": "true if advocate stamp/seal visible (yes/no)",
    },
    "sanction": {
        "applicant_name": "name of the sanctioned applicant",
        "sanctioned_amount": "sanctioned loan amount, with currency as written",
        "roi": "rate of interest / ROI, as written (e.g. 8.35% p.a.)",
        "processing_fee": "processing fee, as written (amount or a percentage)",
        "login_fee": "login / processing-login fee amount if separately stated",
        "tenure_months": "tenure in months if stated",
        "property_address": "property address if stated in the sanction letter",
        "conditions": "LIST of sanction conditions",  # special-cased
    },
    "loan_agreement": {
        "borrower_name": "name of the borrower in the agreement",
        "loan_amount": "loan amount in the agreement, currency as written",
        "borrower_signature_present": "true if borrower signature visible (yes/no)",
    },
    "insurance": {
        "insured_name": "name of the insured",
        "sum_assured": "sum assured / insured value, currency as written",
        "property_address": "address of the insured property if stated",
        "bank_interest_noted": "true if lender/bank interest, hypothecation or loss payee is noted (yes/no)",
        "policy_end": "policy end / expiry date if stated",
    },
    "drl": {
        "applicant_name": "name of the applicant making the request",
        "requested_amount": "amount requested for disbursement, currency as written",
        "borrower_signature_present": "true if borrower signature visible (yes/no)",
    },
    "affidavit": {
        "deponent_name": "name of the deponent",
        "notarised": "true if notarised / attested (yes/no)",
        "stamp_present": "true if executed on stamp paper / franking visible (yes/no)",
        "signature_present": "true if deponent signature visible (yes/no)",
    },
    "rcu": {
        "applicant_name": "name of the applicant screened",
        "verdict": "the RCU verdict (e.g. positive / negative / refer)",
        "aadhaar_result": "ANY Aadhaar/UIDAI detail shown — the status / seeding status / verification result (e.g. OPERATIVE, seeded, verified, matched) OR the (masked) Aadhaar number if one is printed. Quote it as written.",
        "pan_result": "ANY PAN detail shown — the PAN status / type / verification result (e.g. VALID, active, operative, matched) OR the PAN itself if printed. Quote it as written.",
        "bank_statement_result": "result of bank statement / bank account verification if present, as written",
        "kyc_documents": "which KYC/identity documents were submitted or checked (e.g. Aadhaar card, PAN card, bank statement, masked Aadhaar copy)",
        "remarks": "any adverse remark, else state none",
    },
    "fi": {
        "applicant_name": "name of the applicant verified",
        "residence_verdict": "verdict of the residence verification if present",
        "office_verdict": "verdict of the office/business verification if present",
    },
    "enduse": {
        "applicant_name": "name of the declarant",
        "declared_end_use": "the declared purpose/use of the loan",
        "signature_present": "true if signature visible (yes/no)",
    },
}

_PROMPT_HEADER = (
    "You are extracting fields from a single lending document for an audit "
    "trail. The document is a SCANNED PDF and may be medium or low quality, "
    "skewed, or partly faint. Read carefully. Where text is unclear or "
    "illegible, do not guess: set the value to null and confidence to low. "
    "Return ONLY a JSON object, no prose and no markdown fences. For each "
    "requested field output an object with keys: value, page, snippet, "
    "confidence. 'value' is the extracted value exactly as written (or null if "
    "absent or illegible). 'page' is the 1-based page number you read it from "
    "(or null). 'snippet' is a short verbatim quote from the document "
    "supporting the value (or empty string). 'confidence' is one of low, "
    "medium, high; use low whenever the scan quality made you unsure. Keep "
    "snippets under 20 words."
)


@dataclass
class ExtractedField:
    value: Any = None
    page: Optional[int] = None
    snippet: str = ""
    confidence: str = "low"
    # The id of the physical document this value was read from. For a type with a
    # single document this equals the type; for multiple (e.g. main + supplementary
    # loan agreement) it pinpoints which file, so the evidence link opens the right
    # PDF. Set per-placement after extraction, never cached (cache is by content).
    source: Optional[str] = None

    @staticmethod
    def from_obj(obj: Any) -> "ExtractedField":
        if not isinstance(obj, dict):
            return ExtractedField()
        page = obj.get("page")
        if isinstance(page, bool):
            page = None
        elif isinstance(page, float) and page.is_integer():
            page = int(page)
        if not isinstance(page, int) or page < 1:
            page = None
        conf = str(obj.get("confidence", "low")).lower()
        if conf not in ("low", "medium", "high"):
            conf = "low"
        snippet = obj.get("snippet") or ""
        if not isinstance(snippet, str):
            snippet = str(snippet)
        return ExtractedField(value=obj.get("value"), page=page,
                              snippet=snippet[:300], confidence=conf)


@dataclass
class DocumentExtraction:
    doc_key: str
    fields: dict[str, ExtractedField] = field(default_factory=dict)
    ok: bool = True
    error: Optional[str] = None

    def get(self, name: str) -> ExtractedField:
        return self.fields.get(name, ExtractedField())

    def to_dict(self) -> dict:
        return {
            "doc_key": self.doc_key,
            "ok": self.ok,
            "error": self.error,
            "fields": {k: vars(v) for k, v in self.fields.items()},
        }

    @staticmethod
    def from_dict(d: dict) -> "DocumentExtraction":
        de = DocumentExtraction(doc_key=d["doc_key"], ok=d.get("ok", True),
                                error=d.get("error"))
        for k, v in (d.get("fields") or {}).items():
            de.fields[k] = ExtractedField(**v)
        return de


# ---------------------------------------------------------------------------
# Defensive JSON extraction from a model response.
# ---------------------------------------------------------------------------
def _coerce_json(text: str) -> Optional[dict]:
    if not text:
        return None
    s = text.strip()
    # Strip a leading ```json / ``` fence if present.
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
        s = s.strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    # Fall back to the outermost {...} span.
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(s[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def _build_prompt(doc_key: str) -> str:
    spec = DOC_FIELDS[doc_key]
    lines = [f"- {name}: {hint}" for name, hint in spec.items()]
    extra = ""
    if "conditions" in spec:
        extra = (
            "\nFor 'conditions', 'value' must be a JSON array; each element is "
            "an object {text, type, page, snippet} where 'type' is OTC, PDD or "
            "UNKNOWN based on how the letter classifies it (UNKNOWN if not "
            "labelled). Set page/snippet/confidence at the top level as usual."
        )
    return f"{_PROMPT_HEADER}\n\nFields:\n" + "\n".join(lines) + extra


def _parse_extraction(doc_key: str, obj: dict) -> dict[str, ExtractedField]:
    out: dict[str, ExtractedField] = {}
    for name in DOC_FIELDS[doc_key]:
        out[name] = ExtractedField.from_obj(obj.get(name))
    return out


# ---------------------------------------------------------------------------
# Isolated google-genai calls. Concurrency is handled by callers via
# asyncio.to_thread; these are synchronous.
# ---------------------------------------------------------------------------
def _gen(parts, json_mode: bool) -> str:
    if json_mode:
        cfg = types.GenerateContentConfig(
            temperature=0.0, response_mime_type="application/json")
    else:
        cfg = types.GenerateContentConfig(temperature=0.0)
    resp = _client.models.generate_content(
        model=config.GEMINI_MODEL, contents=parts, config=cfg)
    txt = getattr(resp, "text", None)
    if txt:
        return txt
    # No text part (e.g. safety block / empty). Surface a reason if present.
    reason = ""
    try:
        cands = getattr(resp, "candidates", None) or []
        if cands:
            reason = str(getattr(cands[0], "finish_reason", ""))
    except Exception:
        pass
    raise RuntimeError(f"no text in response (finish_reason={reason})")


def _call_gemini_sync(pdf_bytes: bytes, prompt: str) -> str:
    part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
    return _gen([part, prompt], json_mode=True)


def transcribe_sync(pdf_bytes: bytes) -> str:
    """Plain-text transcription of a (scanned) PDF, with simple page markers.
    Used to build the search index. Plain text is more robust than JSON for
    long OCR output."""
    part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
    prompt = (
        "Transcribe this scanned document to plain text. Preserve reading order. "
        "Begin each page with a line '=== PAGE n ===' (n is the 1-based page "
        "number). Output only the transcription, no commentary.")
    return _gen([part, prompt], json_mode=False)


def classify_pdf_sync(pdf_bytes: bytes) -> dict:
    """Ask Gemini what kind of loan document this is.

    Returns {"doc_key", "confidence", "reason"} where doc_key is one of
    config.DOC_KEYS or "unknown". This is the auto-detection step that lets the
    upload flow pre-fill a document type for the reviewer to confirm/override; it
    does not touch the downstream verification logic. Never raises — any failure
    degrades to {"unknown", "low", <reason>} so the reviewer simply chooses by
    hand."""
    if not GEMINI_AVAILABLE:
        return {"doc_key": "unknown", "confidence": "low",
                "reason": _GEMINI_INIT_ERROR or "Gemini unavailable"}
    catalog = "\n".join(f"- {k}: {config.DOC_LABELS.get(k, k)}"
                        for k in config.DOC_KEYS)
    prompt = (
        "You are classifying a single document from an Indian home-loan file. "
        "From its content (title, headings, fields, stamps) decide which ONE of "
        "these document types it is:\n" + catalog + "\n\n"
        "Respond ONLY as JSON: {\"doc_key\": <one key from the list above, or "
        "\"unknown\" if it matches none>, \"confidence\": \"high\"|\"medium\"|"
        "\"low\", \"reason\": <short phrase citing what you saw>}. If you are not "
        "sure, use \"unknown\" with low confidence rather than guessing."
    )
    part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
    try:
        raw = _gen([part, prompt], json_mode=True)
        obj = _coerce_json(raw) or {}
    except Exception as exc:
        return {"doc_key": "unknown", "confidence": "low",
                "reason": f"{type(exc).__name__}: {exc}"}
    key = str(obj.get("doc_key", "unknown")).strip()
    if key not in config.DOC_KEYS:
        key = "unknown"
    conf = str(obj.get("confidence", "low")).strip().lower()
    if conf not in ("high", "medium", "low"):
        conf = "low"
    return {"doc_key": key, "confidence": conf,
            "reason": str(obj.get("reason", ""))[:200]}


def embed_texts(texts: list[str]) -> Optional[list[list[float]]]:
    """Embed a list of texts. Returns one vector per input, or None on failure
    or when Gemini is unavailable."""
    if not GEMINI_AVAILABLE or not texts:
        return None
    try:
        out: list[list[float]] = []
        batch = 50
        for i in range(0, len(texts), batch):
            resp = _client.models.embed_content(
                model=config.EMBED_MODEL, contents=texts[i:i + batch])
            embs = getattr(resp, "embeddings", None) or []
            for e in embs:
                out.append([float(x) for x in e.values])
        return out if len(out) == len(texts) else None
    except Exception:
        return None


def _cache_path(file_hash: str) -> Path:
    return config.CACHE_DIR / f"{file_hash}.json"


def _read_cache(file_hash: str, doc_key: str) -> Optional[DocumentExtraction]:
    p = _cache_path(file_hash)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        de = DocumentExtraction.from_dict(d)
        # Only trust the cache if it is for this doc type.
        if de.doc_key != doc_key:
            return None
        # Auto-invalidate a cache written before new fields were added to the
        # spec (e.g. the KYC or fee fields): if the current field spec has keys
        # this cached extraction lacks, treat it as stale and re-extract, so the
        # added checks are populated instead of reading empty values. (A failed
        # extraction is kept as-is; re-trying it is the caller's concern.)
        if de.ok:
            spec = set(DOC_FIELDS.get(doc_key, {}))
            if not spec.issubset(de.fields.keys()):
                return None
        return de
    except Exception:
        return None  # corrupt cache -> re-extract


def _write_cache(file_hash: str, de: DocumentExtraction) -> None:
    try:
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(file_hash).write_text(
            json.dumps(de.to_dict(), ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass  # caching is best-effort; never fail the extraction over it


def doc_type_of(doc_id: str) -> str:
    """The canonical type of a physical document id. Ids are '<type>' for the
    first document of a type and '<type>__N' for the rest, so the type is the
    part before the first '__'."""
    return doc_id.split("__", 1)[0]


def _tag_source(de: DocumentExtraction, doc_id: str) -> DocumentExtraction:
    """Stamp every field with the physical document it came from. Done after
    cache read/write so the cache stays keyed purely by file content."""
    for ef in de.fields.values():
        ef.source = doc_id
    return de


async def extract_document(doc_id: str, doc_type: str, pdf_path: Path,
                           sem: asyncio.Semaphore,
                           use_cache: bool = True) -> DocumentExtraction:
    """Extract one physical document. `doc_type` selects the field spec/prompt;
    `doc_id` is recorded as the source of every field. Always returns a
    DocumentExtraction; failures are captured in .ok/.error rather than raised."""
    try:
        pdf_bytes = pdf_path.read_bytes()
    except Exception as exc:
        return DocumentExtraction(doc_type, ok=False,
                                  error=f"cannot read file: {exc}")
    if not pdf_bytes:
        return DocumentExtraction(doc_type, ok=False, error="empty file")
    if len(pdf_bytes) > config.INLINE_MAX_BYTES:
        return DocumentExtraction(
            doc_type, ok=False,
            error=("file exceeds inline size limit; GCS staging not implemented"))

    file_hash = hashlib.sha256(pdf_bytes).hexdigest()
    if use_cache:
        cached = _read_cache(file_hash, doc_type)
        if cached is not None:
            return _tag_source(cached, doc_id)

    if not GEMINI_AVAILABLE:
        return DocumentExtraction(
            doc_type, ok=False,
            error=f"Gemini unavailable ({_GEMINI_INIT_ERROR})")

    prompt = _build_prompt(doc_type)
    last_err = "unknown error"
    async with sem:
        for attempt in range(config.GEMINI_MAX_RETRIES + 1):
            try:
                raw = await asyncio.wait_for(
                    asyncio.to_thread(_call_gemini_sync, pdf_bytes, prompt),
                    timeout=config.GEMINI_TIMEOUT_S,
                )
                obj = _coerce_json(raw)
                if obj is None:
                    last_err = "model did not return parseable JSON"
                else:
                    de = DocumentExtraction(doc_type,
                                            fields=_parse_extraction(doc_type, obj))
                    _write_cache(file_hash, de)
                    return _tag_source(de, doc_id)
            except asyncio.TimeoutError:
                last_err = f"timeout after {config.GEMINI_TIMEOUT_S}s"
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            if attempt < config.GEMINI_MAX_RETRIES:
                await asyncio.sleep(1.5 * (attempt + 1))

    return DocumentExtraction(doc_type, ok=False, error=last_err)


async def extract_documents(id_to_path: dict[str, Path],
                            use_cache: bool = True) -> dict[str, DocumentExtraction]:
    """Concurrently extract a mapping of doc_id -> path. Returns one
    DocumentExtraction per physical document, keyed by doc_id (the type is
    derived from the id). Callers merge these per type via merge_extractions."""
    sem = asyncio.Semaphore(config.MAX_CONCURRENCY)
    items = list(id_to_path.items())
    results = await asyncio.gather(
        *[extract_document(doc_id, doc_type_of(doc_id), p, sem, use_cache)
          for doc_id, p in items]
    )
    return {doc_id: r for (doc_id, _), r in zip(items, results)}


_CONF_RANK = {"high": 3, "medium": 2, "low": 1}


def merge_extractions(doc_type: str,
                      parts: list[DocumentExtraction]) -> DocumentExtraction:
    """Combine all physical documents of one type into a single extraction the
    rest of the pipeline can consume unchanged.

    For each field, take the best non-null value across the documents (highest
    confidence wins; ties keep the first), preserving its source doc_id, page and
    snippet. A field is therefore 'found' if ANY document of the type supplies it
    (e.g. a value present only on a supplementary loan agreement is used). With a
    single document this returns exactly that document's fields."""
    oks = [p for p in parts if p.ok]
    merged = DocumentExtraction(doc_key=doc_type, ok=bool(oks))
    spec_names = list(DOC_FIELDS.get(doc_type, {}).keys())
    if not oks:
        merged.error = ("; ".join(p.error for p in parts if p.error)
                        or "no readable document")
        for name in spec_names:
            merged.fields[name] = ExtractedField(source=parts[0].doc_key if parts else doc_type)
        return merged
    # Single document: pass its fields straight through (identical to old path).
    if len(oks) == 1:
        merged.fields = dict(oks[0].fields)
        return merged
    for name in spec_names:
        best: Optional[ExtractedField] = None
        for p in oks:
            ef = p.get(name)
            if ef.value is None:
                continue
            if best is None or _CONF_RANK.get(ef.confidence, 0) > _CONF_RANK.get(best.confidence, 0):
                best = ef
        merged.fields[name] = best if best is not None else ExtractedField()
    return merged


def _transcript_cache_path(file_hash: str) -> Path:
    return config.CACHE_DIR / (file_hash + ".txt")


async def transcribe_document(doc_key: str, pdf_path: Path,
                              sem: asyncio.Semaphore,
                              use_cache: bool = True) -> tuple[str, Optional[str]]:
    """Transcribe one document to plain text for the search index. Returns
    (text, error); text is "" on failure."""
    try:
        pdf_bytes = pdf_path.read_bytes()
    except Exception as exc:
        return "", f"cannot read file: {exc}"
    if not pdf_bytes:
        return "", "empty file"
    if len(pdf_bytes) > config.INLINE_MAX_BYTES:
        return "", "file exceeds inline size limit"

    file_hash = hashlib.sha256(pdf_bytes).hexdigest()
    if use_cache:
        p = _transcript_cache_path(file_hash)
        if p.exists():
            try:
                return p.read_text(encoding="utf-8"), None
            except Exception:
                pass

    if not GEMINI_AVAILABLE:
        return "", f"Gemini unavailable ({_GEMINI_INIT_ERROR})"

    last_err = "unknown error"
    async with sem:
        for attempt in range(config.GEMINI_MAX_RETRIES + 1):
            try:
                text = await asyncio.wait_for(
                    asyncio.to_thread(transcribe_sync, pdf_bytes),
                    timeout=config.GEMINI_TIMEOUT_S,
                )
                try:
                    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    _transcript_cache_path(file_hash).write_text(text, encoding="utf-8")
                except Exception:
                    pass
                return text, None
            except asyncio.TimeoutError:
                last_err = f"timeout after {config.GEMINI_TIMEOUT_S}s"
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            if attempt < config.GEMINI_MAX_RETRIES:
                await asyncio.sleep(1.5 * (attempt + 1))
    return "", last_err
