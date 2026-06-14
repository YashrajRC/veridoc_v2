"""
Deterministic reconciliation across documents.

This is the auditable core: pure Python over already-extracted fields, no model
calls. Design bias: when a comparison is uncertain (a value could not be parsed,
or a required document is absent), the rule does NOT silently pass and does NOT
fabricate a mismatch -- it returns a status that routes the line to a human.
Auto-pass is reserved for cases that are unambiguously consistent.

Returns a ReconResult carrying the per-document values (with their page/snippet)
so the UI can show the side-by-side comparison and link each value to source.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import config
from extraction import DocumentExtraction, ExtractedField
from models import VerificationStatus


@dataclass
class ReconResult:
    status: VerificationStatus
    finding: str
    confidence: str = "medium"
    # doc_key -> {"value", "display", "page", "snippet"}
    values: dict[str, dict] = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


# --- Normalisation ----------------------------------------------------------
_HONORIFICS = {"MR", "MRS", "MS", "M/S", "MS.", "SHRI", "SMT", "DR", "KUMARI",
               "SRI", "S/O", "S/O", "D/O", "W/O", "C/O"}


def norm_name(value) -> Optional[str]:
    if value is None:
        return None
    s = str(value).upper()
    # Cut relationship clauses; everything before S/o, D/o, W/o is the name.
    s = re.split(r"\bS/?O\b|\bD/?O\b|\bW/?O\b|\bC/?O\b", s)[0]
    s = re.sub(r"[^A-Z ]", " ", s)
    tokens = [t for t in s.split() if t and t not in _HONORIFICS]
    if not tokens:
        return None
    return " ".join(tokens)


def names_match(a, b) -> bool:
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return False
    return na == nb  # strict: any divergence is surfaced for human review


_AMOUNT_MULTIPLIERS = [
    (re.compile(r"\bCRORES?\b|\bCR\b", re.I), 1_00_00_000),
    (re.compile(r"\bLAKHS?\b|\bLACS?\b|\bLAC\b", re.I), 1_00_000),
]


def parse_amount(value) -> Optional[float]:
    """Parse an Indian-format monetary string. Returns None if it cannot be
    parsed confidently (e.g. amount written only in words)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value)
    multiplier = 1
    for rx, m in _AMOUNT_MULTIPLIERS:
        if rx.search(s):
            multiplier = m
            break
    # Pull the first numeric group (handles 45,00,000 / 4500000 / 45.00).
    m = re.search(r"\d[\d,]*\.?\d*", s)
    if not m:
        return None
    num = m.group(0).replace(",", "")
    try:
        num_val = float(num)
    except ValueError:
        return None
    # If a unit word (lakh/crore) is present but the figure is already at or
    # above that scale, the words are a restatement of the same amount, not a
    # multiplier. e.g. "Rs. 45,00,000 (Forty Five Lakhs Only)" -> 4500000, not
    # 4500000 * 100000. Only apply the multiplier to a small leading figure
    # ("45 lakh", "4.5 Cr").
    if multiplier > 1 and num_val >= multiplier:
        multiplier = 1
    return num_val * multiplier


def amounts_match(a, b, tol: float = 1.0) -> Optional[bool]:
    pa, pb = parse_amount(a), parse_amount(b)
    if pa is None or pb is None:
        return None  # not comparable
    return abs(pa - pb) <= tol


def _norm_tokens(value) -> set[str]:
    if value is None:
        return set()
    s = re.sub(r"[^a-z0-9 ]", " ", str(value).lower())
    return {t for t in s.split() if len(t) > 1}


def address_similarity(a, b) -> Optional[float]:
    ta, tb = _norm_tokens(a), _norm_tokens(b)
    if not ta or not tb:
        return None
    return len(ta & tb) / len(ta | tb)


def norm_survey(value) -> Optional[str]:
    if value is None:
        return None
    s = re.sub(r"[^a-z0-9]", "", str(value).lower())
    return s or None


def _cell(ef: ExtractedField) -> dict:
    return {"value": ef.value,
            "display": "" if ef.value is None else str(ef.value),
            "page": ef.page, "snippet": ef.snippet,
            "doc_id": ef.source}   # which physical PDF this value came from


def _present(ext: dict[str, DocumentExtraction], doc_key: str) -> bool:
    de = ext.get(doc_key)
    return de is not None and de.ok


# --- Rules ------------------------------------------------------------------
def recon_borrower_name(item, ext: dict[str, DocumentExtraction]) -> ReconResult:
    name_field_by_doc = {
        "sanction": "applicant_name", "legal": "applicant_name",
        "technical": "applicant_name", "affidavit": "deponent_name",
        "insurance": "insured_name", "loan_agreement": "borrower_name",
        "drl": "applicant_name", "fi": "applicant_name", "rcu": "applicant_name",
        "enduse": "applicant_name",
    }
    values: dict[str, dict] = {}
    present_names: list[tuple[str, object]] = []
    for doc_key in item.recon_docs:
        de = ext.get(doc_key)
        if de is None or not de.ok:
            continue
        ef = de.get(name_field_by_doc.get(doc_key, "applicant_name"))
        if ef.value is None:
            continue
        values[doc_key] = _cell(ef)
        present_names.append((doc_key, ef.value))

    if len(present_names) < 2:
        return ReconResult(
            VerificationStatus.MANUAL_REVIEW,
            "Name found in fewer than two documents; verify manually.",
            confidence="low", values=values)

    base_doc, base_val = present_names[0]
    mismatches = [d for d, v in present_names[1:] if not names_match(base_val, v)]
    if mismatches:
        return ReconResult(
            VerificationStatus.EXCEPTION,
            f"Borrower name differs across documents ({', '.join(mismatches)} "
            f"vs {base_doc}).",
            confidence="high", values=values)
    return ReconResult(
        VerificationStatus.VERIFIED,
        f"Borrower name consistent across {len(present_names)} documents.",
        confidence="high", values=values)


def recon_property_identity(item, ext) -> ReconResult:
    docs = [d for d in item.recon_docs if _present(ext, d)]
    values = {}
    surveys, addresses = {}, {}
    for d in docs:
        de = ext[d]
        sv = de.get("survey_or_plot_no")
        ad = de.get("property_address")
        values[d] = {"survey": _cell(sv), "address": _cell(ad)}
        if sv.value is not None:
            surveys[d] = norm_survey(sv.value)
        if ad.value is not None:
            addresses[d] = ad.value

    # Strong signal: survey/plot numbers.
    distinct_surveys = {v for v in surveys.values() if v}
    if len(surveys) >= 2 and len(distinct_surveys) > 1:
        return ReconResult(VerificationStatus.EXCEPTION,
                           "Survey/plot number differs across documents.",
                           confidence="high", values=values)
    if len(surveys) >= 2 and len(distinct_surveys) == 1:
        return ReconResult(VerificationStatus.VERIFIED,
                           "Survey/plot number matches across documents.",
                           confidence="high", values=values)

    # Fall back to address token overlap.
    addr_docs = list(addresses.items())
    if len(addr_docs) >= 2:
        lowest = 1.0
        for i in range(len(addr_docs)):
            for j in range(i + 1, len(addr_docs)):
                sim = address_similarity(addr_docs[i][1], addr_docs[j][1])
                if sim is not None:
                    lowest = min(lowest, sim)
        if lowest < 0.6:
            return ReconResult(
                VerificationStatus.EXCEPTION,
                "Property address differs materially across documents; verify.",
                confidence="low", values=values)
        return ReconResult(
            VerificationStatus.VERIFIED,
            "Property address broadly consistent (heuristic); confirm survey no.",
            confidence="low", values=values)

    return ReconResult(VerificationStatus.MANUAL_REVIEW,
                       "Insufficient property data to reconcile; verify manually.",
                       confidence="low", values=values)


def recon_sanctioned_amount(item, ext) -> ReconResult:
    if not _present(ext, "sanction") or not _present(ext, "drl"):
        missing = [d for d in ("sanction", "drl") if not _present(ext, d)]
        return ReconResult(VerificationStatus.DOCUMENT_MISSING,
                           f"Cannot reconcile; missing: {', '.join(missing)}.",
                           confidence="low")
    s = ext["sanction"].get("sanctioned_amount")
    d = ext["drl"].get("requested_amount")
    values = {"sanction": _cell(s), "drl": _cell(d)}
    res = amounts_match(s.value, d.value)
    if res is None:
        return ReconResult(VerificationStatus.MANUAL_REVIEW,
                           "Amount could not be parsed from one side; verify.",
                           confidence="low", values=values)
    if res:
        return ReconResult(VerificationStatus.VERIFIED,
                           "Sanctioned amount matches disbursement request.",
                           confidence="high", values=values)
    return ReconResult(VerificationStatus.EXCEPTION,
                       "Sanctioned amount does not match disbursement request.",
                       confidence="high", values=values)


def recon_ltv(item, ext) -> ReconResult:
    if not _present(ext, "technical") or not _present(ext, "sanction"):
        missing = [d for d in ("technical", "sanction") if not _present(ext, d)]
        return ReconResult(VerificationStatus.DOCUMENT_MISSING,
                           f"Cannot compute LTV; missing: {', '.join(missing)}.",
                           confidence="low")
    mv = ext["technical"].get("market_value")
    sa = ext["sanction"].get("sanctioned_amount")
    values = {"technical": _cell(mv), "sanction": _cell(sa)}
    value, loan = parse_amount(mv.value), parse_amount(sa.value)
    if not value or not loan or value <= 0:
        return ReconResult(VerificationStatus.MANUAL_REVIEW,
                           "Valuation or loan amount unparseable; verify LTV manually.",
                           confidence="low", values=values)
    ltv = loan / value
    pct = round(ltv * 100, 1)
    # We do NOT have the policy cap (lives in LOS), so flag only an obviously
    # high LTV for review and state the limitation plainly.
    if ltv > 0.90:
        return ReconResult(
            VerificationStatus.EXCEPTION,
            f"LTV {pct}% looks high; confirm against policy cap (cap not "
            f"available without LOS).",
            confidence="medium", values=values, extra={"ltv_pct": pct})
    return ReconResult(
        VerificationStatus.VERIFIED,
        f"LTV {pct}% computed; policy cap to be confirmed from LOS.",
        confidence="medium", values=values, extra={"ltv_pct": pct})


def recon_insurance_adequacy(item, ext) -> ReconResult:
    if not _present(ext, "insurance") or not _present(ext, "sanction"):
        missing = [d for d in ("insurance", "sanction") if not _present(ext, d)]
        return ReconResult(VerificationStatus.DOCUMENT_MISSING,
                           f"Cannot assess adequacy; missing: {', '.join(missing)}.",
                           confidence="low")
    sa = ext["insurance"].get("sum_assured")
    loan_f = ext["sanction"].get("sanctioned_amount")
    values = {"insurance": _cell(sa), "sanction": _cell(loan_f)}
    sum_assured, loan = parse_amount(sa.value), parse_amount(loan_f.value)
    if sum_assured is None or loan is None:
        return ReconResult(VerificationStatus.MANUAL_REVIEW,
                           "Sum assured or loan amount unparseable; verify manually.",
                           confidence="low", values=values)
    if sum_assured >= loan:
        return ReconResult(VerificationStatus.VERIFIED,
                           "Insurance sum assured is at least the loan amount.",
                           confidence="medium", values=values)
    return ReconResult(VerificationStatus.EXCEPTION,
                       "Insurance sum assured is below the loan amount.",
                       confidence="medium", values=values)


def recon_conditions(item, ext) -> ReconResult:
    """Track sanction conditions and split OTC vs PDD. Mapping a condition to
    evidence is heuristic (keyword based) and always human-confirmed."""
    if not _present(ext, "sanction"):
        return ReconResult(VerificationStatus.DOCUMENT_MISSING,
                           "Sanction letter not available.", confidence="low")
    cond_field = ext["sanction"].get("conditions")
    conditions = cond_field.value if isinstance(cond_field.value, list) else []
    if not conditions:
        return ReconResult(VerificationStatus.MANUAL_REVIEW,
                           "No conditions parsed from sanction letter; verify manually.",
                           confidence="low")

    # Keyword -> evidence document presence.
    keyword_doc = {
        "insurance": "insurance", "end use": "enduse", "end-use": "enduse",
        "valuation": "technical", "technical": "technical", "legal": "legal",
        "title": "legal", "affidavit": "affidavit", "agreement": "loan_agreement",
    }
    otc_open = otc_total = pdd_total = 0
    rows = []
    for c in conditions:
        if isinstance(c, str):
            c = {"text": c, "type": "UNKNOWN"}
        if not isinstance(c, dict):
            continue
        text = str(c.get("text", "")).strip()
        ctype = str(c.get("type", "UNKNOWN")).upper()
        if ctype not in ("OTC", "PDD", "UNKNOWN"):
            ctype = "UNKNOWN"
        ltext = text.lower()
        evidence_doc = next((doc for kw, doc in keyword_doc.items()
                             if kw in ltext), None)
        satisfied = bool(evidence_doc) and _present(ext, evidence_doc)
        if ctype == "OTC":
            otc_total += 1
            if not satisfied:
                otc_open += 1
        elif ctype == "PDD":
            pdd_total += 1
        rows.append({"text": text, "type": ctype,
                     "evidence_doc": evidence_doc, "satisfied": satisfied,
                     "page": c.get("page"), "snippet": c.get("snippet", "")})

    extra = {"conditions": rows, "otc_total": otc_total,
             "otc_open": otc_open, "pdd_total": pdd_total}
    if otc_open > 0:
        return ReconResult(
            VerificationStatus.EXCEPTION,
            f"{otc_open} of {otc_total} OTC condition(s) not evidenced; review.",
            confidence="low", extra=extra)
    return ReconResult(
        VerificationStatus.MANUAL_REVIEW,
        f"{otc_total} OTC, {pdd_total} PDD condition(s) parsed; confirm mapping.",
        confidence="low", extra=extra)


RECON_RULES = {
    "recon_borrower_name": recon_borrower_name,
    "recon_property_identity": recon_property_identity,
    "recon_sanctioned_amount": recon_sanctioned_amount,
    "recon_ltv": recon_ltv,
    "recon_insurance_adequacy": recon_insurance_adequacy,
    "recon_conditions": recon_conditions,
}
