from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class CaseType(str, Enum):
    CIVIL = "CIVIL"
    CRIMINAL = "CRIMINAL"


class CourtLevel(str, Enum):
    SUMMARY = "SUMMARY"    # 簡易裁判所（略式事件）
    DISTRICT = "DISTRICT"
    HIGH = "HIGH"
    SUPREME = "SUPREME"


class CivilPhase(str, Enum):
    COMPLAINT_FILED = "COMPLAINT_FILED"
    REVIEW = "REVIEW"
    DISMISSED = "DISMISSED"              # 却下（受理審査で不受理）
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
    SUMMARY_CONSENT = "SUMMARY_CONSENT"       # 略式手続同意確認
    SUMMARY_ORDER = "SUMMARY_ORDER"           # 略式命令
    SUMMARY_OBJECTION = "SUMMARY_OBJECTION"   # 正式裁判請求済
    VERDICT = "VERDICT"
    CLOSED = "CLOSED"


class CaseStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    DISMISSED = "DISMISSED"                # 却下
    APPEALED = "APPEALED"
    SETTLED = "SETTLED"
    JOKOKU_APPEALED = "JOKOKU_APPEALED"  # 上告済
    RETRIAL = "RETRIAL"                    # 再審中
    KOKOKU_APPEALED = "KOKOKU_APPEALED"    # 抗告済


class CaseSubtype(str, Enum):
    SUMMARY = "SUMMARY"
    JOKOKU = "JOKOKU"
    RETRIAL = "RETRIAL"
    KOKOKU = "KOKOKU"
    FORMAL_FROM_SUMMARY = "FORMAL_FROM_SUMMARY"


class Verdict(str, Enum):
    GUILTY = "GUILTY"
    NOT_GUILTY = "NOT_GUILTY"
    PLAINTIFF_WINS = "PLAINTIFF_WINS"
    DEFENDANT_WINS = "DEFENDANT_WINS"
    PARTIAL = "PARTIAL"
    DISMISSED = "DISMISSED"
    SETTLED = "SETTLED"
    SUMMARY_FINE = "SUMMARY_FINE"  # 略式命令（罰金・科料）


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
    # NOT_PROSECUTED is a terminal branch from PROSECUTION_DECISION (not in linear order)
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

# Terminal phases that branch off the main flow
CRIMINAL_TERMINAL_PHASES = {
    CriminalPhase.NOT_PROSECUTED,  # branches from PROSECUTION_DECISION
    CriminalPhase.SUMMARY_CONSENT,  # branches into summary flow
    CriminalPhase.SUMMARY_ORDER,
    CriminalPhase.SUMMARY_OBJECTION,
}

# Summary trial phase order (略式手続フロー)
CRIMINAL_SUMMARY_PHASE_ORDER = [
    CriminalPhase.COMPLAINT_FILED,
    CriminalPhase.INVESTIGATION,
    CriminalPhase.PROSECUTION_DECISION,
    CriminalPhase.SUMMARY_CONSENT,
    CriminalPhase.SUMMARY_ORDER,
    CriminalPhase.SUMMARY_OBJECTION,  # only if formal trial requested
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
        CourtLevel.SUMMARY: "い",
        CourtLevel.DISTRICT: "わ",
        CourtLevel.HIGH: "う",
        CourtLevel.SUPREME: "あ",
    },
}


def generate_case_number(case_type: CaseType, court_level: CourtLevel, seq: int, is_retrial: bool = False) -> str:
    """Generate a case number like R8-(ワ)-001"""
    if seq < 1:
        seq = 1
    type_symbols = CASE_NUMBER_SYMBOLS.get(case_type, {})
    symbol = type_symbols.get(court_level)
    if symbol is None:
        # Fallback: use DISTRICT symbol if court level not defined for this case type
        symbol = type_symbols.get(CourtLevel.DISTRICT, "?")
    if is_retrial:
        symbol += "再"
    return f"R8-({symbol})-{seq:03d}"


# Phase display names in Japanese
PHASE_DISPLAY_NAMES = {
    # Civil phases
    "COMPLAINT_FILED": "訴状提出",
    "REVIEW": "受理審査",
    "CASE_NUMBERED": "事件番号付与",
    "ANSWER_SUBMITTED": "答弁書提出済",
    "ISSUE_ORGANIZATION": "争点整理",
    "SETTLEMENT_PROPOSED": "和解勧告中",
    "ORAL_ARGUMENT": "口頭弁論",
    "EVIDENCE_EXAMINATION": "証拠調べ",
    "FINAL_BRIEF": "最終準備書面",
    "VERDICT": "判決",
    "CLOSED": "終結",
    # Criminal phases (including summary trial)
    "SUMMARY_CONSENT": "略式手続同意確認",
    "SUMMARY_ORDER": "略式命令",
    "SUMMARY_OBJECTION": "正式裁判請求",
    "INVESTIGATION": "捜査",
    "PROSECUTION_DECISION": "起訴判断",
    "NOT_PROSECUTED": "不起訴",
    "RIGHTS_NOTIFICATION": "権利告知",
    "ARRAIGNMENT": "罪状認否",
    "OPENING_PROCEDURE": "冒頭手続",
    "OPENING_STATEMENT": "冒頭陳述",
    "DEFENDANT_QUESTIONING": "被告人質問",
    "PROSECUTION_CLOSING": "論告求刑",
    "DEFENSE_CLOSING": "最終弁論",
    "FINAL_STATEMENT": "最終陳述",
}


def get_phase_display(phase: str) -> str:
    """Get Japanese display name for a phase."""
    name = PHASE_DISPLAY_NAMES.get(phase)
    if name is None:
        return phase  # Return as-is for unknown phases
    return name


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
    case_subtype: Optional[str] = None  # "SUMMARY", "JOKOKU", "RETRIAL", "KOKOKU"

    def __post_init__(self):
        """Validate enum fields if non-empty (empty allowed for default construction)."""
        _valid_case_types = {e.value for e in CaseType}
        _valid_court_levels = {e.value for e in CourtLevel}
        _valid_statuses = {e.value for e in CaseStatus}
        if self.case_type and self.case_type not in _valid_case_types:
            raise ValueError(f"Invalid case_type: {self.case_type}")
        if self.court_level and self.court_level not in _valid_court_levels:
            raise ValueError(f"Invalid court_level: {self.court_level}")
        if self.status and self.status not in _valid_statuses:
            raise ValueError(f"Invalid status: {self.status}")


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
