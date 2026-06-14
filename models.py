"""
Status model, reviewer actions, and the maker-checker decision record.

Design principle: the AI proposes a `VerificationStatus`; the human adjudicates
with a `ReviewAction`. The two are kept strictly separate. The set of actions
offered for a line is a function of its status (adaptive), and any action
recorded is validated against that set so an invalid action can never be
persisted.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class VerificationStatus(str, Enum):
    VERIFIED = "verified"                  # checked against a document, passes
    EXCEPTION = "exception"                # checked, fails or mismatches
    DOCUMENT_MISSING = "document_missing"  # required doc absent from the file set
    PENDING_SYSTEM_DATA = "pending_system" # needs LOS/CIBIL/CAM, not yet available
    NOT_APPLICABLE = "not_applicable"      # conditional item that does not apply
    MANUAL_REVIEW = "manual_review"        # policy-driven human-only check
    NEEDS_SIGNOFF = "needs_signoff"        # sign/seal/notary: AI detects, human authenticates


class ReviewAction(str, Enum):
    ACCEPT = "accept"                  # endorse an auto-verified line
    DECLINE = "decline"                # reject an auto-verified line
    OVERRIDE_ACCEPT = "override_accept"# accept despite a flagged exception
    CONFIRM_ISSUE = "confirm_issue"    # agree the exception is real
    RAISE_QUERY = "raise_query"        # send back for clarification
    NOTE = "note"                      # record an observation (no pass/fail)
    WAIVE = "waive"                    # explicitly waive a missing/pending item
    CONFIRM_SIGNOFF = "confirm_signoff"# human confirms sign/seal present & valid


# Adaptive action sets. A line's status determines which actions are offered,
# and recording validates against this map. NOT_APPLICABLE intentionally allows
# only NOTE (a reviewer who disagrees records why).
ALLOWED_ACTIONS: dict[VerificationStatus, tuple[ReviewAction, ...]] = {
    VerificationStatus.VERIFIED: (ReviewAction.ACCEPT, ReviewAction.DECLINE),
    VerificationStatus.EXCEPTION: (
        ReviewAction.OVERRIDE_ACCEPT,
        ReviewAction.CONFIRM_ISSUE,
        ReviewAction.RAISE_QUERY,
    ),
    VerificationStatus.DOCUMENT_MISSING: (ReviewAction.NOTE, ReviewAction.WAIVE),
    VerificationStatus.PENDING_SYSTEM_DATA: (ReviewAction.NOTE, ReviewAction.WAIVE),
    VerificationStatus.NOT_APPLICABLE: (ReviewAction.NOTE,),
    VerificationStatus.MANUAL_REVIEW: (
        ReviewAction.CONFIRM_SIGNOFF,
        ReviewAction.DECLINE,
        ReviewAction.RAISE_QUERY,
    ),
    VerificationStatus.NEEDS_SIGNOFF: (
        ReviewAction.CONFIRM_SIGNOFF,
        ReviewAction.DECLINE,
        ReviewAction.RAISE_QUERY,
    ),
}

# Statuses eligible for the "accept all auto-verified" bulk action.
BULK_ACCEPTABLE = {VerificationStatus.VERIFIED}


def allowed_actions_for(status: VerificationStatus) -> list[str]:
    return [a.value for a in ALLOWED_ACTIONS.get(status, ())]


def is_action_allowed(status: VerificationStatus, action: ReviewAction) -> bool:
    return action in ALLOWED_ACTIONS.get(status, ())


@dataclass
class Decision:
    """An append-only adjudication record. Captures the AI's verdict and the
    exact evidence at the moment the human decided, for the audit trail."""
    case_id: str
    item_id: str
    action: ReviewAction
    reviewer: str
    created_at: str                       # ISO-8601 UTC
    note: Optional[str] = None
    ai_status_at_decision: Optional[str] = None
    ai_finding_at_decision: Optional[str] = None
    evidence_doc: Optional[str] = None
    evidence_page: Optional[int] = None
    evidence_snippet: Optional[str] = None

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["action"] = self.action.value if isinstance(self.action, ReviewAction) else self.action
        return d
