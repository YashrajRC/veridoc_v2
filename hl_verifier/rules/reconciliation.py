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

import difflib
import re
from dataclasses import dataclass, field
from typing import Optional

from hl_verifier import config
from hl_verifier.extraction import DocumentExtraction, ExtractedField
from hl_verifier.models import VerificationStatus


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


def name_tokens(value) -> set[str]:
    n = norm_name(value)
    return set(n.split()) if n else set()


def split_parties(value) -> list[str]:
    """A name field may carry several parties (applicant + co-applicant), joined
    by 'and' / '&' / ','. Return them in order as normalised names. When there is
    no separator the whole thing is one party (we still compare by token core, so
    a run-together 'A B C D' is handled by the core logic, not here)."""
    if value is None:
        return []
    parts = re.split(r"\s*(?:&|\band\b|,)\s*", str(value), flags=re.I)
    out = []
    for p in parts:
        n = norm_name(p)
        if n:
            out.append(n)
    return out


def _token_matches(token: str, token_set: set[str], thr: float) -> bool:
    """A token is present in a set if it matches exactly, as an initial (A ~
    Anil), or fuzzily (OCR/spelling variants of the same word, e.g. HAQUE ~
    HAQULL)."""
    if token in token_set:
        return True
    if len(token) == 1:
        return any(t.startswith(token) for t in token_set)
    for t in token_set:
        if len(t) == 1 and token.startswith(t):
            return True
        if abs(len(t) - len(token)) <= 2 and \
                difflib.SequenceMatcher(None, token, t).ratio() >= thr:
            return True
    return False


def core_coverage(doc_tokens: set[str], core: set[str], thr: float) -> float:
    """Fraction of the shared 'core' name covered (fuzzily) by a document."""
    if not core:
        return 1.0
    matched = sum(1 for c in core if _token_matches(c, doc_tokens, thr))
    return matched / len(core)


def names_match(a, b) -> bool:
    """Two name strings refer to the same person if one's tokens are (fuzzily)
    covered by the other's — tolerant of honorifics, OCR noise and extra
    co-applicant tokens, but still False for a genuinely different name."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return False
    thr = getattr(config, "NAME_TOKEN_FUZZ", 0.82)
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return core_coverage(big, small, thr) >= getattr(config, "NAME_CORE_COVERAGE", 0.6)


_AMOUNT_MULTIPLIERS = [
    (re.compile(r"\bCRORES?\b|\bCR\b", re.I), 1_00_00_000),
    (re.compile(r"\bLAKHS?\b|\bLACS?\b|\bLAC\b", re.I), 1_00_000),
]

# Number words for the "amount in words" parser. Indian scales (lakh/crore) are
# included alongside the international ones; sanction letters and valuation
# reports routinely write the figure only in words.
_WORD_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}
_WORD_SCALES = {
    "hundred": 100, "thousand": 1_000, "lakh": 1_00_000, "lakhs": 1_00_000,
    "lac": 1_00_000, "lacs": 1_00_000, "crore": 1_00_00_000,
    "crores": 1_00_00_000, "cr": 1_00_00_000, "million": 10_00_000,
    "billion": 1_00_00_00_000,
}


def _words_to_number(value) -> Optional[float]:
    """Convert an amount written in words ("One Crore Four Lakh ... Eighty") to a
    number. Returns None if no number words are present. Tolerant of noise words
    (Rupees, Only, and)."""
    tokens = [t for t in re.findall(r"[a-zA-Z]+", str(value).lower())
              if t in _WORD_UNITS or t in _WORD_SCALES]
    if not tokens:
        return None
    total = 0
    current = 0
    seen = False
    for t in tokens:
        if t in _WORD_UNITS:
            current += _WORD_UNITS[t]
            seen = True
        else:  # a scale word
            scale = _WORD_SCALES[t]
            seen = True
            if current == 0:
                current = 1
            if scale == 100:
                current *= 100
            else:
                total += current * scale
                current = 0
    total += current
    return float(total) if seen and total > 0 else None


def parse_amount(value) -> Optional[float]:
    """Parse an Indian-format monetary string. Falls back to an amount-in-words
    parser when no digits are present; returns None only if neither a figure nor
    number words can be read."""
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
        # No digits: the figure may be spelled out ("One Crore Four Lakh ...").
        return _words_to_number(s)
    num = m.group(0).replace(",", "")
    try:
        num_val = float(num)
    except ValueError:
        return _words_to_number(s)
    # If a unit word (lakh/crore) is present but the figure is already at or
    # above that scale, the words are a restatement of the same amount, not a
    # multiplier. e.g. "Rs. 45,00,000 (Forty Five Lakhs Only)" -> 4500000, not
    # 4500000 * 100000. Only apply the multiplier to a small leading figure
    # ("45 lakh", "4.5 Cr").
    if multiplier > 1 and num_val >= multiplier:
        multiplier = 1
    return num_val * multiplier


def fmt_inr(n) -> str:
    """Format a number with Indian digit grouping and a rupee sign:
    2600000 -> '₹26,00,000'. Used only in the derived calculation read-outs; the
    verbatim document value is always shown/linked alongside it."""
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    neg = n < 0
    whole = int(round(abs(n)))
    s = str(whole)
    if len(s) > 3:
        last3, rest = s[-3:], s[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        s = ",".join(groups) + "," + last3
    return ("-" if neg else "") + "₹" + s


def _name_similarity(a, b) -> float:
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


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


def survey_numbers(value) -> set[str]:
    """The set of plot / survey / khasra numbers in a field, ignoring the words
    around them ('Khasra No-', 'Part', 'Sy. No.'). So '613/49, 613/154 Part' and
    '613/49, 613/154' both yield {'613/49', '613/154'} and reconcile."""
    if value is None:
        return set()
    return set(re.findall(r"\d+(?:/\d+)?", str(value)))


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

    # Establish the applicant "core": name tokens that recur across documents.
    # The applicant's own name tokens appear in (nearly) every document; a
    # co-applicant's appear in fewer. A document is consistent if it covers the
    # core (fuzzily, so OCR/spelling variants of a name token still count); only
    # a document sharing little of the core is flagged as a genuine difference.
    n_docs = len(present_names)
    per_doc_tokens: dict[str, set] = {}
    token_doc_count: dict[str, int] = {}
    for d, v in present_names:
        toks = name_tokens(v)
        per_doc_tokens[d] = toks
        for t in toks:
            token_doc_count[t] = token_doc_count.get(t, 0) + 1
    # The applicant's tokens recur in (nearly) every document, so they have the
    # highest doc-frequency; a co-applicant named in only some documents has a
    # lower count. Take the most-frequent tokens (allowing one document of OCR
    # drift) as the applicant core; the rest that still recur are co-applicants.
    max_count = max(token_doc_count.values()) if token_doc_count else 0
    core_min = max(2, max_count - 1)
    core = {t for t, c in token_doc_count.items() if c >= core_min}
    extra_tokens = sorted(t for t, c in token_doc_count.items()
                          if c >= 2 and t not in core)

    if not core:
        return ReconResult(
            VerificationStatus.MANUAL_REVIEW,
            "Could not establish a consistent applicant name automatically; verify.",
            confidence="low", values=values)

    thr = getattr(config, "NAME_TOKEN_FUZZ", 0.82)
    cov_floor = getattr(config, "NAME_CORE_COVERAGE", 0.6)
    coverage = {d: core_coverage(per_doc_tokens[d], core, thr)
                for d, _ in present_names}
    diffs = [(d, v) for d, v in present_names if coverage[d] < cov_floor]
    applicant = " ".join(sorted(core))
    co_note = (f" Co-applicant name(s) also appear in some documents: "
               f"{' / '.join(extra_tokens)}." if extra_tokens else "")
    steps = [{"label": d, "value": f"{v}  ({round(coverage[d] * 100)}% of applicant name)"}
             for d, v in present_names]

    if diffs:
        calc = {
            "title": "Applicant name consistency",
            "steps": steps,
            "result": f"Applicant core: “{applicant}”.{co_note}",
            "verdict": ("Differs materially on: " + ", ".join(d for d, _ in diffs)
                        + " — these share little of the applicant name; confirm "
                        "they are the same borrower (not a misfiled document)."),
            "references": [],
        }
        return ReconResult(
            VerificationStatus.EXCEPTION,
            f"Applicant name differs on {', '.join(d for d, _ in diffs)}; verify.",
            confidence="high", values=values, extra={"calculation": calc})

    calc = {
        "title": "Applicant name consistency",
        "steps": steps,
        "result": (f"Applicant “{applicant}” reconciles across all {n_docs} "
                   f"documents (honorific / OCR / spelling variants tolerated).{co_note}"),
        "verdict": "Consistent — only minor spelling/honorific/OCR variation.",
        "references": [],
    }
    return ReconResult(
        VerificationStatus.VERIFIED,
        f"Applicant name consistent across {n_docs} documents (variants tolerated).",
        confidence="high", values=values, extra={"calculation": calc})


def recon_property_identity(item, ext) -> ReconResult:
    docs = [d for d in item.recon_docs if _present(ext, d)]
    values = {}
    survey_sets, survey_cell, addresses = {}, {}, {}
    for d in docs:
        de = ext[d]
        sv = de.get("survey_or_plot_no")
        ad = de.get("property_address")
        values[d] = {"survey": _cell(sv), "address": _cell(ad)}
        nums = survey_numbers(sv.value)
        if nums:
            survey_sets[d] = nums
            survey_cell[d] = sv
        if ad.value is not None:
            addresses[d] = ad

    # Strong signal: plot / survey / khasra numbers, compared as number sets so
    # different surrounding wording does not cause a false mismatch.
    if len(survey_sets) >= 2:
        common = set.intersection(*survey_sets.values())
        steps = [{"label": d, "value": ", ".join(sorted(s)),
                  "doc": survey_cell[d].source or d, "page": survey_cell[d].page}
                 for d, s in survey_sets.items()]
        if common:
            calc = {"title": "Property identity (survey / plot no.)", "steps": steps,
                    "result": f"Shared plot/survey number(s): {', '.join(sorted(common))}.",
                    "verdict": "Same property — survey/plot numbers reconcile.",
                    "references": []}
            return ReconResult(VerificationStatus.VERIFIED,
                               "Survey/plot number matches across documents.",
                               confidence="high", values=values,
                               extra={"calculation": calc})
        calc = {"title": "Property identity (survey / plot no.)", "steps": steps,
                "result": "No plot/survey number is common to all documents.",
                "verdict": "Survey/plot numbers differ — confirm this is one property.",
                "references": []}
        return ReconResult(VerificationStatus.EXCEPTION,
                           "Survey/plot number differs across documents.",
                           confidence="high", values=values,
                           extra={"calculation": calc})

    # Fall back to address word overlap (lenient: only a very low overlap, with
    # no survey number to lean on, is treated as a material difference).
    addr_docs = list(addresses.items())
    if len(addr_docs) >= 2:
        floor = getattr(config, "ADDRESS_SIM_FLOOR", 0.30)
        lowest = 1.0
        for i in range(len(addr_docs)):
            for j in range(i + 1, len(addr_docs)):
                sim = address_similarity(addr_docs[i][1].value, addr_docs[j][1].value)
                if sim is not None:
                    lowest = min(lowest, sim)
        steps = [{"label": d, "value": str(ef.value)[:90],
                  "doc": ef.source or d, "page": ef.page} for d, ef in addr_docs]
        calc = {"title": "Property identity (address)", "steps": steps,
                "result": f"Lowest address word-overlap between documents: {round(lowest * 100)}%.",
                "verdict": ("Addresses differ materially; confirm this is one property."
                            if lowest < floor else
                            "Addresses broadly consistent; no survey number to confirm against."),
                "references": []}
        if lowest < floor:
            return ReconResult(
                VerificationStatus.EXCEPTION,
                "Property address differs materially across documents; verify.",
                confidence="low", values=values, extra={"calculation": calc})
        return ReconResult(
            VerificationStatus.VERIFIED,
            "Property address broadly consistent (heuristic); confirm survey no.",
            confidence="low", values=values, extra={"calculation": calc})

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
    ps, pd = parse_amount(s.value), parse_amount(d.value)
    calc = {
        "title": "Sanctioned amount vs disbursement requested",
        "steps": [
            {"label": "Sanctioned (sanction letter)", "value": fmt_inr(ps),
             "doc": s.source or "sanction", "page": s.page},
            {"label": "Requested (DRL)", "value": fmt_inr(pd),
             "doc": d.source or "drl", "page": d.page},
        ],
        "result": f"{fmt_inr(ps)} vs {fmt_inr(pd)} — difference {fmt_inr(abs(ps - pd))}",
        "verdict": ("Amounts match." if res
                    else "Amounts differ; reconcile before disbursing."),
        "references": [],
    }
    if res:
        return ReconResult(VerificationStatus.VERIFIED,
                           "Sanctioned amount matches disbursement request.",
                           confidence="high", values=values,
                           extra={"calculation": calc})
    return ReconResult(VerificationStatus.EXCEPTION,
                       "Sanctioned amount does not match disbursement request.",
                       confidence="high", values=values,
                       extra={"calculation": calc})


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
    cap = getattr(config, "LTV_REVIEW_CAP", 0.90)
    cap_pct = round(cap * 100)
    calc = {
        "title": "Loan-to-value (LTV)",
        "steps": [
            {"label": "Sanctioned loan", "value": fmt_inr(loan),
             "doc": sa.source or "sanction", "page": sa.page},
            {"label": "Assessed market value", "value": fmt_inr(value),
             "doc": mv.source or "technical", "page": mv.page},
        ],
        "result": f"LTV = {fmt_inr(loan)} ÷ {fmt_inr(value)} = {pct}%",
        "verdict": (f"Above the {cap_pct}% review trigger; confirm against the "
                    f"product LTV norm." if ltv > cap
                    else f"Within the {cap_pct}% review trigger."),
        "references": [
            "HL pricing grid states no single LTV cap; the product LTV norm "
            "(COP vs market value) is applied in LOS.",
            "Policy: LAP special pricing requires LTV ≤ 70%; Industrial LAP "
            "Prime ≤ 55% (Mortgage Plus ≤ 70%).",
        ],
    }
    # The HL grid does not publish a single LTV cap, so we flag against a
    # configurable review trigger and state the basis plainly.
    if ltv > cap:
        return ReconResult(
            VerificationStatus.EXCEPTION,
            f"LTV {pct}% exceeds the {cap_pct}% review trigger; confirm against "
            f"the product cap.",
            confidence="medium", values=values,
            extra={"ltv_pct": pct, "calculation": calc})
    return ReconResult(
        VerificationStatus.VERIFIED,
        f"LTV {pct}% computed; within the {cap_pct}% review trigger (confirm "
        f"product cap).",
        confidence="medium", values=values,
        extra={"ltv_pct": pct, "calculation": calc})


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
    cover = round(sum_assured / loan * 100) if loan else 0
    calc = {
        "title": "Insurance adequacy",
        "steps": [
            {"label": "Sum assured (policy)", "value": fmt_inr(sum_assured),
             "doc": sa.source or "insurance", "page": sa.page},
            {"label": "Sanctioned loan", "value": fmt_inr(loan),
             "doc": loan_f.source or "sanction", "page": loan_f.page},
        ],
        "result": f"Sum assured {fmt_inr(sum_assured)} vs loan {fmt_inr(loan)} "
                  f"(cover {cover}% of loan)",
        "verdict": ("Sum assured is at least the loan amount." if sum_assured >= loan
                    else "Sum assured is below the loan amount; cover the shortfall."),
        "references": [],
    }
    if sum_assured >= loan:
        return ReconResult(VerificationStatus.VERIFIED,
                           "Insurance sum assured is at least the loan amount.",
                           confidence="medium", values=values,
                           extra={"calculation": calc})
    return ReconResult(VerificationStatus.EXCEPTION,
                       "Insurance sum assured is below the loan amount.",
                       confidence="medium", values=values,
                       extra={"calculation": calc})


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
