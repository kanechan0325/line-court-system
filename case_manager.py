"""Case lifecycle management — state machine for civil and criminal cases."""

import logging
from datetime import datetime, timedelta
from typing import Optional

from config import APPEAL_DEADLINE_DAYS
from models import (
    CaseType, CourtLevel, CaseStatus, CivilPhase, CriminalPhase,
    CaseRecord,
)
import database as db
import ai_engine

logger = logging.getLogger(__name__)


def _format_appeal_deadline(deadline: Optional[datetime]) -> str:
    """Format appeal deadline for display."""
    if not deadline:
        return "14日以内"
    return deadline.strftime("%Y年%m月%d日 %H:%M まで")


def _format_logs(logs: list[dict]) -> str:
    """Format case logs for AI context."""
    if not logs:
        return "（記録なし）"
    parts = []
    for log in logs:
        ts = log.get("created_at", "")
        if isinstance(ts, datetime):
            ts = ts.strftime("%Y-%m-%d %H:%M")
        parts.append(f"[{ts}] [{log['actor']}] ({log['phase']})\n{log['content']}")
    return "\n\n".join(parts)


def _format_evidence(evidences: list) -> str:
    """Format evidence list for AI context."""
    if not evidences:
        return ""
    parts = []
    for i, ev in enumerate(evidences, 1):
        parts.append(f"証拠{i} ({ev.evidence_type}): {ev.content}")
    return "\n".join(parts)


def _format_precedents(precedents: list) -> str:
    """Format precedent list for AI context."""
    if not precedents:
        return ""
    parts = []
    for p in precedents:
        parts.append(f"【{p.case_number}】{p.summary}\n判決: {p.verdict}")
        if p.sentence:
            parts.append(f"量刑: {p.sentence}")
    return "\n".join(parts)


# ============================================================
# Civil Case Flow
# ============================================================

async def file_civil_complaint(
    group_id: str, plaintiff_id: str, defendant_id: str, complaint_text: str
) -> str:
    """File a civil complaint: create case → review → assign number."""
    # Create case
    case = await db.create_case(
        case_type=CaseType.CIVIL,
        court_level=CourtLevel.DISTRICT,
        phase=CivilPhase.COMPLAINT_FILED,
        plaintiff_id=plaintiff_id,
        defendant_id=defendant_id,
        group_id=group_id,
        complaint_text=complaint_text,
    )
    await db.add_case_log(case.id, CivilPhase.COMPLAINT_FILED, plaintiff_id, complaint_text)

    # AI judge reviews
    await db.update_case_phase(case.id, CivilPhase.REVIEW)
    case.phase = CivilPhase.REVIEW
    review = await ai_engine.judge_review_complaint(case)
    await db.add_case_log(case.id, CivilPhase.REVIEW, "JUDGE", review)

    # Accept (default behavior) — assign case number
    await db.update_case_phase(case.id, CivilPhase.CASE_NUMBERED)

    # Clerk creates record
    record = await ai_engine.clerk_create_record(case, "訴状受理", complaint_text)
    await db.add_case_log(case.id, CivilPhase.CASE_NUMBERED, "CLERK", record)

    response = (
        f"⚖️ 【民事事件受理】\n"
        f"事件番号: {case.case_number}\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 【受理審査結果】\n{review}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 {record}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"被告は /答弁 コマンドで答弁書を提出してください。"
    )
    return response


async def submit_answer(case_id: int, defendant_id: str, answer_text: str) -> str:
    """Submit an answer to a civil complaint."""
    case = await db.get_case(case_id)
    if not case:
        return "❌ 事件が見つかりません。"
    if case.case_type != CaseType.CIVIL:
        return "❌ この事件は民事事件ではありません。"
    if case.defendant_id != defendant_id:
        return "❌ あなたはこの事件の被告ではありません。"
    if case.phase not in (CivilPhase.CASE_NUMBERED, CivilPhase.ANSWER_SUBMITTED):
        return f"❌ 現在のフェーズ（{case.phase}）では答弁書を提出できません。\n答弁書は事件番号付与後に提出してください。"

    await db.update_case_answer(case_id, answer_text)
    await db.update_case_phase(case_id, CivilPhase.ANSWER_SUBMITTED)
    await db.add_case_log(case_id, CivilPhase.ANSWER_SUBMITTED, defendant_id, answer_text)

    # Proceed to issue organization
    case.answer_text = answer_text
    case.phase = CivilPhase.ISSUE_ORGANIZATION
    await db.update_case_phase(case_id, CivilPhase.ISSUE_ORGANIZATION)

    logs = await db.get_case_logs(case_id)
    issues = await ai_engine.judge_organize_issues(case, _format_logs(logs))
    await db.add_case_log(case_id, CivilPhase.ISSUE_ORGANIZATION, "JUDGE", issues)

    return (
        f"⚖️ 【{case.case_number}】答弁書受理・争点整理\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 【答弁書】\n{answer_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"🔍 【争点整理】\n{issues}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"/弁論 で口頭弁論、/証拠 で証拠提出、/和解 で和解勧告を求められます。"
    )


async def propose_settlement(case_id: int) -> str:
    """Judge proposes a settlement."""
    case = await db.get_case(case_id)
    if not case:
        return "❌ 事件が見つかりません。"
    if case.case_type != CaseType.CIVIL:
        return "❌ 和解勧告は民事事件のみです。"
    allowed = (
        CivilPhase.ISSUE_ORGANIZATION, CivilPhase.ORAL_ARGUMENT,
        CivilPhase.EVIDENCE_EXAMINATION,
    )
    if case.phase not in allowed:
        return f"❌ 現在のフェーズ（{case.phase}）では和解勧告を行えません。\n争点整理〜証拠調べの段階で /和解 を使用してください。"

    await db.update_case_phase(case_id, CivilPhase.SETTLEMENT_PROPOSED)
    logs = await db.get_case_logs(case_id)
    settlement = await ai_engine.judge_settlement_proposal(case, _format_logs(logs))
    await db.add_case_log(case_id, CivilPhase.SETTLEMENT_PROPOSED, "JUDGE", settlement)

    return (
        f"⚖️ 【{case.case_number}】和解勧告\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"{settlement}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"原告・被告は /和解同意 または /和解拒否 で応答してください。"
    )


async def accept_settlement(case_id: int, user_id: str) -> str:
    """Accept a settlement proposal."""
    case = await db.get_case(case_id)
    if not case:
        return "❌ 事件が見つかりません。"
    if case.phase != CivilPhase.SETTLEMENT_PROPOSED:
        return "❌ 和解勧告が出されていません。"
    if user_id not in (case.plaintiff_id, case.defendant_id):
        return "❌ あなたはこの事件の当事者ではありません。"

    await db.add_case_log(case_id, CivilPhase.SETTLEMENT_PROPOSED, user_id, "和解に同意")

    # Check if both parties agreed
    logs = await db.get_case_logs(case_id)
    agreed_parties = set()
    for log in logs:
        if log["phase"] == CivilPhase.SETTLEMENT_PROPOSED and "和解に同意" in log["content"]:
            agreed_parties.add(log["actor"])

    if case.plaintiff_id in agreed_parties and case.defendant_id in agreed_parties:
        await db.update_case_status(case_id, CaseStatus.SETTLED)
        await db.update_case_phase(case_id, CivilPhase.CLOSED)
        return (
            f"⚖️ 【{case.case_number}】和解成立\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"双方の合意により和解が成立しました。\n"
            f"本事件は終結します。"
        )
    else:
        role = "原告" if user_id == case.plaintiff_id else "被告"
        return f"✅ {role}が和解に同意しました。もう一方の当事者の応答を待っています。"


async def reject_settlement(case_id: int, user_id: str) -> str:
    """Reject a settlement proposal."""
    case = await db.get_case(case_id)
    if not case:
        return "❌ 事件が見つかりません。"
    if case.phase != CivilPhase.SETTLEMENT_PROPOSED:
        return "❌ 和解勧告が出されていません。"
    if user_id not in (case.plaintiff_id, case.defendant_id):
        return "❌ あなたはこの事件の当事者ではありません。"

    await db.add_case_log(case_id, CivilPhase.SETTLEMENT_PROPOSED, user_id, "和解を拒否")
    await db.update_case_phase(case_id, CivilPhase.ORAL_ARGUMENT)

    role = "原告" if user_id == case.plaintiff_id else "被告"
    return (
        f"⚖️ 【{case.case_number}】和解拒否\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{role}が和解を拒否しました。\n"
        f"口頭弁論に移行します。/弁論 で弁論を行ってください。"
    )


async def submit_argument(case_id: int, user_id: str, content: str) -> str:
    """Submit an oral argument."""
    case = await db.get_case(case_id)
    if not case:
        return "❌ 事件が見つかりません。"
    if case.status != CaseStatus.ACTIVE:
        return "❌ この事件は既に終結しています。"
    if user_id not in (case.plaintiff_id, case.defendant_id):
        return "❌ あなたはこの事件の当事者ではありません。"

    # Allow arguments in appropriate phases
    if case.case_type == CaseType.CIVIL:
        allowed_phases = (
            CivilPhase.ISSUE_ORGANIZATION, CivilPhase.ORAL_ARGUMENT,
            CivilPhase.EVIDENCE_EXAMINATION,
        )
    else:
        allowed_phases = (
            CriminalPhase.EVIDENCE_EXAMINATION, CriminalPhase.DEFENDANT_QUESTIONING,
        )
    if case.phase not in allowed_phases:
        return f"❌ 現在のフェーズ（{case.phase}）では弁論できません。\n/事件詳細 で現在のフェーズを確認してください。"

    if case.case_type == CaseType.CIVIL and case.phase != CivilPhase.ORAL_ARGUMENT:
        await db.update_case_phase(case_id, CivilPhase.ORAL_ARGUMENT)

    await db.add_case_log(case_id, case.phase, user_id, content)

    # Judge responds
    logs = await db.get_case_logs(case_id)
    judge_response = await ai_engine.judge_respond(
        case, content, f"口頭弁論における当事者の主張:\n{content}"
    )
    await db.add_case_log(case_id, case.phase, "JUDGE", judge_response)

    role = "原告" if user_id == case.plaintiff_id else "被告"
    return (
        f"⚖️ 【{case.case_number}】口頭弁論\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📢 【{role}の主張】\n{content}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"⚖️ 【裁判官】\n{judge_response}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"/弁論 で追加弁論、/証拠 で証拠提出、/判決 で判決を求められます。"
    )


async def submit_evidence(case_id: int, user_id: str, content: str) -> str:
    """Submit evidence for a case."""
    case = await db.get_case(case_id)
    if not case:
        return "❌ 事件が見つかりません。"
    if case.status != CaseStatus.ACTIVE:
        return "❌ この事件は既に終結しています。"
    if user_id not in (case.plaintiff_id, case.defendant_id):
        return "❌ あなたはこの事件の当事者ではありません。"

    evidence = await db.add_evidence(case_id, user_id, content)
    await db.add_case_log(case_id, case.phase, user_id, f"証拠提出: {content}")

    if case.case_type == CaseType.CIVIL and case.phase != CivilPhase.EVIDENCE_EXAMINATION:
        await db.update_case_phase(case_id, CivilPhase.EVIDENCE_EXAMINATION)

    return (
        f"⚖️ 【{case.case_number}】証拠提出\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"証拠番号: {evidence.id}\n"
        f"提出者: {'原告' if user_id == case.plaintiff_id else '被告'}\n"
        f"内容: {content}\n\n"
        f"証拠は記録されました。"
    )


async def render_civil_verdict(case_id: int) -> str:
    """Render a verdict for a civil case."""
    case = await db.get_case(case_id)
    if not case:
        return "❌ 事件が見つかりません。"
    if case.case_type != CaseType.CIVIL:
        return "❌ この事件は民事事件ではありません。"
    if case.phase in (CivilPhase.VERDICT, CivilPhase.CLOSED):
        return "❌ この事件は既に判決済みです。"

    # Phase prerequisite: must have had at least oral argument or evidence
    required_phases = (
        CivilPhase.ORAL_ARGUMENT, CivilPhase.EVIDENCE_EXAMINATION,
        CivilPhase.FINAL_BRIEF, CivilPhase.SETTLEMENT_PROPOSED,
    )
    if case.phase not in required_phases:
        return (
            f"❌ 判決の前に弁論または証拠調べが必要です（現在: {case.phase}）。\n"
            f"/弁論 で口頭弁論、/証拠 で証拠を提出してください。"
        )

    # Transition through FINAL_BRIEF before verdict
    await db.update_case_phase(case_id, CivilPhase.FINAL_BRIEF)
    await db.add_case_log(case_id, CivilPhase.FINAL_BRIEF, "JUDGE", "最終準備書面段階を経て判決に移行")

    logs = await db.get_case_logs(case_id)
    evidences = await db.get_evidence(case_id)
    precedents = await db.search_precedents(case.complaint_text[:50])

    verdict_text, verdict_data = await ai_engine.judge_render_verdict(
        case, _format_logs(logs), _format_evidence(evidences), _format_precedents(precedents)
    )

    sentence = None
    summary = verdict_text[:100] if verdict_text else ""
    verdict_label = ""
    if verdict_data:
        sentence = verdict_data.get("sentence")
        summary = verdict_data.get("summary", summary)
        verdict_label = verdict_data.get("verdict", "")

    # Always save as precedent (fallback if JSON parse failed)
    await db.save_precedent(
        case_number=case.case_number,
        case_type=case.case_type,
        summary=summary,
        verdict=verdict_label or "UNKNOWN",
        sentence=sentence,
        judgment_text=verdict_text,
    )

    await db.update_case_verdict(case_id, verdict_text, sentence)
    await db.update_case_phase(case_id, CivilPhase.VERDICT)
    await db.add_case_log(case_id, CivilPhase.VERDICT, "JUDGE", verdict_text)

    # Refresh case to get appeal_deadline
    case = await db.get_case(case_id)
    deadline_str = _format_appeal_deadline(case.appeal_deadline if case else None)

    return (
        f"⚖️ 【{case.case_number}】判決\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"{verdict_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"控訴期限: {deadline_str}\n"
        f"/控訴 で控訴できます。"
    )


# ============================================================
# Criminal Case Flow
# ============================================================

async def file_criminal_complaint(
    group_id: str, accuser_id: str, suspect_id: str, complaint_text: str
) -> str:
    """File a criminal complaint and begin investigation."""
    case = await db.create_case(
        case_type=CaseType.CRIMINAL,
        court_level=CourtLevel.DISTRICT,
        phase=CriminalPhase.COMPLAINT_FILED,
        plaintiff_id=accuser_id,
        defendant_id=suspect_id,
        group_id=group_id,
        complaint_text=complaint_text,
    )
    await db.add_case_log(case.id, CriminalPhase.COMPLAINT_FILED, accuser_id, complaint_text)

    # Prosecutor investigates
    await db.update_case_phase(case.id, CriminalPhase.INVESTIGATION)
    case.phase = CriminalPhase.INVESTIGATION
    investigation = await ai_engine.prosecutor_investigate(case)
    await db.add_case_log(case.id, CriminalPhase.INVESTIGATION, "PROSECUTOR", investigation)

    # Clerk records
    record = await ai_engine.clerk_create_record(case, "告訴受理・捜査開始", complaint_text)
    await db.add_case_log(case.id, CriminalPhase.INVESTIGATION, "CLERK", record)

    return (
        f"🔍 【刑事事件受理】\n"
        f"事件番号: {case.case_number}\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"🔎 【捜査報告】\n{investigation}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 {record}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"/起訴判断 で検察官の起訴/不起訴判断を求めてください。"
    )


async def decide_prosecution(case_id: int) -> str:
    """Prosecutor decides whether to prosecute."""
    case = await db.get_case(case_id)
    if not case:
        return "❌ 事件が見つかりません。"
    if case.case_type != CaseType.CRIMINAL:
        return "❌ この事件は刑事事件ではありません。"
    if case.phase != CriminalPhase.INVESTIGATION:
        return "❌ 捜査段階ではありません。"

    logs = await db.get_case_logs(case_id)
    investigation_log = ""
    for log in logs:
        if log["actor"] == "PROSECUTOR":
            investigation_log = log["content"]

    await db.update_case_phase(case_id, CriminalPhase.PROSECUTION_DECISION)
    case.phase = CriminalPhase.PROSECUTION_DECISION

    decision_text, is_prosecuted = await ai_engine.prosecutor_decide_charge(
        case, investigation_log
    )
    await db.add_case_log(case_id, CriminalPhase.PROSECUTION_DECISION, "PROSECUTOR", decision_text)

    if not is_prosecuted:
        await db.update_case_phase(case_id, CriminalPhase.NOT_PROSECUTED)
        await db.update_case_status(case_id, CaseStatus.CLOSED)
        return (
            f"⚖️ 【{case.case_number}】不起訴処分\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"{decision_text}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"本事件は不起訴により終結しました。"
        )

    # Prosecuted — notify rights
    await db.update_case_phase(case_id, CriminalPhase.RIGHTS_NOTIFICATION)
    case.phase = CriminalPhase.RIGHTS_NOTIFICATION
    rights = await ai_engine.defense_rights_notification(case)
    await db.add_case_log(case_id, CriminalPhase.RIGHTS_NOTIFICATION, "DEFENSE", rights)

    return (
        f"⚖️ 【{case.case_number}】起訴決定・権利告知\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 【起訴判断】\n{decision_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"🛡️ 【弁護人による権利告知】\n{rights}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"被告人は /罪状認否 認める または /罪状認否 否認 で応答してください。"
    )


async def arraignment(case_id: int, defendant_id: str, plea: str) -> str:
    """Handle arraignment (罪状認否)."""
    case = await db.get_case(case_id)
    if not case:
        return "❌ 事件が見つかりません。"
    if case.defendant_id != defendant_id:
        return "❌ あなたはこの事件の被告人ではありません。"
    if case.phase != CriminalPhase.RIGHTS_NOTIFICATION:
        return f"❌ 現在のフェーズ（{case.phase}）では罪状認否を行えません。\n権利告知後に /罪状認否 を使用してください。"

    await db.update_case_phase(case_id, CriminalPhase.ARRAIGNMENT)
    await db.add_case_log(case_id, CriminalPhase.ARRAIGNMENT, defendant_id, f"罪状認否: {plea}")

    # Opening procedure
    await db.update_case_phase(case_id, CriminalPhase.OPENING_PROCEDURE)
    case.phase = CriminalPhase.OPENING_PROCEDURE
    procedure = await ai_engine.opening_procedure(case)
    await db.add_case_log(case_id, CriminalPhase.OPENING_PROCEDURE, "JUDGE", procedure)

    return (
        f"⚖️ 【{case.case_number}】罪状認否・冒頭手続き\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 【被告人の罪状認否】\n{plea}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"⚖️ 【冒頭手続き】\n{procedure}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"/次へ で冒頭陳述に進みます。"
    )


async def proceed_criminal_phase(case_id: int) -> str:
    """Advance the criminal case to the next phase."""
    case = await db.get_case(case_id)
    if not case:
        return "❌ 事件が見つかりません。"
    if case.case_type != CaseType.CRIMINAL:
        return "❌ この事件は刑事事件ではありません。"
    if case.status != CaseStatus.ACTIVE:
        return "❌ この事件は既に終結しています。"

    logs = await db.get_case_logs(case_id)
    evidences = await db.get_evidence(case_id)
    logs_text = _format_logs(logs)
    evidence_text = _format_evidence(evidences)

    current_phase = case.phase

    if current_phase == CriminalPhase.OPENING_PROCEDURE:
        # → Opening statements
        await db.update_case_phase(case_id, CriminalPhase.OPENING_STATEMENT)
        case.phase = CriminalPhase.OPENING_STATEMENT

        prosecutor_opening = await ai_engine.prosecutor_opening(case)
        await db.add_case_log(case_id, CriminalPhase.OPENING_STATEMENT, "PROSECUTOR", prosecutor_opening)

        defense_open = await ai_engine.defense_opening(case)
        await db.add_case_log(case_id, CriminalPhase.OPENING_STATEMENT, "DEFENSE", defense_open)

        return (
            f"⚖️ 【{case.case_number}】冒頭陳述\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📢 【検察官冒頭陳述】\n{prosecutor_opening}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"🛡️ 【弁護人冒頭陳述】\n{defense_open}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"/証拠 で証拠提出、/次へ で被告人質問に進みます。"
        )

    elif current_phase == CriminalPhase.OPENING_STATEMENT:
        # → Evidence examination
        await db.update_case_phase(case_id, CriminalPhase.EVIDENCE_EXAMINATION)
        return (
            f"⚖️ 【{case.case_number}】証拠調べ\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"証拠調べに入ります。\n"
            f"/証拠 で証拠を提出してください。\n"
            f"/弁論 で主張を追加できます。\n"
            f"/次へ で被告人質問に進みます。"
        )

    elif current_phase == CriminalPhase.EVIDENCE_EXAMINATION:
        # → Defendant questioning
        await db.update_case_phase(case_id, CriminalPhase.DEFENDANT_QUESTIONING)
        case.phase = CriminalPhase.DEFENDANT_QUESTIONING

        questions = await ai_engine.defendant_questioning_prompt(case, logs_text)
        await db.add_case_log(case_id, CriminalPhase.DEFENDANT_QUESTIONING, "JUDGE", questions)

        return (
            f"⚖️ 【{case.case_number}】被告人質問\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"⚖️ 【裁判官からの質問】\n{questions}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"被告人は /弁論 で回答してください。\n"
            f"/次へ で論告求刑に進みます。"
        )

    elif current_phase == CriminalPhase.DEFENDANT_QUESTIONING:
        # → Prosecution closing
        await db.update_case_phase(case_id, CriminalPhase.PROSECUTION_CLOSING)
        case.phase = CriminalPhase.PROSECUTION_CLOSING

        closing = await ai_engine.prosecutor_closing(case, logs_text, evidence_text)
        await db.add_case_log(case_id, CriminalPhase.PROSECUTION_CLOSING, "PROSECUTOR", closing)

        return (
            f"⚖️ 【{case.case_number}】論告求刑\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📢 【検察官論告求刑】\n{closing}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"/次へ で最終弁論に進みます。"
        )

    elif current_phase == CriminalPhase.PROSECUTION_CLOSING:
        # → Defense closing
        await db.update_case_phase(case_id, CriminalPhase.DEFENSE_CLOSING)
        case.phase = CriminalPhase.DEFENSE_CLOSING

        defense_close = await ai_engine.defense_closing(case, logs_text, evidence_text)
        await db.add_case_log(case_id, CriminalPhase.DEFENSE_CLOSING, "DEFENSE", defense_close)

        return (
            f"⚖️ 【{case.case_number}】最終弁論\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"🛡️ 【弁護人最終弁論】\n{defense_close}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"被告人は /最終陳述 で最終陳述を行ってください。"
        )

    elif current_phase == CriminalPhase.DEFENSE_CLOSING:
        # Waive final statement and proceed
        await db.update_case_phase(case_id, CriminalPhase.FINAL_STATEMENT)
        await db.add_case_log(
            case_id, CriminalPhase.FINAL_STATEMENT, "JUDGE",
            "被告人は最終陳述の権利を放棄したものとみなす。"
        )
        return (
            f"⚖️ 【{case.case_number}】最終陳述省略\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"被告人は最終陳述の権利を放棄したものとみなします。\n"
            f"/判決 で判決を求めてください。"
        )

    elif current_phase == CriminalPhase.FINAL_STATEMENT:
        return "❌ 最終陳述は完了しています。/判決 で判決を求めてください。"

    else:
        return f"❌ 現在のフェーズ（{current_phase}）では /次へ は使用できません。\n/事件詳細 で現在のフェーズを確認してください。"


async def final_statement(case_id: int, defendant_id: str, content: str) -> str:
    """Handle defendant's final statement."""
    case = await db.get_case(case_id)
    if not case:
        return "❌ 事件が見つかりません。"
    if case.defendant_id != defendant_id:
        return "❌ あなたはこの事件の被告人ではありません。"
    if case.phase != CriminalPhase.DEFENSE_CLOSING:
        return f"❌ 現在のフェーズ（{case.phase}）では最終陳述を行えません。\n最終弁論の後に /最終陳述 を使用してください。"

    await db.update_case_phase(case_id, CriminalPhase.FINAL_STATEMENT)
    await db.add_case_log(case_id, CriminalPhase.FINAL_STATEMENT, defendant_id, content)

    return (
        f"⚖️ 【{case.case_number}】被告人最終陳述\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"{content}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"最終陳述が記録されました。\n"
        f"/判決 で判決を求めてください。"
    )


async def render_criminal_verdict(case_id: int) -> str:
    """Render a verdict for a criminal case."""
    case = await db.get_case(case_id)
    if not case:
        return "❌ 事件が見つかりません。"
    if case.case_type != CaseType.CRIMINAL:
        return "❌ この事件は刑事事件ではありません。"
    if case.phase in (CriminalPhase.VERDICT, CriminalPhase.CLOSED):
        return "❌ この事件は既に判決済みです。"

    # Phase prerequisite: must have had final statement or waiver
    required_phases = (
        CriminalPhase.FINAL_STATEMENT, CriminalPhase.DEFENSE_CLOSING,
        CriminalPhase.PROSECUTION_CLOSING,
    )
    if case.phase not in required_phases:
        return (
            f"❌ 判決の前に論告求刑・最終弁論・最終陳述が必要です（現在: {case.phase}）。\n"
            f"/次へ でフェーズを進めてください。"
        )

    logs = await db.get_case_logs(case_id)
    evidences = await db.get_evidence(case_id)
    precedents = await db.search_precedents(case.complaint_text[:50])

    verdict_text, verdict_data = await ai_engine.judge_render_verdict(
        case, _format_logs(logs), _format_evidence(evidences), _format_precedents(precedents)
    )

    sentence = None
    summary = verdict_text[:100] if verdict_text else ""
    verdict_label = ""
    if verdict_data:
        sentence = verdict_data.get("sentence")
        summary = verdict_data.get("summary", summary)
        verdict_label = verdict_data.get("verdict", "")

    # Always save as precedent
    await db.save_precedent(
        case_number=case.case_number,
        case_type=case.case_type,
        summary=summary,
        verdict=verdict_label or "UNKNOWN",
        sentence=sentence,
        judgment_text=verdict_text,
    )

    await db.update_case_verdict(case_id, verdict_text, sentence)
    await db.update_case_phase(case_id, CriminalPhase.VERDICT)
    await db.add_case_log(case_id, CriminalPhase.VERDICT, "JUDGE", verdict_text)

    case = await db.get_case(case_id)
    deadline_str = _format_appeal_deadline(case.appeal_deadline if case else None)

    return (
        f"⚖️ 【{case.case_number}】判決\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"{verdict_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"控訴期限: {deadline_str}\n"
        f"/控訴 で控訴できます。"
    )


# ============================================================
# Common Functions
# ============================================================

async def file_appeal(case_id: int, appellant_id: str) -> str:
    """File an appeal to a higher court."""
    case = await db.get_case(case_id)
    if not case:
        return "❌ 事件が見つかりません。"
    if appellant_id not in (case.plaintiff_id, case.defendant_id):
        return "❌ あなたはこの事件の当事者ではありません。"

    verdict_phase = CivilPhase.VERDICT if case.case_type == CaseType.CIVIL else CriminalPhase.VERDICT
    if case.phase != verdict_phase:
        return "❌ 判決が出ていない事件には控訴できません。"

    # Check appeal deadline
    if case.appeal_deadline and datetime.utcnow() > case.appeal_deadline:
        return "❌ 控訴期限（14日）を過ぎています。"

    # Determine next court level
    if case.court_level == CourtLevel.DISTRICT:
        next_level = CourtLevel.HIGH
    elif case.court_level == CourtLevel.HIGH:
        next_level = CourtLevel.SUPREME
    else:
        return "❌ 最高裁判所の判決に対しては控訴できません。"

    # Mark original case as appealed
    await db.update_case_status(case_id, CaseStatus.APPEALED)
    closed_phase = CivilPhase.CLOSED if case.case_type == CaseType.CIVIL else CriminalPhase.CLOSED
    await db.update_case_phase(case_id, closed_phase)

    # Create new case in higher court
    initial_phase = CivilPhase.COMPLAINT_FILED if case.case_type == CaseType.CIVIL else CriminalPhase.COMPLAINT_FILED
    new_case = await db.create_case(
        case_type=case.case_type,
        court_level=next_level,
        phase=initial_phase,
        plaintiff_id=case.plaintiff_id,
        defendant_id=case.defendant_id,
        group_id=case.group_id,
        complaint_text=f"【控訴】原審事件番号: {case.case_number}\n原審判決: {case.verdict_text or '(判決文なし)'}\n\n控訴理由: 原審判決に不服があるため控訴する。",
        parent_case_id=case.id,
    )

    # Auto-review
    await db.update_case_phase(new_case.id, CivilPhase.REVIEW if case.case_type == CaseType.CIVIL else CriminalPhase.INVESTIGATION)
    new_case.phase = CivilPhase.REVIEW if case.case_type == CaseType.CIVIL else CriminalPhase.INVESTIGATION

    review = await ai_engine.judge_review_complaint(new_case)
    await db.add_case_log(new_case.id, new_case.phase, "JUDGE", review)

    court_name = "高等裁判所" if next_level == CourtLevel.HIGH else "最高裁判所"
    return (
        f"⚖️ 【控訴受理】\n"
        f"新事件番号: {new_case.case_number}\n"
        f"審級: {court_name}\n"
        f"原審事件番号: {case.case_number}\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 【受理審査】\n{review}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{'被告は /答弁 で答弁書を提出してください。' if case.case_type == CaseType.CIVIL else '/起訴判断 で手続きを進めてください。'}"
    )


async def get_case_list(group_id: str) -> str:
    """Get a list of cases for a group."""
    cases = await db.get_active_cases(group_id)
    if not cases:
        return "📋 現在進行中の事件はありません。"

    lines = ["📋 【事件一覧】\n━━━━━━━━━━━━━━━━━━"]
    for c in cases:
        case_type = "民事" if c.case_type == CaseType.CIVIL else "刑事"
        lines.append(f"\n📌 {c.case_number} ({case_type})")
        lines.append(f"   フェーズ: {c.phase}")
        lines.append(f"   状態: {c.status}")
    return "\n".join(lines)


async def get_case_detail(case_id: int) -> str:
    """Get detailed information about a case."""
    case = await db.get_case(case_id)
    if not case:
        return "❌ 事件が見つかりません。"

    case_type = "民事" if case.case_type == CaseType.CIVIL else "刑事"
    court = {"DISTRICT": "地裁", "HIGH": "高裁", "SUPREME": "最高裁"}.get(case.court_level, case.court_level)

    detail = (
        f"⚖️ 【事件詳細】{case.case_number}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"種別: {case_type} | 審級: {court}\n"
        f"フェーズ: {case.phase}\n"
        f"状態: {case.status}\n"
        f"提訴日: {case.created_at}\n"
    )

    if case.verdict_text:
        detail += f"\n📋 【判決】\n{case.verdict_text[:500]}"
        if case.sentence:
            detail += f"\n量刑: {case.sentence}"
        if case.appeal_deadline:
            detail += f"\n控訴期限: {case.appeal_deadline}"

    return detail


async def search_precedents_cmd(keyword: str) -> str:
    """Search for precedents by keyword."""
    precedents = await db.search_precedents(keyword)
    if not precedents:
        return f"📚 「{keyword}」に関連する判例は見つかりませんでした。"

    lines = [f"📚 【判例検索結果】「{keyword}」\n━━━━━━━━━━━━━━━━━━"]
    for p in precedents:
        lines.append(f"\n📌 {p.case_number}")
        lines.append(f"   {p.summary}")
        lines.append(f"   判決: {p.verdict}")
        if p.sentence:
            lines.append(f"   量刑: {p.sentence}")
    return "\n".join(lines)
