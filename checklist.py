"""
Checklist configuration -- the spine of the system.

IMPORTANT / HONEST NOTE
-----------------------
The exact line-by-line wording and the precise pass/fail criteria of your A-J
sheet are NOT all reproduced here verbatim, because I do not have the full
sheet text. What follows is the *structure* with the document mapping and
evaluation mode we established, plus representative items per section. Replace
`text` with your exact wording and confirm each `rule` against your real
acceptance criteria. Items flagged `# TODO-WORDING` are placeholders for lines
whose exact phrasing you should paste in.

Each item declares HOW it is evaluated (`mode`), WHAT it reads (`source_doc`),
and WHICH deterministic rule decides pass/fail (`rule`). The evaluator
(evaluate.py) turns these into a VerificationStatus.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EvalMode(str, Enum):
    AUTO_DOC = "auto_doc"        # extract from one document + apply a rule
    AUTO_RECON = "auto_recon"    # cross-document reconciliation rule
    SIGNOFF = "signoff"          # sign/seal/notary: detect, then human authenticates
    MANUAL = "manual"            # policy-driven human-only check
    SYSTEM = "system"            # needs LOS/system data -> pending
    CONDITIONAL = "conditional"  # may be N/A depending on case attributes


@dataclass
class ChecklistItem:
    id: str
    section: str
    text: str
    mode: EvalMode
    source_doc: Optional[str] = None          # canonical doc key (AUTO_DOC, SIGNOFF)
    recon_docs: tuple[str, ...] = ()           # docs involved (AUTO_RECON)
    rule: Optional[str] = None                 # rule id dispatched in evaluate.py
    condition: Optional[str] = None            # CONDITIONAL: attribute flag to test
    inner_mode: Optional[EvalMode] = None      # CONDITIONAL: mode if it DOES apply
    inner_rule: Optional[str] = None


# ---------------------------------------------------------------------------
# The checklist. Grouped by section for readability; order here is the source
# order, not the display order (the UI re-ranks exception-first).
# ---------------------------------------------------------------------------
CHECKLIST: list[ChecklistItem] = [
    # --- A. Applicant verification ------------------------------------------
    ChecklistItem("A1", "A. Applicant", "Residence field investigation positive",
                  EvalMode.AUTO_DOC, source_doc="fi", rule="fi_residence_positive"),
    ChecklistItem("A2", "A. Applicant", "Office/business field investigation positive",
                  EvalMode.AUTO_DOC, source_doc="fi", rule="fi_office_positive"),
    ChecklistItem("A4", "A. Applicant", "RCU screening verdict clear",
                  EvalMode.AUTO_DOC, source_doc="rcu", rule="rcu_clear"),
    ChecklistItem("A5", "A. Applicant", "CIBIL / bureau report on file",  # not supplied
                  EvalMode.AUTO_DOC, source_doc="cibil", rule="present"),
    ChecklistItem("A6", "A. Applicant", "KYC documents on file",          # not supplied
                  EvalMode.AUTO_DOC, source_doc="kyc", rule="present"),

    # --- B. Legal & title ----------------------------------------------------
    ChecklistItem("B1", "B. Legal", "Title reported clear and marketable",
                  EvalMode.AUTO_DOC, source_doc="legal", rule="legal_title_clear"),
    ChecklistItem("B2", "B. Legal", "No subsisting encumbrance reported",
                  EvalMode.AUTO_DOC, source_doc="legal", rule="legal_no_encumbrance"),
    ChecklistItem("B5", "B. Legal", "Advocate signature and seal present on report",
                  EvalMode.SIGNOFF, source_doc="legal"),
    ChecklistItem("B7", "B. Legal", "Title search vetted by senior counsel (loans >= 5 Cr)",
                  EvalMode.CONDITIONAL, condition="ticket_ge_5cr",
                  inner_mode=EvalMode.MANUAL),

    # --- C. Technical / valuation -------------------------------------------
    ChecklistItem("C1", "C. Technical", "Property described and valued in technical report",
                  EvalMode.AUTO_DOC, source_doc="technical", rule="technical_value_present"),
    ChecklistItem("C2", "C. Technical", "Valuer signature and seal present",
                  EvalMode.SIGNOFF, source_doc="technical"),
    # TODO-WORDING: construction stage / approved-plan adherence lines, etc.

    # --- D. (partly document, partly system) --------------------------------
    ChecklistItem("D1", "D. Credit", "End-use of facility declared",
                  EvalMode.AUTO_DOC, source_doc="enduse", rule="enduse_present"),
    # TODO-WORDING: D lines that depend on the credit appraisal / CAM are SYSTEM.

    # --- E. Operations / disbursement docs ----------------------------------
    ChecklistItem("E1", "E. Ops", "Disbursement request letter on file and signed",
                  EvalMode.AUTO_DOC, source_doc="drl", rule="drl_present_signed"),
    ChecklistItem("E2", "E. Ops", "List of documents / foreclosure letter (BT cases)",  # not supplied
                  EvalMode.AUTO_DOC, source_doc="lod", rule="present"),
    ChecklistItem("E11", "E. Ops", "RCU report referenced in ops file",
                  EvalMode.AUTO_DOC, source_doc="rcu", rule="present"),
    ChecklistItem("E15", "E. Ops", "CERSAI charge registration evidence",  # not supplied
                  EvalMode.AUTO_DOC, source_doc="cersai", rule="present"),

    # --- F. Fees / login (system) -------------------------------------------
    ChecklistItem("F1", "F. Fees", "Processing fee per grid / login balance",
                  EvalMode.SYSTEM),

    # --- G. Sanction / insurance --------------------------------------------
    ChecklistItem("G1", "G. Sanction", "Sanction letter present with terms",
                  EvalMode.AUTO_DOC, source_doc="sanction", rule="sanction_present"),
    ChecklistItem("G2", "G. Insurance", "Property/loan insurance on file",
                  EvalMode.AUTO_DOC, source_doc="insurance", rule="insurance_present"),
    ChecklistItem("G3", "G. Insurance", "Bank's interest noted on the policy",
                  EvalMode.AUTO_DOC, source_doc="insurance", rule="insurance_bank_interest"),

    # --- H. System block (LOS / Salesforce) ---------------------------------
    ChecklistItem("H1", "H. System", "LOS application data matches documents",
                  EvalMode.SYSTEM),
    ChecklistItem("H5", "H. System", "CIBIL pulled and recorded in system",
                  EvalMode.SYSTEM),
    ChecklistItem("H6", "H. System", "CAM (credit appraisal memo) on file",  # Excel, not supplied
                  EvalMode.AUTO_DOC, source_doc="cam", rule="present"),
    # TODO-WORDING: the remaining H lines are all SYSTEM until LOS access lands.

    # --- I. Legal documentation (agreement / affidavit / end-use) -----------
    ChecklistItem("I1", "I. Docs", "Loan agreement executed and signed by borrower",
                  EvalMode.SIGNOFF, source_doc="loan_agreement"),
    ChecklistItem("I2", "I. Docs", "MITC acknowledged",  # not supplied
                  EvalMode.AUTO_DOC, source_doc="mitc", rule="present"),
    ChecklistItem("I3", "I. Docs", "Affidavit executed, notarised and stamped",
                  EvalMode.SIGNOFF, source_doc="affidavit"),

    # --- J. Deviations (system) ---------------------------------------------
    ChecklistItem("J1", "J. Deviations", "Deviations approved at correct authority",
                  EvalMode.SYSTEM),

    # --- Cross-document reconciliation (no system data required) ------------
    ChecklistItem("R1", "R. Reconciliation", "Borrower name consistent across documents",
                  EvalMode.AUTO_RECON,
                  recon_docs=("sanction", "legal", "technical", "affidavit",
                              "insurance", "loan_agreement", "drl", "fi", "rcu", "enduse"),
                  rule="recon_borrower_name"),
    ChecklistItem("R2", "R. Reconciliation", "Property identity consistent (address / survey no.)",
                  EvalMode.AUTO_RECON, recon_docs=("technical", "legal", "insurance"),
                  rule="recon_property_identity"),
    ChecklistItem("R3", "R. Reconciliation", "Sanctioned amount matches disbursement request",
                  EvalMode.AUTO_RECON, recon_docs=("sanction", "drl"),
                  rule="recon_sanctioned_amount"),
    ChecklistItem("R4", "R. Reconciliation", "Loan-to-value within bounds (valuation vs loan)",
                  EvalMode.AUTO_RECON, recon_docs=("technical", "sanction"),
                  rule="recon_ltv"),
    ChecklistItem("R5", "R. Reconciliation", "Insurance sum assured adequate vs loan",
                  EvalMode.AUTO_RECON, recon_docs=("insurance", "sanction"),
                  rule="recon_insurance_adequacy"),
    ChecklistItem("R6", "R. Reconciliation", "Sanction conditions (OTC/PDD) tracked to evidence",
                  EvalMode.AUTO_RECON, recon_docs=("sanction",),
                  rule="recon_conditions"),
]


def by_id() -> dict[str, ChecklistItem]:
    return {it.id: it for it in CHECKLIST}
