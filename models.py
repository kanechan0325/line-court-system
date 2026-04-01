from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class CaseType(str, Enum):
    CIVIL = "CIVIL"
    CRIMINAL = "CRIMINAL"


class CourtLevel(str, Enum):
    DISTRICT = "DISTRICT"
    HIGH = "HIGH"
    SUPREME = "SUPREME"


class CivilPhase(str, Enum):
    COMPLAINT_FILED = "COMPLAINT_FILED"
    REVIEW = "REVIEW"
    CASE_NUMBERED = "CASE_NUMBERED"
    ANSWER_SUBMITTED = "ANSWER_SUBMITTED"
    ISSUE_ORGANIZATION = "ISSUE_ORGANIZATION"
    SETTLEMENT_PROPOSED = "SETTLEMENT_PROPOSED"
    ORAL_ARGUMENT = "ORAL_ARGUMENT"
    EVIDENCE_EXAMINATION = "EVIDENCE_EXAMINATION"
    FINAL_BRIEF = "FINAL_BRIEF"
    VERDICT = "VERDICT"
    CLOSED = "CLOSED"


class CriminalPhase(str, Enum):
    COMPLAINT_FILED = "COMPLAINT_FILED"
    INVESTIGATION = "INVESTIGATION"
    PROSECUTION_DECISION = "PROSECUTION_DECISION"
    NOT_PROSECUTED = "NOT_PROSECUTED"
    RIGHTS_NOTIFICATION = "RIGHTS_NOTIFICATION"
    ARRAIGNMENT = "ARRAIGNMENT"
    OPENING_PROCEDURE = "OPENING_PROCEDURE"
    OPENING_STATEMENT = "OPENING_STATEMENT"
    EVIDENCE_EXAMINATION = "EVIDENCE_EXAMINATION"
    DEFENDANT_QUESTIONING = "DEFENDANT_QUESTIONING"
    PROSECUTION_CLOSING = "PROSECUTION_CLOSING"
    DEFENSE_CLOSING = "DEFENSE_CLOSING"
    FINAL_STATEMENT = "FINAL_STATEMENT"
    VERDICT = "VERDICT"
    CLOSED = "CLOSED"


class CaseStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    APPEALED = "APPEALED"
    SETTLED = "SETTLED"


class Verdict(str, Enum):
    GUILTY = "GUILTY"
    NOT_GUILTY = "NOT_GUILTY"
    PLAINTIFF_WINS = "PLAINTIFF_WINS"
    DEFENDANT_WINS = "DEFENDANT_WINS"
    PARTIAL = "PARTIAL"
    DISMISSED = "DISMISSED"
    SETTLED = "SETTLED"


# Phase transition orders
CIVIL_PHASE_ORDER = [
    CivilPhase.COMPLAINT_FILED,
    CivilPhase.REVIEW,
    CivilPhase.CASE_NUMBERED,
    CivilPhase.ANSWER_SUBMITTED,
    CivilPhase.ISSUE_ORGANIZATION,
    CivilPhase.SETTLEMENT_PROPOSED,
    CivilPhase.ORAL_ARGUMENT,
    CivilPhase.EVIDENCE_EXAMINATION,
    CivilPhase.FINAL_BRIEF,
    CivilPhase.VERDICT,
    CivilPhase.CLOSED,
]

CRIMINAL_PHASE_ORDER = [
    CriminalPhase.COMPLAINT_FILED,
    CriminalPhase.INVESTIGATION,
    CriminalPhase.PROSECUTION_DECISION,
    CriminalPhase.RIGHTS_NOTIFICATION,
    CriminalPhase.ARRAIGNMENT,
    CriminalPhase.OPENING_PROCEDURE,
    CriminalPhase.OPENING_STATEMENT,
    CriminalPhase.EVIDENCE_EXAMINATION,
    CriminalPhase.DEFENDANT_QUESTIONING,
    CriminalPhase.PROSECUTION_CLOSING,
    CriminalPhase.DEFENSE_CLOSING,
    CriminalPhase.FINAL_STATEMENT,
    CriminalPhase.VERDICT,
    CriminalPhase.CLOSED,
]

# Case number symbols per court level and type
CASE_NUMBER_SYMBOLS = {
    CaseType.CIVIL: {
        CourtLevel.DISTRICT: "ワ",
        CourtLevel.HIGH: "ネ",
        CourtLevel.SUPREME: "オ",
    },
    CaseType.CRIMINAL: {
        CourtLevel.DISTRICT: "わ",
        CourtLevel.HIGH: "う",
        CourtLevel.SUPREME: "あ",
    },
}


def generate_case_number(case_type: CaseType, court_level: CourtLevel, seq: int) -> str:
    """Generate a case number like R8-(ワ)-001"""
    symbol = CASE_NUMBER_SYMBOLS[case_type][court_level]
    return f"R8-({symbol})-{seq:03d}"


@dataclass
class CaseRecord:
    id: int = 0
    case_number: str = ""
    case_type: str = ""
    court_level: str = "DISTRICT"
    phase: str = ""
    plaintiff_id: str = ""
    defendant_id: str = ""
    group_id: str = ""
    complaint_text: str = ""
    answer_text: Optional[str] = None
    verdict_text: Optional[str] = None
    sentence: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    appeal_deadline: Optional[datetime] = None
    parent_case_id: Optional[int] = None
    status: str = "ACTIVE"


@dataclass
class EvidenceRecord:
    id: int = 0
    case_id: int = 0
    submitter_id: str = ""
    content: str = ""
    evidence_type: str = "DOCUMENT"
    submitted_at: Optional[datetime] = None


@dataclass
class PrecedentRecord:
    id: int = 0
    case_number: str = ""
    case_type: str = ""
    summary: str = ""
    verdict: str = ""
    sentence: Optional[str] = None
    judgment_text: Optional[str] = None
    created_at: Optional[datetime] = None
