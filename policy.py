"""
Policy / pricing compliance against the lender's published grid.

This encodes the L&T Finance "Mortgage Pricing Grid (HL & LAP, w.e.f. Apr'26)"
so the sanctioned terms in a loan file can be checked against the actual credit
policy instead of a guessed heuristic. Like reconciliation.py this is pure,
auditable Python over already-extracted fields -- no model calls -- and it is
deliberately conservative: when a figure cannot be parsed, or the exact grid
cell cannot be pinned without data we do not have (CIBIL band, salaried-vs-SEP
profile, channel), the rule does NOT silently pass. It states the published
window, does the arithmetic it can, and routes anything ambiguous to a human.

Every result carries a `calculation` (the steps + the formula + the verdict) and
`references` (verbatim policy clauses) so the reviewer sees the reasoning and the
proof, not just a colour. Grid figures below are transcribed from the policy PDF
in /Policy; the source clause for each check is quoted in REFERENCES.
"""
from __future__ import annotations

from typing import Optional

import config
from models import VerificationStatus
from reconciliation import ReconResult, _cell, fmt_inr, parse_amount

# ---------------------------------------------------------------------------
# Loan-amount slabs (rupees). The grid columns are these four bands.
# ---------------------------------------------------------------------------
HL_SLABS: list[tuple[str, float, float]] = [
    ("0-50L",     0,            50_00_000),
    ("50-100L",   50_00_001,    1_00_00_000),
    ("100-150L",  1_00_00_001,  1_50_00_000),
    (">150L",     1_50_00_001,  float("inf")),
]

CIBIL_BANDS = ["650-699", "700-749 & NTC", "750-799", "800 & above"]

# HL ROI grids: profile -> CIBIL band -> rate per slab (in HL_SLABS order).
HL_ROI_NORMAL: dict[str, dict[str, list[float]]] = {
    "Salaried": {
        "650-699":       [8.90, 8.80, 8.65, 8.60],
        "700-749 & NTC": [8.35, 8.30, 8.15, 8.10],
        "750-799":       [8.20, 8.15, 8.00, 7.95],
        "800 & above":   [7.95, 7.80, 7.80, 7.75],
    },
    "SEP/SENP": {
        "650-699":       [9.20, 9.10, 8.95, 8.90],
        "700-749 & NTC": [8.65, 8.60, 8.45, 8.40],
        "750-799":       [8.50, 8.45, 8.30, 8.25],
        "800 & above":   [8.30, 8.25, 8.25, 8.20],
    },
}
HL_ROI_SURROGATE: dict[str, dict[str, list[float]]] = {
    "Salaried": {
        "650-699":       [9.15, 9.05, 8.90, 8.85],
        "700-749 & NTC": [8.60, 8.55, 8.40, 8.35],
        "750-799":       [8.45, 8.40, 8.25, 8.20],
        "800 & above":   [8.25, 8.20, 8.20, 8.15],
    },
    "SEP/SENP": {
        "650-699":       [9.30, 9.20, 9.05, 9.00],
        "700-749 & NTC": [8.75, 8.70, 8.55, 8.50],
        "750-799":       [8.60, 8.55, 8.40, 8.35],
        "800 & above":   [8.40, 8.35, 8.35, 8.30],
    },
}

# Absolute deviation floors (per the policy's deviation matrix + notes).
ROI_FLOOR_STANDARD = 7.75   # note 16: rates below this (except DLOD) need CE
ROI_FLOOR_BH_SALARIED = 7.50
ROI_FLOOR_BH_SENP = 7.65

# Processing-fee rules (Prime channel).
PF_HL_SALARIED_FLAT = 10_000.0     # Rs. 10,000 + GST
PF_HL_SELF_PCT = 0.50              # 0.50% + GST
PF_LAP_PCT = 1.00                 # 1% + GST
PF_MAX_PCT = 1.25                 # Mortgage Plus LAP upper bound

LOGIN_FEE_CAP = 1_000.0           # note 11: up to Rs. 1000 for HL & LAP

REFERENCES = {
    "grid": ("L&T Finance — Mortgage Pricing Grid (HL & LAP), w.e.f. Apr'26: "
             "Home Loan ROI by CIBIL band and loan amount."),
    "floor": ("Pricing notes (16): “Any other deviation and rates below 7.75% "
              "(except DLOD…) can be approved by CE.” BH may go to 7.50% "
              "(Salaried) / 7.65% (SEP & SENP)."),
    "pf_salaried": "Processing Fees (Prime): HL Salaried — Rs. 10,000 + GST.",
    "pf_self": "Processing Fees (Prime): HL Self Employed — 0.50% + GST (LAP 1% + GST).",
    "login": ("Pricing notes (11): “Login fee of upto Rs. 1000 will be charged "
              "for HL & LAP. Any waiver is with ZSM.”"),
}


# ---------------------------------------------------------------------------
# Small parsing/formatting helpers.
# ---------------------------------------------------------------------------
def parse_pct(value) -> Optional[float]:
    """Parse a percentage like '8.35%', '8.35 % p.a.', 'ROI 8.35'. Returns the
    number (8.35), or None if no plausible rate is present."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if 0 < v < 100 else None
    import re
    m = re.search(r"\d{1,2}(?:\.\d+)?", str(value))
    if not m:
        return None
    try:
        v = float(m.group(0))
    except ValueError:
        return None
    return v if 0 < v < 100 else None


def fmt_pct(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:g}%"


def loan_slab(amount: Optional[float]) -> Optional[int]:
    if amount is None:
        return None
    for i, (_, lo, hi) in enumerate(HL_SLABS):
        if lo <= amount <= hi:
            return i
    return None


def _slab_window(grid: dict[str, dict[str, list[float]]], slab: int,
                 profile: str) -> tuple[float, float]:
    rates = [grid[profile][b][slab] for b in CIBIL_BANDS]
    return min(rates), max(rates)


def _grid_rows(slab: int) -> list[list[str]]:
    """Per-CIBIL-band rows (normal grid) for the resolved slab, both profiles,
    so the reviewer can pin the exact cell once the CIBIL band is known."""
    rows = []
    for b in CIBIL_BANDS:
        rows.append([b,
                     fmt_pct(HL_ROI_NORMAL["Salaried"][b][slab]),
                     fmt_pct(HL_ROI_NORMAL["SEP/SENP"][b][slab])])
    return rows


# ---------------------------------------------------------------------------
# Rules. Same signature as reconciliation rules: fn(item, ext) -> ReconResult.
# ---------------------------------------------------------------------------
def policy_roi(item, ext) -> ReconResult:
    de = ext.get("sanction")
    if de is None or not de.ok:
        return ReconResult(VerificationStatus.DOCUMENT_MISSING,
                           "Sanction letter not available to check the rate.",
                           confidence="low")
    roi_ef = de.get("roi")
    amt_ef = de.get("sanctioned_amount")
    values = {"sanction": _cell(roi_ef)}
    roi = parse_pct(roi_ef.value)
    amount = parse_amount(amt_ef.value)
    slab = loan_slab(amount)

    steps = []
    if amount is not None:
        slab_label = HL_SLABS[slab][0] if slab is not None else "—"
        steps.append({"label": "Sanctioned amount", "value": fmt_inr(amount)
                      + f"  (slab {slab_label})", "doc": amt_ef.source or "sanction",
                      "page": amt_ef.page})
    steps.append({"label": "Sanctioned interest rate",
                  "value": fmt_pct(roi) if roi is not None else "not read",
                  "doc": roi_ef.source or "sanction", "page": roi_ef.page})

    refs = [REFERENCES["grid"], REFERENCES["floor"]]

    # Cannot pin the slab/rate -> show the policy and route to a human.
    if roi is None or slab is None:
        calc = {"title": "Interest rate vs published grid", "steps": steps,
                "result": "Cannot compute automatically.",
                "verdict": ("Rate or amount not parsed from the sanction letter; "
                            "confirm against the grid below."),
                "references": refs}
        if slab is not None:
            calc["grid"] = {"slab": HL_SLABS[slab][0],
                            "columns": ["CIBIL band", "Salaried", "SEP/SENP"],
                            "rows": _grid_rows(slab)}
        return ReconResult(VerificationStatus.MANUAL_REVIEW,
                           "Interest rate not auto-verifiable; confirm against the grid.",
                           confidence="low", values=values,
                           extra={"calculation": calc})

    sal_lo, sal_hi = _slab_window(HL_ROI_NORMAL, slab, "Salaried")
    sep_lo, sep_hi = _slab_window(HL_ROI_NORMAL, slab, "SEP/SENP")
    sur_hi = max(_slab_window(HL_ROI_SURROGATE, slab, "Salaried")[1],
                 _slab_window(HL_ROI_SURROGATE, slab, "SEP/SENP")[1])
    grid_max = max(sal_hi, sep_hi, sur_hi)

    window = (f"Salaried {fmt_pct(sal_lo)}–{fmt_pct(sal_hi)}, "
              f"SEP/SENP {fmt_pct(sep_lo)}–{fmt_pct(sep_hi)} "
              f"(standard grid); floor {fmt_pct(ROI_FLOOR_STANDARD)}.")

    if roi < ROI_FLOOR_STANDARD:
        status = VerificationStatus.EXCEPTION
        verdict = (f"{fmt_pct(roi)} is below the {fmt_pct(ROI_FLOOR_STANDARD)} "
                   f"floor — permissible only via documented BH/CE deviation; "
                   f"confirm the approval is on file.")
        conf = "medium"
    elif roi <= grid_max:
        status = VerificationStatus.VERIFIED
        verdict = (f"{fmt_pct(roi)} sits within the published HL window for this "
                   f"slab. Confirm the CIBIL band & profile to pin the exact cell.")
        conf = "low"
    else:
        status = VerificationStatus.MANUAL_REVIEW
        verdict = (f"{fmt_pct(roi)} is above the standard HL grid for this slab "
                   f"(max {fmt_pct(grid_max)}); confirm the product/program "
                   f"(Mortgage Plus / special).")
        conf = "low"

    calc = {"title": "Interest rate vs published grid", "steps": steps,
            "result": f"Sanctioned {fmt_pct(roi)} vs window: {window}",
            "verdict": verdict, "references": refs,
            "grid": {"slab": HL_SLABS[slab][0],
                     "columns": ["CIBIL band", "Salaried", "SEP/SENP"],
                     "rows": _grid_rows(slab)}}
    finding = (f"Sanctioned rate {fmt_pct(roi)}; standard HL grid for "
               f"{HL_SLABS[slab][0]} is {window}")
    return ReconResult(status, finding, confidence=conf, values=values,
                       extra={"calculation": calc})


def policy_fees(item, ext) -> ReconResult:
    de = ext.get("sanction")
    if de is None or not de.ok:
        return ReconResult(VerificationStatus.DOCUMENT_MISSING,
                           "Sanction letter not available to check fees.",
                           confidence="low")
    pf_ef = de.get("processing_fee")
    login_ef = de.get("login_fee")
    amt_ef = de.get("sanctioned_amount")
    amount = parse_amount(amt_ef.value)
    values = {"processing_fee": _cell(pf_ef), "login_fee": _cell(login_ef)}

    steps = []
    refs = [REFERENCES["pf_salaried"], REFERENCES["pf_self"], REFERENCES["login"]]
    issues: list[str] = []
    ok_parts: list[str] = []

    # --- processing fee -----------------------------------------------------
    pf_raw = pf_ef.value
    pf_pct = pf_amt = None
    if pf_raw is not None and "%" in str(pf_raw):
        pf_pct = parse_pct(pf_raw)
        if pf_pct is not None and amount:
            pf_amt = pf_pct / 100.0 * amount
    elif pf_raw is not None:
        pf_amt = parse_amount(pf_raw)
        if pf_amt is not None and amount:
            pf_pct = pf_amt / amount * 100.0

    if pf_raw is None:
        steps.append({"label": "Processing fee", "value": "not stated",
                      "doc": pf_ef.source or "sanction", "page": pf_ef.page})
    else:
        disp = str(pf_raw)
        if pf_amt is not None:
            disp = fmt_inr(pf_amt)
            if pf_pct is not None:
                disp += f"  ({pf_pct:.2f}% of loan)"
        steps.append({"label": "Processing fee charged", "value": disp,
                      "doc": pf_ef.source or "sanction", "page": pf_ef.page})
        # Salaried flat 10k, or self-employed/LAP percentage band 0.50%-1.25%.
        near_flat = pf_amt is not None and abs(pf_amt - PF_HL_SALARIED_FLAT) <= 100
        in_pct_band = pf_pct is not None and PF_HL_SELF_PCT - 0.01 <= pf_pct <= PF_MAX_PCT + 0.01
        flat_as_pct_ok = (pf_amt is not None and amount
                          and pf_amt <= PF_HL_SALARIED_FLAT + 100)
        if near_flat:
            ok_parts.append("PF matches HL Salaried (Rs. 10,000 + GST)")
        elif in_pct_band or flat_as_pct_ok:
            ok_parts.append("PF within the published HL/LAP range "
                            "(Rs. 10,000 / 0.50%–1.25%)")
        else:
            issues.append("processing fee is outside the published grid "
                          "(HL Salaried Rs. 10,000, Self-Employed 0.50%, LAP 1%)")

    # --- login fee ----------------------------------------------------------
    login_amt = parse_amount(login_ef.value) if login_ef.value is not None else None
    if login_ef.value is None:
        steps.append({"label": "Login fee", "value": "not stated separately",
                      "doc": login_ef.source or "sanction", "page": login_ef.page})
    else:
        steps.append({"label": "Login fee charged", "value": fmt_inr(login_amt)
                      if login_amt is not None else str(login_ef.value),
                      "doc": login_ef.source or "sanction", "page": login_ef.page})
        if login_amt is not None and login_amt > LOGIN_FEE_CAP:
            issues.append(f"login fee {fmt_inr(login_amt)} exceeds the "
                          f"{fmt_inr(LOGIN_FEE_CAP)} cap (waiver is with ZSM)")
        elif login_amt is not None:
            ok_parts.append(f"login fee within the {fmt_inr(LOGIN_FEE_CAP)} cap")

    # --- verdict ------------------------------------------------------------
    if pf_raw is None and login_ef.value is None:
        status = VerificationStatus.MANUAL_REVIEW
        verdict = ("Fee details not found in the sanction letter; verify against "
                   "the pricing grid.")
        finding = "Processing/login fee not parsed from sanction; verify."
        conf = "low"
    elif issues:
        status = VerificationStatus.EXCEPTION
        verdict = "Outside policy: " + "; ".join(issues) + "."
        finding = verdict
        conf = "medium"
    else:
        status = VerificationStatus.VERIFIED
        verdict = (("; ".join(ok_parts) + ". Confirm profile/channel.")
                   if ok_parts else "Within the published fee policy.")
        finding = "Fees consistent with the published pricing grid."
        conf = "low"

    calc = {"title": "Fees vs pricing policy", "steps": steps,
            "result": ("HL Salaried PF Rs. 10,000 + GST · Self-Employed 0.50% "
                       "· LAP 1% · login fee cap "
                       + fmt_inr(LOGIN_FEE_CAP)),
            "verdict": verdict, "references": refs}
    return ReconResult(status, finding, confidence=conf, values=values,
                       extra={"calculation": calc})


POLICY_RULES = {
    "policy_roi": policy_roi,
    "policy_fees": policy_fees,
}
