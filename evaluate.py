"""
Case evaluator -- composes checklist + extractions + reconciliation + recorded
decisions into the structure the UI/API consumes.

Status derivation is centralised here so it can be reviewed in one place.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import config
import store
from checklist import CHECKLIST, ChecklistItem, EvalMode
from extraction import DocumentExtraction, ExtractedField, extract_documents
from models import VerificationStatus, allowed_actions_for
from reconciliation import RECON_RULES, ReconResult, parse_amount


# --- Document discovery ------------------------------------------------------
def _canonical_key(stem: str) -> Optional[str]:
    norm = re.sub(r"[^a-z0-9]", "", stem.lower())
    if norm in config.FILENAME_ALIASES:
        return config.FILENAME_ALIASES[norm]
    if norm in config.DOC_KEYS:
        return norm
    return None


def discover_documents(case_id: str) -> tuple[dict[str, Path], list[str]]:
    """Return (present doc_key -> path, list of unrecognised filenames)."""
    case_dir = config.DATA_DIR / case_id
    present: dict[str, Path] = {}
    unknown: list[str] = []
    if not case_dir.is_dir():
        return present, unknown
    for p in sorted(case_dir.iterdir()):
        if not p.is_file() or p.suffix.lower() != ".pdf":
            continue
        key = _canonical_key(p.stem)
        if key is None:
            unknown.append(p.name)
        elif key not in present:  # first match wins; ignore duplicates
            present[key] = p
    return present, unknown


def list_cases() -> list[str]:
    if not config.DATA_DIR.is_dir():
        return []
    return sorted(p.name for p in config.DATA_DIR.iterdir() if p.is_dir())


# --- AUTO_DOC rules ----------------------------------------------------------
# Each returns (status, finding, confidence) given the document extraction.
def _truthy(ef: ExtractedField) -> bool:
    v = ef.value
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "yes", "y", "present", "1")


def _rule_present(de: DocumentExtraction):
    return (VerificationStatus.VERIFIED, "Document present.", "high")


def _rule_fi_residence(de):
    ef = de.get("residence_verdict")
    if ef.value is None:
        return (VerificationStatus.MANUAL_REVIEW, "Residence verdict not found; verify.", "low")
    if "negative" in str(ef.value).lower():
        return (VerificationStatus.EXCEPTION, f"Residence FI: {ef.value}.", "high")
    return (VerificationStatus.VERIFIED, f"Residence FI: {ef.value}.", ef.confidence)


def _rule_fi_office(de):
    ef = de.get("office_verdict")
    if ef.value is None:
        return (VerificationStatus.MANUAL_REVIEW, "Office verdict not found; verify.", "low")
    if "negative" in str(ef.value).lower():
        return (VerificationStatus.EXCEPTION, f"Office FI: {ef.value}.", "high")
    return (VerificationStatus.VERIFIED, f"Office FI: {ef.value}.", ef.confidence)


def _rule_rcu_clear(de):
    ef = de.get("verdict")
    if ef.value is None:
        return (VerificationStatus.MANUAL_REVIEW, "RCU verdict not found; verify.", "low")
    v = str(ef.value).lower()
    if "negative" in v or "refer" in v:
        return (VerificationStatus.EXCEPTION, f"RCU verdict: {ef.value}.", "high")
    return (VerificationStatus.VERIFIED, f"RCU verdict: {ef.value}.", ef.confidence)


def _rule_legal_title(de):
    ef = de.get("title_status")
    if ef.value is None:
        return (VerificationStatus.MANUAL_REVIEW, "Title status not found; verify.", "low")
    v = str(ef.value).lower()
    if any(w in v for w in ("clear", "marketable", "mortgageable")):
        return (VerificationStatus.VERIFIED, f"Title: {ef.value}.", ef.confidence)
    return (VerificationStatus.EXCEPTION, f"Title status flagged: {ef.value}.", "medium")


def _rule_legal_no_encumbrance(de):
    ef = de.get("encumbrances")
    if ef.value is None:
        return (VerificationStatus.MANUAL_REVIEW, "Encumbrance position not found; verify.", "low")
    v = str(ef.value).strip().lower()
    clean = {"none", "nil", "no encumbrance", "no encumbrances",
             "no charges", "no charge", "clear", "no liabilities"}
    # Words that signal a carve-out or a subsisting charge even inside an
    # otherwise-negative sentence; force a human look rather than auto-clearing.
    red_flags = ("except", "mortgage", "charge", "lien", "lis pendens",
                 "attachment", "subsisting", "pending")
    if v in clean and not any(w in v for w in red_flags):
        return (VerificationStatus.VERIFIED, "No encumbrance reported.", ef.confidence)
    if (v.startswith(("none", "nil", "no encumbrance")) and len(v) <= 40
            and not any(w in v for w in red_flags)):
        return (VerificationStatus.VERIFIED, "No encumbrance reported.", ef.confidence)
    return (VerificationStatus.EXCEPTION, f"Encumbrance position to review: {ef.value}.", "medium")


def _rule_value_present(de):
    ef = de.get("market_value")
    if parse_amount(ef.value) is None:
        return (VerificationStatus.MANUAL_REVIEW, "Valuation not parseable; verify.", "low")
    return (VerificationStatus.VERIFIED, f"Valuation stated: {ef.value}.", ef.confidence)


def _rule_enduse_present(de):
    ef = de.get("declared_end_use")
    if ef.value is None:
        return (VerificationStatus.MANUAL_REVIEW, "End-use not stated; verify.", "low")
    return (VerificationStatus.VERIFIED, f"End-use declared: {ef.value}.", ef.confidence)


def _rule_drl_present_signed(de):
    sig = de.get("borrower_signature_present")
    amt = de.get("requested_amount")
    if not _truthy(sig):
        return (VerificationStatus.EXCEPTION, "DRL present but signature not detected.", "low")
    return (VerificationStatus.VERIFIED, f"DRL signed; amount {amt.value}.", amt.confidence)


def _rule_sanction_present(de):
    amt = de.get("sanctioned_amount")
    if amt.value is None:
        return (VerificationStatus.MANUAL_REVIEW, "Sanction terms not parsed; verify.", "low")
    return (VerificationStatus.VERIFIED, f"Sanction present; amount {amt.value}.", amt.confidence)


def _rule_insurance_present(de):
    sa = de.get("sum_assured")
    if sa.value is None:
        return (VerificationStatus.MANUAL_REVIEW, "Insurance details not parsed; verify.", "low")
    return (VerificationStatus.VERIFIED, f"Insurance present; sum assured {sa.value}.", sa.confidence)


def _rule_insurance_bank_interest(de):
    ef = de.get("bank_interest_noted")
    if _truthy(ef):
        return (VerificationStatus.VERIFIED, "Lender interest noted on policy.", ef.confidence)
    return (VerificationStatus.EXCEPTION, "Lender interest not detected on policy.", "low")


AUTO_DOC_RULES = {
    "present": _rule_present,
    "fi_residence_positive": _rule_fi_residence,
    "fi_office_positive": _rule_fi_office,
    "rcu_clear": _rule_rcu_clear,
    "legal_title_clear": _rule_legal_title,
    "legal_no_encumbrance": _rule_legal_no_encumbrance,
    "technical_value_present": _rule_value_present,
    "enduse_present": _rule_enduse_present,
    "drl_present_signed": _rule_drl_present_signed,
    "sanction_present": _rule_sanction_present,
    "insurance_present": _rule_insurance_present,
    "insurance_bank_interest": _rule_insurance_bank_interest,
}


# --- Case attributes (for CONDITIONAL items) --------------------------------
def derive_attributes(ext: dict[str, DocumentExtraction]) -> dict[str, bool]:
    attrs = {"ticket_ge_5cr": False}
    de = ext.get("sanction")
    if de is not None and de.ok:
        amt = parse_amount(de.get("sanctioned_amount").value)
        if amt is not None and amt >= config.TITLE_VETTING_THRESHOLD:
            attrs["ticket_ge_5cr"] = True
    return attrs


# --- Status derivation per item ---------------------------------------------
SIGNOFF_FIELDS = {
    "legal": [("advocate_signature_present", "advocate signature"),
              ("advocate_seal_present", "advocate seal")],
    "technical": [("valuer_signature_present", "valuer signature"),
                  ("valuer_seal_present", "valuer seal")],
    "loan_agreement": [("borrower_signature_present", "borrower signature")],
    "affidavit": [("signature_present", "deponent signature"),
                  ("notarised", "notarisation"), ("stamp_present", "stamp")],
}


def _evidence_from_field(doc_key: str, ef: ExtractedField) -> list[dict]:
    if ef.page is None and not ef.snippet:
        return [{"doc_key": doc_key, "page": None, "snippet": ""}]
    return [{"doc_key": doc_key, "page": ef.page, "snippet": ef.snippet}]


def _evidence_from_recon(values: dict[str, dict]) -> list[dict]:
    out = []
    for doc_key, cell in values.items():
        # property identity nests survey/address; flatten to first with a page
        if "page" in cell:
            out.append({"doc_key": doc_key, "page": cell.get("page"),
                        "snippet": cell.get("snippet", ""),
                        "value": cell.get("display", "")})
        else:
            for sub in cell.values():
                out.append({"doc_key": doc_key, "page": sub.get("page"),
                            "snippet": sub.get("snippet", ""),
                            "value": sub.get("display", "")})
                break
    return out


def _evaluate_item(item: ChecklistItem, ext: dict[str, DocumentExtraction],
                   attrs: dict[str, bool]) -> dict:
    status = VerificationStatus.MANUAL_REVIEW
    finding = ""
    confidence = "medium"
    evidence: list[dict] = []
    extra: dict = {}

    mode = item.mode
    # Resolve CONDITIONAL into either NOT_APPLICABLE or its inner mode.
    if mode == EvalMode.CONDITIONAL:
        applies = attrs.get(item.condition, False)
        if not applies:
            return _pack(item, VerificationStatus.NOT_APPLICABLE,
                         "Conditional item does not apply to this case.",
                         "high", [], {})
        mode = item.inner_mode or EvalMode.MANUAL

    if mode == EvalMode.SYSTEM:
        status = VerificationStatus.PENDING_SYSTEM_DATA
        finding = "Requires LOS / system data (not yet available)."
        confidence = "high"

    elif mode == EvalMode.MANUAL:
        status = VerificationStatus.MANUAL_REVIEW
        finding = "Requires manual review."
        confidence = "high"

    elif mode == EvalMode.SIGNOFF:
        de = ext.get(item.source_doc)
        if de is None:
            status = VerificationStatus.DOCUMENT_MISSING
            finding = f"{item.source_doc} not in file set."
            confidence = "high"
        elif not de.ok:
            # The document is present but could not be read automatically. It
            # is not missing; a human must inspect it for the marks anyway.
            status = VerificationStatus.NEEDS_SIGNOFF
            finding = (f"Could not auto-detect signatures/seal "
                       f"({de.error}); confirm in document.")
            confidence = "low"
        else:
            parts = []
            for fname, label in SIGNOFF_FIELDS.get(item.source_doc, []):
                ef = de.get(fname)
                detected = _truthy(ef)
                parts.append(f"{label}: {'detected' if detected else 'not detected'}")
                if ef.page is not None or ef.snippet:
                    evidence.append({"doc_key": item.source_doc,
                                     "page": ef.page, "snippet": ef.snippet})
            status = VerificationStatus.NEEDS_SIGNOFF
            finding = "; ".join(parts) + " -- confirm authenticity."
            confidence = "medium"

    elif mode == EvalMode.AUTO_DOC:
        de = ext.get(item.source_doc)
        if de is None:
            status = VerificationStatus.DOCUMENT_MISSING
            finding = f"{item.source_doc} not in file set."
            confidence = "high"
        elif not de.ok:
            status = VerificationStatus.MANUAL_REVIEW
            finding = f"Could not read {item.source_doc} automatically ({de.error})."
            confidence = "low"
        else:
            rule = AUTO_DOC_RULES.get(item.rule)
            if rule is None:
                status = VerificationStatus.MANUAL_REVIEW
                finding = f"No rule implemented for '{item.rule}'; verify manually."
                confidence = "low"
            else:
                status, finding, confidence = rule(de)
                # Attach evidence from the primary field the rule cares about.
                primary = _PRIMARY_FIELD.get(item.rule)
                if primary:
                    evidence = _evidence_from_field(item.source_doc, de.get(primary))

    elif mode == EvalMode.AUTO_RECON:
        fn = RECON_RULES.get(item.rule)
        if fn is None:
            status = VerificationStatus.MANUAL_REVIEW
            finding = f"No reconciliation rule for '{item.rule}'."
            confidence = "low"
        else:
            res: ReconResult = fn(item, ext)
            status, finding, confidence = res.status, res.finding, res.confidence
            evidence = _evidence_from_recon(res.values)
            extra = dict(res.extra)
            extra["recon_values"] = res.values

    return _pack(item, status, finding, confidence, evidence, extra)


_PRIMARY_FIELD = {
    "fi_residence_positive": "residence_verdict",
    "fi_office_positive": "office_verdict",
    "rcu_clear": "verdict",
    "legal_title_clear": "title_status",
    "legal_no_encumbrance": "encumbrances",
    "technical_value_present": "market_value",
    "enduse_present": "declared_end_use",
    "drl_present_signed": "requested_amount",
    "sanction_present": "sanctioned_amount",
    "insurance_present": "sum_assured",
    "insurance_bank_interest": "bank_interest_noted",
}

# Triage ranking: lower sorts first. Exceptions, then things needing a human,
# then missing/pending, then clean, then N/A.
_RANK = {
    VerificationStatus.EXCEPTION: 0,
    VerificationStatus.NEEDS_SIGNOFF: 1,
    VerificationStatus.MANUAL_REVIEW: 1,
    VerificationStatus.DOCUMENT_MISSING: 2,
    VerificationStatus.PENDING_SYSTEM_DATA: 3,
    VerificationStatus.VERIFIED: 4,
    VerificationStatus.NOT_APPLICABLE: 5,
}


def _pack(item: ChecklistItem, status, finding, confidence, evidence, extra) -> dict:
    # Low-confidence auto results are flagged for attention even if "verified".
    needs_attention = (status in (VerificationStatus.EXCEPTION,
                                  VerificationStatus.NEEDS_SIGNOFF,
                                  VerificationStatus.MANUAL_REVIEW)
                       or (status == VerificationStatus.VERIFIED
                           and confidence == "low"))
    return {
        "id": item.id,
        "section": item.section,
        "text": item.text,
        "mode": item.mode.value,
        "source_doc": item.source_doc,
        "status": status.value,
        "finding": finding,
        "confidence": confidence,
        "needs_attention": needs_attention,
        "evidence": evidence,
        "extra": extra,
        "allowed_actions": allowed_actions_for(status),
        "rank": _RANK.get(status, 9),
        "decision": None,  # filled in by evaluate_case
    }


async def evaluate_case(case_id: str, use_cache: bool = True) -> dict:
    docs, unknown = discover_documents(case_id)
    ext = await extract_documents(docs, use_cache=use_cache) if docs else {}
    attrs = derive_attributes(ext)

    items = [_evaluate_item(it, ext, attrs) for it in CHECKLIST]

    # Merge latest decisions.
    latest = store.latest_decision_per_item(case_id)
    for it in items:
        d = latest.get(it["id"])
        if d:
            it["decision"] = d

    items.sort(key=lambda x: (x["rank"], x["id"]))

    reviewed = sum(1 for it in items if it["decision"])
    counts: dict[str, int] = {}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1

    return {
        "case_id": case_id,
        "documents_present": sorted(docs.keys()),
        "documents_unrecognised": unknown,
        "documents_missing": [k for k in config.DOC_KEYS if k not in docs],
        "extraction_errors": {k: v.error for k, v in ext.items() if not v.ok},
        "items": items,
        "summary": {
            "total": len(items),
            "reviewed": reviewed,
            "open": len(items) - reviewed,
            "status_counts": counts,
        },
    }
