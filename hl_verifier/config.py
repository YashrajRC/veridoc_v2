"""
Central configuration.

Everything here that touches the GCP project or the on-disk layout is meant to
be confirmed/overridden via environment variables before the app is run. Nothing
in this file makes a network call.
"""
from __future__ import annotations

import os
from pathlib import Path

# The package lives in hl_verifier/; runtime artifacts (data, cache, the SQLite
# stores) live at the PROJECT ROOT (the package's parent), so moving the code
# into a package did NOT relocate your existing cases/cache. The bundled web
# console ships inside the package. All paths remain environment-overridable.
PACKAGE_DIR = Path(__file__).resolve().parent
BASE_DIR = PACKAGE_DIR.parent
WEB_DIR = PACKAGE_DIR / "web"

# --- On-disk layout ----------------------------------------------------------
# Expected: DATA_DIR/<case_id>/<document>.pdf
DATA_DIR = Path(os.environ.get("HL_DATA_DIR", str(BASE_DIR / "data")))
CACHE_DIR = Path(os.environ.get("HL_CACHE_DIR", str(BASE_DIR / "cache")))
DB_PATH = Path(os.environ.get("HL_DB_PATH", str(BASE_DIR / "review.db")))
VEC_DB_PATH = Path(os.environ.get("HL_VEC_DB_PATH", str(BASE_DIR / "vectors.db")))

# --- Vertex / Gemini (via the google-genai SDK) ------------------------------
# CONFIRM these against your project. On a Workbench instance the project is
# often inferred from the environment, but we read it explicitly so failures are
# loud rather than silent.
GCP_PROJECT = os.environ.get("GCP_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
GCP_LOCATION = os.environ.get("GCP_LOCATION")
# Generation + embedding model ids. Confirm both are enabled in your project
# (`gcloud ai models list`, or the Vertex Model Garden / console). Override via
# env if these names differ for you.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
EMBED_MODEL = os.environ.get("HL_EMBED_MODEL", "text-embedding-005")

# Bound parallel Gemini calls so we don't trip quota during the fan-out.
MAX_CONCURRENCY = int(os.environ.get("HL_MAX_CONCURRENCY", "5"))
# Per-document wall-clock budget (seconds). On timeout the document is marked
# as failed-to-read and its checklist lines fall back to manual review.
GEMINI_TIMEOUT_S = int(os.environ.get("HL_GEMINI_TIMEOUT_S", "90"))
GEMINI_MAX_RETRIES = int(os.environ.get("HL_GEMINI_MAX_RETRIES", "2"))

# Vertex inline-data has a request-size ceiling. Above this we would need to
# stage the file in GCS and pass a gs:// URI instead. That path is NOT
# implemented here; oversized files are reported as an explicit error rather
# than silently truncated.
INLINE_MAX_BYTES = int(os.environ.get("HL_INLINE_MAX_BYTES", str(18 * 1024 * 1024)))

# --- Document keys -----------------------------------------------------------
# The canonical document types we expect per case. "missing" detection is based
# on whether a file resolving to one of these keys exists in the case folder.
DOC_KEYS = [
    "technical",       # technical / valuation report
    "legal",           # legal & search report (TSR/LSR)
    "sanction",        # sanction letter
    "loan_agreement",  # loan agreement
    "insurance",       # property / loan insurance
    "drl",             # disbursement request letter
    "affidavit",       # affidavit
    "rcu",             # RCU / fraud sampling report
    "fi",              # field investigation report
    "enduse",          # end-use declaration / certificate
]

# Human-readable labels for the upload assignment dropdown.
DOC_LABELS = {
    "technical": "Technical / valuation report",
    "legal": "Legal & search report (TSR/LSR)",
    "sanction": "Sanction letter",
    "loan_agreement": "Loan agreement",
    "insurance": "Insurance policy",
    "drl": "Disbursement request letter",
    "affidavit": "Affidavit",
    "rcu": "RCU report",
    "fi": "Field investigation report",
    "enduse": "End-use declaration",
}

# Documents the checklist references that we do NOT have in this test set.
# Lines mapped to these resolve to DOCUMENT_MISSING (honest "not supplied"),
# which is distinct from PENDING_SYSTEM_DATA.
ABSENT_DOC_KEYS = ["cibil", "kyc", "cam", "lod", "mitc", "cersai"]

# Map a file stem (lowercased, non-alphanumerics stripped) to a canonical key.
# Lets you drop files named slightly differently without renaming everything.
FILENAME_ALIASES = {
    "technical": "technical", "technicalreport": "technical", "valuation": "technical",
    "valuationreport": "technical", "tech": "technical",
    "legal": "legal", "legalandsearch": "legal", "legalsearch": "legal",
    "tsr": "legal", "lsr": "legal", "searchreport": "legal", "title": "legal",
    "sanction": "sanction", "sanctionletter": "sanction", "offerletter": "sanction",
    "loanagreement": "loan_agreement", "agreement": "loan_agreement", "la": "loan_agreement",
    "insurance": "insurance", "policy": "insurance", "insurancepolicy": "insurance",
    "drl": "drl", "disbursementrequest": "drl", "disbursementrequestletter": "drl",
    "affidavit": "affidavit",
    "rcu": "rcu",
    "fi": "fi", "fieldinvestigation": "fi", "fiv": "fi",
    "enduse": "enduse", "endusecertificate": "enduse", "endusedeclaration": "enduse",
}

# Loan ticket-size threshold (in rupees) above which the title search requires
# senior/empanelled vetting per the checklist (B-section, >= 5 Cr).
TITLE_VETTING_THRESHOLD = 5_00_00_000  # 5 crore

# --- Editable policy thresholds ---------------------------------------------
# Credit-policy numbers that the deterministic rules compare against. Kept here
# (not hard-coded in the rules) so they can be tuned per product without code
# changes. The pricing GRID itself (ROI/PF bands by CIBIL & slab) lives in
# policy.py; these are the single-number triggers.
#
# The HL pricing grid publishes no single LTV cap (the product LTV norm, COP vs
# market value, is applied in LOS), so LTV is flagged against a review trigger
# rather than a hard cap. LAP special pricing in the policy requires LTV <= 70%.
LTV_REVIEW_CAP = float(os.environ.get("HL_LTV_REVIEW_CAP", "0.90"))
# Login fee cap per the pricing notes (Rs. 1000 for HL & LAP; waiver with ZSM).
LOGIN_FEE_CAP = float(os.environ.get("HL_LOGIN_FEE_CAP", "1000"))

# --- Fuzzy matching tolerances ----------------------------------------------
# Names and addresses on scanned Indian documents legitimately vary (honorifics,
# OCR noise, co-applicant ordering, "Khasra No-" prefixes). These tolerances let
# the reconciliation rules treat genuine variants of the SAME name/property as a
# match while still flagging a genuinely different one. Raise toward 1.0 for
# stricter matching, lower for more tolerance.
#
# NAME_TOKEN_FUZZ:    similarity (0-1) at which two name tokens are "the same"
#                     (e.g. HAQUE vs HAQULL ~0.83 -> same word).
# NAME_CORE_COVERAGE: fraction of the shared "core" applicant name a document
#                     must cover to be consistent; below it the name is flagged.
# ADDRESS_SIM_FLOOR:  address token-overlap below which (with no survey match)
#                     the property is flagged as materially different.
NAME_TOKEN_FUZZ = float(os.environ.get("HL_NAME_TOKEN_FUZZ", "0.82"))
NAME_CORE_COVERAGE = float(os.environ.get("HL_NAME_CORE_COVERAGE", "0.6"))
ADDRESS_SIM_FLOOR = float(os.environ.get("HL_ADDRESS_SIM_FLOOR", "0.30"))
