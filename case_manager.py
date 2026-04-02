"""Case lifecycle management — state machine for civil and criminal cases."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

JST = timezone(timedelta(hours=9))

from config import APPEAL_DEADLINE_DAYS, KOKOKU_DEADLINE_DAYS
from models import (
    CaseType, CourtLevel, CaseStatus, CivilPhase, CriminalPhase,
    CaseRecord, Verdict, get_phase_display,
)
import database as db
import ai_engine
from ai_engine import AIResponseError

logger = logging.getLogger(__name__)

# Keywords that indicate the judge dismissed/rejected the complaint
_DISMISS_KEYWORDS = ("却下", "不受理")
_ACCEPT_KEYWORDS = ("受理",)


def _is_dismissed(review_text: str) -> bool:
    """Check if the judge's review indicates dismissal.

    Counts accept vs dismiss keyword occurrences.
    '受理' alone means accepted; '却下' or '不受理' means dismissed.
    Since '不受理' contains '受理', we check dismiss keywords first.
    """
    text = review_text.replace("不受理", "＿不受理＿")  # protect compound word
    dismiss_count = sum(text.count(kw) for kw in ("＿不受理＿", "却下"))
    accept_count = text.count("受理")
    return dismiss_count > 0 and dismiss_count >= accept_count


def _format_appeal_deadline(deadline: Optional[datetime]) -> str:
    """Format appeal deadline for display in JST."""
    if not deadline:
        return "14日以内"
    # Convert to JST for display
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    jst_deadline = deadline.astimezone(JST)
    return jst_deadline.strftime("%Y年%m月%d日 %H:%M まで（日本時間）")


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

    # AI judge reviews — call AI first, then update phase
    case.phase = CivilPhase.REVIEW
    try:
        review = await ai_engine.judge_review_complaint(case)
    except AIResponseError as e:
        return f"❌ 【{case.case_number}】受理審査中にエラーが発生しました: {e}\n再度 /訴状 で提出してください。"

    await db.update_case_phase(case.id, CivilPhase.REVIEW)
    await db.add_case_log(case.id, CivilPhase.REVIEW, "JUDGE", review)

    # Check if judge dismissed the complaint
    if _is_dismissed(review):
        await db.update_case_phase(case.id, CivilPhase.DISMISSED)
        await db.update_case_status(case.id, CaseStatus.DISMISSED)
        await db.add_case_log(case.id, CivilPhase.DISMISSED, "JUDGE", "訴状却下")
        return (
            f"⚖️ 【民事事件却下】\n"
            f"事件番号: {case.case_number}\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📋 【受理審査結果】\n{review}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"訴状は却下されました。内容を修正して再度 /訴状 で提出できます。"
        )

    # Clerk creates record — call AI first, then update phase
    try:
        record = await ai_engine.clerk_create_record(case, "訴状受理", complaint_text)
    except AIResponseError:
        record = "（書記官記録の作成に失敗しました）"

    await db.update_case_phase(case.id, CivilPhase.CASE_NUMBERED)
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

    is_resubmission = case.phase == CivilPhase.ANSWER_SUBMITTED

    await db.update_case_answer(case_id, answer_text)
    await db.add_case_log(case_id, CivilPhase.ANSWER_SUBMITTED, defendant_id, answer_text)

    if is_resubmission:
        # Resubmission: update answer only, don't re-run issue organization
        return (
            f"⚖️ 【{case.case_number}】答弁書更新\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📋 【答弁書（更新）】\n{answer_text}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"答弁書が更新されました。"
        )

    await db.update_case_phase(case_id, CivilPhase.ANSWER_SUBMITTED)

    # First submission: proceed to issue organization
    case.answer_text = answer_text
    case.phase = CivilPhase.ISSUE_ORGANIZATION

    logs = await db.get_case_logs(case_id)
    try:
        issues = await ai_engine.judge_organize_issues(case, _format_logs(logs))
    except AIResponseError as e:
        # Phase stays at ANSWER_SUBMITTED so user can retry
        return f"❌ 【{case.case_number}】争点整理中にエラーが発生しました: {e}\n再度 /答弁 で答弁書を提出してください。"

    await db.update_case_phase(case_id, CivilPhase.ISSUE_ORGANIZATION)
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

    pre_settlement_phase = case.phase
    logs = await db.get_case_logs(case_id)

    try:
        settlement = await ai_engine.judge_settlement_proposal(case, _format_logs(logs))
    except AIResponseError as e:
        return f"❌ 【{case.case_number}】和解案生成中にエラーが発生しました: {e}\n再度 /和解 をお試しください。"

    await db.update_case_phase(case_id, CivilPhase.SETTLEMENT_PROPOSED)
    await db.add_case_log(
        case_id, CivilPhase.SETTLEMENT_PROPOSED, "JUDGE", settlement,
        action_type=f"SETTLEMENT_FROM:{pre_settlement_phase}",
    )

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

    await db.add_case_log(
        case_id, CivilPhase.SETTLEMENT_PROPOSED, user_id, "和解に同意",
        action_type="SETTLEMENT_ACCEPT",
    )

    # Check if both parties agreed (using action_type for reliable detection)
    logs = await db.get_case_logs(case_id)
    agreed_parties = set()
    for log in logs:
        if log.get("action_type") == "SETTLEMENT_ACCEPT":
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

    # Restore the phase from before settlement was proposed (use latest SETTLEMENT_FROM)
    restore_phase = CivilPhase.ORAL_ARGUMENT  # default fallback
    logs = await db.get_case_logs(case_id)
    for log in reversed(logs):
        action = log.get("action_type") or ""
        if action.startswith("SETTLEMENT_FROM:"):
            restore_phase = action.split(":", 1)[1]
            break

    await db.update_case_phase(case_id, restore_phase)

    role = "原告" if user_id == case.plaintiff_id else "被告"
    phase_label = get_phase_display(restore_phase)
    return (
        f"⚖️ 【{case.case_number}】和解拒否\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{role}が和解を拒否しました。\n"
        f"{phase_label}に戻ります。/弁論 で弁論を行ってください。"
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

    await db.add_case_log(case_id, case.phase, user_id, content)

    # Judge responds
    try:
        judge_response = await ai_engine.judge_respond(
            case, content, f"口頭弁論における当事者の主張:\n{content}"
        )
    except AIResponseError as e:
        return f"❌ 【{case.case_number}】裁判官応答中にエラーが発生しました: {e}\n再度 /弁論 をお試しください。"

    if case.case_type == CaseType.CIVIL and case.phase != CivilPhase.ORAL_ARGUMENT:
        await db.update_case_phase(case_id, CivilPhase.ORAL_ARGUMENT)

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

    # Phase restriction for criminal cases
    if case.case_type == CaseType.CRIMINAL:
        allowed_criminal = (
            CriminalPhase.OPENING_STATEMENT, CriminalPhase.EVIDENCE_EXAMINATION,
            CriminalPhase.DEFENDANT_QUESTIONING,
        )
        if case.phase not in allowed_criminal:
            return f"❌ 現在のフェーズ（{case.phase}）では証拠を提出できません。\n冒頭陳述〜被告人質問の段階で /証拠 を使用してください。"

    evidence = await db.add_evidence(case_id, user_id, content)
    await db.add_case_log(case_id, case.phase, user_id, f"証拠提出: {content}")

    if case.case_type == CaseType.CIVIL and case.phase != CivilPhase.EVIDENCE_EXAMINATION:
        await db.update_case_phase(case_id, CivilPhase.EVIDENCE_EXAMINATION)
    elif case.case_type == CaseType.CRIMINAL and case.phase != CriminalPhase.EVIDENCE_EXAMINATION:
        await db.update_case_phase(case_id, CriminalPhase.EVIDENCE_EXAMINATION)

    # Judge reviews evidence admissibility
    admission_note = ""
    try:
        admission_note = await ai_engine.judge_respond(
            case, content,
            f"証拠として提出された内容の証拠能力・関連性を簡潔に評価してください:\n{content}",
        )
    except AIResponseError:
        admission_note = "（証拠の評価は判決時に行います）"

    role = "原告" if user_id == case.plaintiff_id else "被告"
    return (
        f"⚖️ 【{case.case_number}】証拠提出\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"証拠番号: {evidence.id}\n"
        f"提出者: {role}\n"
        f"内容: {content}\n\n"
        f"⚖️ 【裁判官の証拠評価】\n{admission_note}"
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

    logs = await db.get_case_logs(case_id)
    evidences = await db.get_evidence(case_id)
    precedents = await db.search_precedents(case.complaint_text[:100])

    try:
        verdict_text, verdict_data = await ai_engine.judge_render_verdict(
            case, _format_logs(logs), _format_evidence(evidences), _format_precedents(precedents)
        )
    except AIResponseError as e:
        return f"❌ 【{case.case_number}】判決生成中にエラーが発生しました: {e}\n再度 /判決 をお試しください。"

    # Transition through FINAL_BRIEF before verdict
    await db.update_case_phase(case_id, CivilPhase.FINAL_BRIEF)
    await db.add_case_log(case_id, CivilPhase.FINAL_BRIEF, "JUDGE", "最終準備書面段階を経て判決に移行")

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

    # Guide user to the correct appeal command based on court level
    if case.court_level == CourtLevel.HIGH:
        appeal_guide = "/上告 [理由] で上告できます。"
    elif case.court_level == CourtLevel.SUPREME:
        appeal_guide = "最高裁判所の判決は最終判決です。"
    else:
        appeal_guide = "/控訴 で控訴できます。"

    return (
        f"⚖️ 【{case.case_number}】判決\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"{verdict_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"控訴期限: {deadline_str}\n"
        f"{appeal_guide}"
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

    # Prosecutor investigates — call AI first, then update phase
    case.phase = CriminalPhase.INVESTIGATION
    try:
        investigation = await ai_engine.prosecutor_investigate(case)
    except AIResponseError as e:
        return f"❌ 【{case.case_number}】捜査中にエラーが発生しました: {e}\n再度 /告訴 で提出してください。"

    await db.update_case_phase(case.id, CriminalPhase.INVESTIGATION)
    await db.add_case_log(case.id, CriminalPhase.INVESTIGATION, "PROSECUTOR", investigation)

    # Clerk records
    try:
        record = await ai_engine.clerk_create_record(case, "告訴受理・捜査開始", complaint_text)
    except AIResponseError:
        record = "（書記官記録の作成に失敗しました）"
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

    case.phase = CriminalPhase.PROSECUTION_DECISION

    try:
        decision_text, is_prosecuted, procedure = await ai_engine.prosecutor_decide_charge(
            case, investigation_log
        )
    except AIResponseError as e:
        return f"❌ 【{case.case_number}】起訴判断中にエラーが発生しました: {e}\n再度 /起訴判断 をお試しください。"

    await db.update_case_phase(case_id, CriminalPhase.PROSECUTION_DECISION)
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

    # Check if summary procedure is recommended
    if procedure == "SUMMARY":
        await db.add_case_log(
            case_id, CriminalPhase.PROSECUTION_DECISION, "SYSTEM",
            "略式手続相当と判断されました。", action_type="SUMMARY_ELIGIBLE"
        )
        return (
            f"⚖️ 【{case.case_number}】起訴決定（略式手続相当）\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📋 【起訴判断】\n{decision_text}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"検察官は本件を略式手続相当と判断しました。\n"
            f"/略式起訴 で略式命令を請求、または /次へ で通常の起訴手続きに進めます。"
        )

    # Prosecuted (formal) — notify rights (call AI first, then update phase)
    case.phase = CriminalPhase.RIGHTS_NOTIFICATION
    try:
        rights = await ai_engine.defense_rights_notification(case)
    except AIResponseError:
        rights = "（権利告知の生成に失敗しました。被告人には黙秘権等の権利があります。）"

    await db.update_case_phase(case_id, CriminalPhase.RIGHTS_NOTIFICATION)
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

    # Opening procedure — call AI first, then update phase
    case.phase = CriminalPhase.OPENING_PROCEDURE
    try:
        procedure = await ai_engine.opening_procedure(case)
    except AIResponseError as e:
        return f"❌ 【{case.case_number}】冒頭手続き生成中にエラーが発生しました: {e}\n罪状認否は記録されました。/次へ で再試行してください。"

    await db.update_case_phase(case_id, CriminalPhase.OPENING_PROCEDURE)
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

    if current_phase == CriminalPhase.PROSECUTION_DECISION:
        # Transition from prosecution decision to formal trial (skip summary)
        case.phase = CriminalPhase.RIGHTS_NOTIFICATION
        try:
            rights = await ai_engine.defense_rights_notification(case)
        except AIResponseError:
            rights = "（権利告知の生成に失敗しました。被告人には黙秘権等の権利があります。）"

        await db.update_case_phase(case_id, CriminalPhase.RIGHTS_NOTIFICATION)
        await db.add_case_log(case_id, CriminalPhase.RIGHTS_NOTIFICATION, "DEFENSE", rights)

        return (
            f"⚖️ 【{case.case_number}】通常起訴・権利告知\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"🛡️ 【弁護人による権利告知】\n{rights}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"被告人は /罪状認否 認める または /罪状認否 否認 で応答してください。"
        )

    elif current_phase == CriminalPhase.OPENING_PROCEDURE:
        # → Opening statements — call AI first, then update phase
        case.phase = CriminalPhase.OPENING_STATEMENT

        try:
            prosecutor_opening = await ai_engine.prosecutor_opening(case)
        except AIResponseError as e:
            return f"❌ 【{case.case_number}】冒頭陳述生成中にエラーが発生しました: {e}\n再度 /次へ をお試しください。"

        try:
            defense_open = await ai_engine.defense_opening(case)
        except AIResponseError:
            defense_open = "（弁護人冒頭陳述の生成に失敗しました）"

        await db.update_case_phase(case_id, CriminalPhase.OPENING_STATEMENT)
        await db.add_case_log(case_id, CriminalPhase.OPENING_STATEMENT, "PROSECUTOR", prosecutor_opening)
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
        # → Defendant questioning — call AI first
        case.phase = CriminalPhase.DEFENDANT_QUESTIONING

        try:
            questions = await ai_engine.defendant_questioning_prompt(case, logs_text)
        except AIResponseError as e:
            return f"❌ 【{case.case_number}】被告人質問生成中にエラーが発生しました: {e}\n再度 /次へ をお試しください。"

        await db.update_case_phase(case_id, CriminalPhase.DEFENDANT_QUESTIONING)
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
        # → Prosecution closing — call AI first
        case.phase = CriminalPhase.PROSECUTION_CLOSING

        try:
            closing = await ai_engine.prosecutor_closing(case, logs_text, evidence_text)
        except AIResponseError as e:
            return f"❌ 【{case.case_number}】論告求刑生成中にエラーが発生しました: {e}\n再度 /次へ をお試しください。"

        await db.update_case_phase(case_id, CriminalPhase.PROSECUTION_CLOSING)
        await db.add_case_log(case_id, CriminalPhase.PROSECUTION_CLOSING, "PROSECUTOR", closing)

        return (
            f"⚖️ 【{case.case_number}】論告求刑\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📢 【検察官論告求刑】\n{closing}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"/次へ で最終弁論に進みます。"
        )

    elif current_phase == CriminalPhase.PROSECUTION_CLOSING:
        # → Defense closing — call AI first
        case.phase = CriminalPhase.DEFENSE_CLOSING

        try:
            defense_close = await ai_engine.defense_closing(case, logs_text, evidence_text)
        except AIResponseError as e:
            return f"❌ 【{case.case_number}】最終弁論生成中にエラーが発生しました: {e}\n再度 /次へ をお試しください。"

        await db.update_case_phase(case_id, CriminalPhase.DEFENSE_CLOSING)
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

    # Phase prerequisite: must have had at least defense closing or final statement
    required_phases = (
        CriminalPhase.FINAL_STATEMENT, CriminalPhase.DEFENSE_CLOSING,
    )
    if case.phase not in required_phases:
        return (
            f"❌ 判決の前に論告求刑・最終弁論・最終陳述が必要です（現在: {case.phase}）。\n"
            f"/次へ でフェーズを進めてください。"
        )

    logs = await db.get_case_logs(case_id)
    evidences = await db.get_evidence(case_id)
    precedents = await db.search_precedents(case.complaint_text[:100])

    try:
        verdict_text, verdict_data = await ai_engine.judge_render_verdict(
            case, _format_logs(logs), _format_evidence(evidences), _format_precedents(precedents)
        )
    except AIResponseError as e:
        return f"❌ 【{case.case_number}】判決生成中にエラーが発生しました: {e}\n再度 /判決 をお試しください。"

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

    # Guide user to the correct appeal command based on court level
    if case.court_level == CourtLevel.HIGH:
        appeal_guide = "/上告 [理由] で上告できます。"
    elif case.court_level == CourtLevel.SUPREME:
        appeal_guide = "最高裁判所の判決は最終判決です。"
    else:
        appeal_guide = "/控訴 で控訴できます。"

    return (
        f"⚖️ 【{case.case_number}】判決\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"{verdict_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"控訴期限: {deadline_str}\n"
        f"{appeal_guide}"
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
    now_utc = datetime.now(timezone.utc)
    deadline = case.appeal_deadline
    if deadline and deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    if deadline and now_utc > deadline:
        return f"❌ 控訴期限（{APPEAL_DEADLINE_DAYS}日）を過ぎています。"

    # Determine next court level
    if case.court_level == CourtLevel.DISTRICT:
        next_level = CourtLevel.HIGH
    elif case.court_level == CourtLevel.HIGH:
        return "❌ 高等裁判所の判決に対しては /上告 をご利用ください（上告理由が必要です）。"
    else:
        return "❌ 最高裁判所の判決に対しては控訴できません。"

    # Mark original case as appealed
    await db.update_case_status(case_id, CaseStatus.APPEALED)
    closed_phase = CivilPhase.CLOSED if case.case_type == CaseType.CIVIL else CriminalPhase.CLOSED
    await db.update_case_phase(case_id, closed_phase)

    # Create new case in higher court — start from issue organization (civil) or evidence (criminal)
    # since lower court findings carry over
    if case.case_type == CaseType.CIVIL:
        initial_phase = CivilPhase.ISSUE_ORGANIZATION
    else:
        initial_phase = CriminalPhase.EVIDENCE_EXAMINATION

    appeal_text = (
        f"【控訴審】原審事件番号: {case.case_number}\n"
        f"原審判決: {case.verdict_text or '(判決文なし)'}\n\n"
        f"控訴理由: 原審判決に不服があるため控訴する。"
    )

    new_case = await db.create_case(
        case_type=case.case_type,
        court_level=next_level,
        phase=initial_phase,
        plaintiff_id=case.plaintiff_id,
        defendant_id=case.defendant_id,
        group_id=case.group_id,
        complaint_text=appeal_text,
        parent_case_id=case.id,
    )

    # AI reviews the appeal
    new_case.phase = initial_phase
    try:
        review = await ai_engine.judge_review_complaint(new_case)
    except AIResponseError:
        review = "（受理審査の生成に失敗しました。手続きは進行可能です。）"

    await db.add_case_log(new_case.id, initial_phase, "JUDGE", review)

    court_name = "高等裁判所" if next_level == CourtLevel.HIGH else "最高裁判所"
    if case.case_type == CaseType.CIVIL:
        next_action = "/弁論 で控訴理由の弁論、/証拠 で新証拠を提出、/判決 で判決を求められます。"
    else:
        next_action = "/証拠 で新証拠を提出、/弁論 で主張を追加、/次へ でフェーズを進められます。"

    return (
        f"⚖️ 【控訴受理】\n"
        f"新事件番号: {new_case.case_number}\n"
        f"審級: {court_name}\n"
        f"原審事件番号: {case.case_number}\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 【控訴審受理審査】\n{review}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"原審の記録を引き継いで審理を行います。\n"
        f"{next_action}"
    )


async def _auto_close_expired_summary(case: CaseRecord) -> bool:
    """Auto-close summary order cases whose formal trial deadline has expired. Returns True if closed."""
    if case.phase != CriminalPhase.SUMMARY_ORDER or case.status != CaseStatus.ACTIVE:
        return False
    now_utc = datetime.now(timezone.utc)
    deadline = case.appeal_deadline
    if deadline and deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    if deadline and now_utc > deadline:
        await db.update_case_phase(case.id, CriminalPhase.CLOSED)
        await db.update_case_status(case.id, CaseStatus.CLOSED)
        await db.add_case_log(case.id, CriminalPhase.CLOSED, "SYSTEM",
                              "正式裁判請求期限が経過したため、略式命令が確定しました。")
        return True
    return False


async def get_case_list(group_id: str) -> str:
    """Get a list of cases for a group."""
    cases = await db.get_active_cases(group_id)
    # Auto-close expired summary orders
    for c in cases:
        await _auto_close_expired_summary(c)
    # Re-fetch after possible auto-close
    cases = await db.get_active_cases(group_id)
    if not cases:
        return "📋 現在進行中の事件はありません。"

    lines = ["📋 【事件一覧】\n━━━━━━━━━━━━━━━━━━"]
    for c in cases:
        case_type = "民事" if c.case_type == CaseType.CIVIL else "刑事"
        lines.append(f"\n📌 {c.case_number} ({case_type})")
        lines.append(f"   フェーズ: {get_phase_display(c.phase)}")
        lines.append(f"   状態: {c.status}")
    return "\n".join(lines)


async def get_case_detail(case_id: int) -> str:
    """Get detailed information about a case."""
    case = await db.get_case(case_id)
    if not case:
        return "❌ 事件が見つかりません。"

    # Auto-close expired summary orders
    if await _auto_close_expired_summary(case):
        case = await db.get_case(case_id)

    case_type = "民事" if case.case_type == CaseType.CIVIL else "刑事"
    court = {"SUMMARY": "簡易裁判所", "DISTRICT": "地方裁判所", "HIGH": "高等裁判所", "SUPREME": "最高裁判所"}.get(case.court_level, case.court_level)

    detail = (
        f"⚖️ 【事件詳細】{case.case_number}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"種別: {case_type} | 審級: {court}\n"
        f"フェーズ: {get_phase_display(case.phase)}\n"
        f"状態: {case.status}\n"
        f"提訴日: {case.created_at.astimezone(JST).strftime('%Y年%m月%d日 %H:%M') if case.created_at else '不明'}\n"
    )

    if case.verdict_text:
        detail += f"\n📋 【判決】\n{case.verdict_text[:500]}"
        if case.sentence:
            detail += f"\n量刑: {case.sentence}"
        if case.appeal_deadline:
            detail += f"\n控訴期限: {_format_appeal_deadline(case.appeal_deadline)}"

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


# =============================================================================
# Summary Trial (略式裁判)
# =============================================================================

async def request_summary_prosecution(case_id: int) -> str:
    """Prosecutor requests summary prosecution (略式起訴)."""
    case = await db.get_case(case_id)
    if not case:
        return "❌ 事件が見つかりません。"
    if case.case_type != CaseType.CRIMINAL:
        return "❌ 略式手続は刑事事件のみ利用できます。"
    if case.phase != CriminalPhase.PROSECUTION_DECISION:
        return "❌ 起訴判断段階でのみ略式起訴を請求できます。"

    logs = await db.get_case_logs(case_id)
    investigation_log = ""
    for log in logs:
        if log["actor"] == "PROSECUTOR":
            investigation_log = log["content"]

    # AI checks summary eligibility
    try:
        response, data = await ai_engine.prosecutor_summary_request(case, investigation_log)
    except AIResponseError as e:
        return f"❌ 【{case.case_number}】略式起訴検討中にエラー: {e}"

    await db.add_case_log(case_id, CriminalPhase.PROSECUTION_DECISION, "PROSECUTOR", response,
                          action_type="SUMMARY_REQUEST")

    # Check if AI determined the case is NOT eligible for summary
    if data and data.get("summary_eligible") is False:
        return (
            f"⚖️ 【{case.case_number}】略式手続不適格\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📋 【検察官の検討結果】\n{response}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"本件は略式手続の要件を満たしません。\n"
            f"/次へ で通常の起訴手続きに進めてください。"
        )

    # Transition to SUMMARY_CONSENT — awaiting defendant consent
    await db.update_case_phase(case_id, CriminalPhase.SUMMARY_CONSENT)
    await db.update_case_court_level(case_id, CourtLevel.SUMMARY)

    return (
        f"⚖️ 【{case.case_number}】略式命令請求\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 【検察官の略式請求】\n{response}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"被疑者は /略式同意 または /略式拒否 で応答してください。\n"
        f"（略式手続には被疑者の同意が必要です — 刑事訴訟法461条の2）"
    )


async def consent_summary(case_id: int, defendant_id: str) -> str:
    """Defendant consents to summary procedure."""
    case = await db.get_case(case_id)
    if not case:
        return "❌ 事件が見つかりません。"
    if case.defendant_id != defendant_id:
        return "❌ あなたはこの事件の被疑者ではありません。"
    if case.phase != CriminalPhase.SUMMARY_CONSENT:
        return "❌ 現在のフェーズでは略式同意を行えません。"

    await db.add_case_log(case_id, CriminalPhase.SUMMARY_CONSENT, defendant_id,
                          "被疑者は略式手続に同意した。", action_type="SUMMARY_CONSENT_ACCEPT")

    # AI judge renders summary order
    logs = await db.get_case_logs(case_id)
    logs_text = _format_logs(logs)

    try:
        order_text, verdict_data = await ai_engine.judge_render_summary_order(case, logs_text)
    except AIResponseError as e:
        return f"❌ 【{case.case_number}】略式命令生成中にエラー: {e}"

    await db.update_case_phase(case_id, CriminalPhase.SUMMARY_ORDER)
    await db.add_case_log(case_id, CriminalPhase.SUMMARY_ORDER, "JUDGE", order_text)

    # Record verdict
    sentence = verdict_data.get("sentence", "") if verdict_data else ""
    summary = verdict_data.get("summary", "") if verdict_data else ""
    await db.update_case_verdict(case_id, order_text, sentence)

    # Save as precedent
    try:
        await db.save_precedent(
            case.case_number, case.case_type,
            summary or "略式命令", Verdict.SUMMARY_FINE, sentence,
            order_text[:1000],
        )
    except Exception as e:
        logger.warning(f"Failed to save precedent: {e}")

    # Re-fetch case to get updated appeal_deadline
    updated_case = await db.get_case(case_id)
    deadline = _format_appeal_deadline(updated_case.appeal_deadline if updated_case else None)

    return (
        f"⚖️ 【{case.case_number}】略式命令\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 【書面審理結果】\n{order_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"不服がある場合は14日以内に /正式裁判 で正式裁判を請求できます。\n"
        f"正式裁判請求期限: {deadline}"
    )


async def reject_summary(case_id: int, defendant_id: str) -> str:
    """Defendant rejects summary procedure — case proceeds to formal trial."""
    case = await db.get_case(case_id)
    if not case:
        return "❌ 事件が見つかりません。"
    if case.defendant_id != defendant_id:
        return "❌ あなたはこの事件の被疑者ではありません。"
    if case.phase != CriminalPhase.SUMMARY_CONSENT:
        return "❌ 現在のフェーズでは略式拒否を行えません。"

    await db.add_case_log(case_id, CriminalPhase.SUMMARY_CONSENT, defendant_id,
                          "被疑者は略式手続を拒否した。", action_type="SUMMARY_CONSENT_REJECT")

    # Revert to DISTRICT court and proceed to formal trial
    await db.update_case_court_level(case_id, CourtLevel.DISTRICT)

    # Proceed to rights notification
    case.phase = CriminalPhase.RIGHTS_NOTIFICATION
    try:
        rights = await ai_engine.defense_rights_notification(case)
    except AIResponseError:
        rights = "（権利告知の生成に失敗しました。被告人には黙秘権等の権利があります。）"

    await db.update_case_phase(case_id, CriminalPhase.RIGHTS_NOTIFICATION)
    await db.add_case_log(case_id, CriminalPhase.RIGHTS_NOTIFICATION, "DEFENSE", rights)

    return (
        f"⚖️ 【{case.case_number}】略式手続拒否 → 通常裁判へ移行\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"被疑者が略式手続を拒否したため、通常の刑事裁判手続に移行します。\n\n"
        f"🛡️ 【弁護人による権利告知】\n{rights}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"被告人は /罪状認否 認める または /罪状認否 否認 で応答してください。"
    )


async def request_formal_trial(case_id: int, appellant_id: str) -> str:
    """Request formal trial after summary order (略式命令後の正式裁判請求)."""
    case = await db.get_case(case_id)
    if not case:
        return "❌ 事件が見つかりません。"
    if appellant_id not in (case.plaintiff_id, case.defendant_id):
        return "❌ あなたはこの事件の当事者ではありません。"
    if case.phase != CriminalPhase.SUMMARY_ORDER:
        return "❌ 略式命令が出されていない事件には正式裁判を請求できません。"

    # Check deadline (14 days)
    now_utc = datetime.now(timezone.utc)
    deadline = case.appeal_deadline
    if deadline and deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    if deadline and now_utc > deadline:
        return f"❌ 正式裁判請求期限（{APPEAL_DEADLINE_DAYS}日）を過ぎています。"

    # Mark summary case as objected
    await db.update_case_phase(case_id, CriminalPhase.SUMMARY_OBJECTION)
    await db.update_case_status(case_id, CaseStatus.CLOSED)
    await db.add_case_log(case_id, CriminalPhase.SUMMARY_OBJECTION, appellant_id,
                          "正式裁判が請求された。", action_type="FORMAL_TRIAL_REQUEST")

    # Create new case at DISTRICT level starting from RIGHTS_NOTIFICATION
    formal_text = (
        f"【正式裁判】略式命令に対する正式裁判請求\n"
        f"原事件番号: {case.case_number}\n"
        f"略式命令内容: {case.verdict_text or '（略式命令文なし）'}\n\n"
        f"正式裁判を請求する。"
    )

    new_case = await db.create_case(
        case_type=case.case_type,
        court_level=CourtLevel.DISTRICT,
        phase=CriminalPhase.RIGHTS_NOTIFICATION,
        plaintiff_id=case.plaintiff_id,
        defendant_id=case.defendant_id,
        group_id=case.group_id,
        complaint_text=formal_text,
        parent_case_id=case.id,
        case_subtype="FORMAL_FROM_SUMMARY",
    )

    await db.add_case_log(new_case.id, CriminalPhase.RIGHTS_NOTIFICATION, "SYSTEM",
                          f"略式命令（{case.case_number}）に対する正式裁判請求により新規開廷。")

    # Generate rights notification
    new_case.phase = CriminalPhase.RIGHTS_NOTIFICATION
    try:
        rights = await ai_engine.defense_rights_notification(new_case)
    except AIResponseError:
        rights = "（権利告知の生成に失敗しました。被告人には黙秘権等の権利があります。）"

    await db.add_case_log(new_case.id, CriminalPhase.RIGHTS_NOTIFICATION, "DEFENSE", rights)

    return (
        f"⚖️ 【正式裁判請求受理】\n"
        f"新事件番号: {new_case.case_number}\n"
        f"審級: 地方裁判所\n"
        f"原略式事件: {case.case_number}\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"略式命令に対する正式裁判請求が受理されました。\n"
        f"通常の刑事裁判手続きを開始します。\n\n"
        f"🛡️ 【弁護人による権利告知】\n{rights}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"被告人は /罪状認否 認める または /罪状認否 否認 で応答してください。"
    )


# =============================================================================
# Jokoku Appeal (上告)
# =============================================================================

async def file_jokoku_appeal(case_id: int, appellant_id: str, reason: str) -> str:
    """File a jokoku appeal to the Supreme Court (上告)."""
    case = await db.get_case(case_id)
    if not case:
        return "❌ 事件が見つかりません。"
    if appellant_id not in (case.plaintiff_id, case.defendant_id):
        return "❌ あなたはこの事件の当事者ではありません。"

    verdict_phase = CivilPhase.VERDICT if case.case_type == CaseType.CIVIL else CriminalPhase.VERDICT
    if case.phase != verdict_phase:
        return "❌ 判決が出ていない事件には上告できません。"

    if case.court_level != CourtLevel.HIGH:
        return "❌ 上告は高等裁判所の判決に対してのみ可能です。地裁判決には /控訴 をご利用ください。"

    # Check appeal deadline
    now_utc = datetime.now(timezone.utc)
    deadline = case.appeal_deadline
    if deadline and deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    if deadline and now_utc > deadline:
        return f"❌ 上告期限（{APPEAL_DEADLINE_DAYS}日）を過ぎています。"

    if not reason.strip():
        return "❌ 上告理由を記載してください。\n使い方: /上告 上告理由（憲法違反・判例違反等）"

    closed_phase = CivilPhase.CLOSED if case.case_type == CaseType.CIVIL else CriminalPhase.CLOSED

    # Create new case in Supreme Court
    if case.case_type == CaseType.CIVIL:
        initial_phase = CivilPhase.ORAL_ARGUMENT
    else:
        initial_phase = CriminalPhase.EVIDENCE_EXAMINATION

    jokoku_text = (
        f"【上告審】原審事件番号: {case.case_number}\n"
        f"原審判決: {case.verdict_text or '(判決文なし)'}\n\n"
        f"上告理由: {reason}"
    )

    # AI reviews the jokoku appeal BEFORE changing status or creating new case
    temp_case = CaseRecord(
        case_number="(上告審査中)",
        case_type=case.case_type,
        court_level=CourtLevel.SUPREME,
        phase=initial_phase,
        plaintiff_id=case.plaintiff_id,
        defendant_id=case.defendant_id,
        group_id=case.group_id,
        complaint_text=jokoku_text,
    )

    try:
        review, is_accepted = await ai_engine.judge_review_jokoku(
            temp_case, case.verdict_text or "", reason
        )
    except AIResponseError:
        review = "（上告審受理審査の生成に失敗しました。手続きは進行可能です。）"
        is_accepted = True  # Fail-open: allow appeal if AI fails

    if not is_accepted:
        # Do not create new case — log the rejection on the original case
        await db.add_case_log(case.id, "JOKOKU_REVIEW", "JUDGE", review,
                              action_type="JOKOKU_REJECTED")
        return (
            f"⚖️ 【上告不受理】\n"
            f"対象事件: {case.case_number}\n"
            f"上告理由: {reason}\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📋 【上告審受理審査】\n{review}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"上告理由が法定の上告理由に該当しないため、上告は不受理となりました。"
        )

    # Accepted — mark original case as appealed, then create new case
    await db.update_case_status(case_id, CaseStatus.JOKOKU_APPEALED)
    await db.update_case_phase(case_id, closed_phase)

    new_case = await db.create_case(
        case_type=case.case_type,
        court_level=CourtLevel.SUPREME,
        phase=initial_phase,
        plaintiff_id=case.plaintiff_id,
        defendant_id=case.defendant_id,
        group_id=case.group_id,
        complaint_text=jokoku_text,
        parent_case_id=case.id,
        case_subtype="JOKOKU",
    )

    await db.add_case_log(new_case.id, initial_phase, "JUDGE", review)

    if case.case_type == CaseType.CIVIL:
        next_action = "/弁論 で上告理由の弁論、/証拠 で新証拠を提出、/判決 で判決を求められます。"
    else:
        next_action = "/証拠 で新証拠を提出、/弁論 で主張を追加、/次へ でフェーズを進められます。"

    return (
        f"⚖️ 【上告受理】\n"
        f"新事件番号: {new_case.case_number}\n"
        f"審級: 最高裁判所\n"
        f"原審事件番号: {case.case_number}\n"
        f"上告理由: {reason}\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 【上告審受理審査】\n{review}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"上告審は法律審であり、原審の事実認定は原則として尊重されます。\n"
        f"{next_action}"
    )


# =============================================================================
# Retrial (再審)
# =============================================================================

async def file_retrial(original_case_number: str, group_id: str, petitioner_id: str, reason: str) -> str:
    """File a retrial request (再審請求)."""
    if not reason.strip():
        return "❌ 再審事由を記載してください。\n使い方: /再審 事件番号 再審事由"

    # Find the original case (may be CLOSED)
    case = await db.get_case_by_number(original_case_number)
    if not case:
        return f"❌ 事件番号 {original_case_number} が見つかりません。"
    if case.group_id != group_id:
        return f"❌ 事件 {original_case_number} はこのグループの事件ではありません。"
    if petitioner_id not in (case.plaintiff_id, case.defendant_id):
        return "❌ あなたはこの事件の当事者ではありません。"
    if case.status not in (CaseStatus.CLOSED, CaseStatus.APPEALED, CaseStatus.JOKOKU_APPEALED):
        return "❌ 再審は確定判決（終結済みの事件）に対してのみ請求できます。"

    # Check retrial deadline (365 days from case closure for this simulation)
    if case.updated_at:
        closed_at = case.updated_at
        if closed_at.tzinfo is None:
            closed_at = closed_at.replace(tzinfo=timezone.utc)
        retrial_deadline = closed_at + timedelta(days=365)
        if datetime.now(timezone.utc) > retrial_deadline:
            return "❌ 再審請求の期限（判決確定から1年）を過ぎています。"

    # AI judge reviews the retrial request
    try:
        review_text, is_accepted = await ai_engine.judge_review_retrial(case, reason)
    except AIResponseError as e:
        return f"❌ 再審事由の審査中にエラーが発生しました: {e}"

    await db.add_case_log(case.id, "RETRIAL_REQUEST", "JUDGE", review_text,
                          action_type="RETRIAL_REVIEW")

    if not is_accepted:
        return (
            f"⚖️ 【再審請求棄却】\n"
            f"対象事件: {case.case_number}\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📋 【再審事由審査結果】\n{review_text}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"再審事由に該当しないため、再審請求は棄却されました。"
        )

    # Retrial accepted — create new case at the same court level
    await db.update_case_status(case.id, CaseStatus.RETRIAL)

    if case.case_type == CaseType.CIVIL:
        initial_phase = CivilPhase.ISSUE_ORGANIZATION
    else:
        initial_phase = CriminalPhase.EVIDENCE_EXAMINATION

    retrial_text = (
        f"【再審】原審事件番号: {case.case_number}\n"
        f"原審判決: {case.verdict_text or '(判決文なし)'}\n\n"
        f"再審事由: {reason}"
    )

    court_level = case.court_level
    # If original was at SUMMARY, retry at DISTRICT
    if court_level == CourtLevel.SUMMARY:
        court_level = CourtLevel.DISTRICT

    new_case = await db.create_case(
        case_type=case.case_type,
        court_level=court_level,
        phase=initial_phase,
        plaintiff_id=case.plaintiff_id,
        defendant_id=case.defendant_id,
        group_id=case.group_id,
        complaint_text=retrial_text,
        parent_case_id=case.id,
        case_subtype="RETRIAL",
        is_retrial=True,
    )

    # Copy original case logs to retrial for AI context
    original_logs = await db.get_case_logs(case.id)
    logs_summary = _format_logs(original_logs)
    await db.add_case_log(new_case.id, initial_phase, "SYSTEM",
                          f"再審開始決定。原審事件番号: {case.case_number}\n\n【原審記録の要旨】\n{logs_summary[:3000]}")

    court_name = {
        "DISTRICT": "地方裁判所", "HIGH": "高等裁判所", "SUPREME": "最高裁判所"
    }.get(court_level, court_level)

    if case.case_type == CaseType.CIVIL:
        next_action = "/弁論 で主張を追加、/証拠 で新証拠を提出してください。"
    else:
        next_action = "/証拠 で新証拠を提出、/弁論 で主張を追加してください。"

    return (
        f"⚖️ 【再審開始決定】\n"
        f"新事件番号: {new_case.case_number}\n"
        f"審級: {court_name}\n"
        f"原審事件番号: {case.case_number}\n"
        f"再審事由: {reason}\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 【再審事由審査結果】\n{review_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"原審記録を引き継いで再審理を行います。\n"
        f"{next_action}"
    )


# =============================================================================
# Kokoku Appeal (抗告)
# =============================================================================

async def file_kokoku(original_case_number: str, group_id: str, appellant_id: str, reason: str) -> str:
    """File a kokoku appeal against a settlement decision (抗告)."""
    if not reason.strip():
        return "❌ 抗告理由を記載してください。\n使い方: /抗告 事件番号 抗告理由"

    # Find the original case (must be SETTLED)
    case = await db.get_case_by_number(original_case_number)
    if not case:
        return f"❌ 事件番号 {original_case_number} が見つかりません。"
    if case.group_id != group_id:
        return f"❌ 事件 {original_case_number} はこのグループの事件ではありません。"
    if appellant_id not in (case.plaintiff_id, case.defendant_id):
        return "❌ あなたはこの事件の当事者ではありません。"
    if case.status != CaseStatus.SETTLED:
        return "❌ 抗告は和解決定（和解成立済の事件）に対してのみ可能です。"

    # Check deadline from settlement timestamp (not updated_at which changes on any DB update)
    now_utc = datetime.now(timezone.utc)
    original_logs = await db.get_case_logs(case.id)
    settled_at = None
    for log in reversed(original_logs):
        if log.get("action_type") == "SETTLEMENT_ACCEPT":
            ts = log.get("created_at")
            if isinstance(ts, datetime):
                settled_at = ts
                break
    if settled_at is None:
        settled_at = case.updated_at  # fallback
    if settled_at:
        if settled_at.tzinfo is None:
            settled_at = settled_at.replace(tzinfo=timezone.utc)
        kokoku_deadline = settled_at + timedelta(days=KOKOKU_DEADLINE_DAYS)
        if now_utc > kokoku_deadline:
            return f"❌ 即時抗告の期限（{KOKOKU_DEADLINE_DAYS}日）を過ぎています。"

    # Get settlement content from logs for context
    settlement_content = ""
    original_logs = await db.get_case_logs(case.id)
    for log in original_logs:
        if log.get("actor") == "JUDGE" and log.get("phase") == "SETTLEMENT_PROPOSED":
            settlement_content = log["content"]
            break

    # AI judge reviews the kokoku appeal
    try:
        review_text, is_accepted = await ai_engine.judge_review_kokoku(case, reason, settlement_content)
    except AIResponseError as e:
        return f"❌ 抗告理由の審査中にエラーが発生しました: {e}"

    await db.add_case_log(case.id, "KOKOKU_REQUEST", "JUDGE", review_text,
                          action_type="KOKOKU_REVIEW")

    if not is_accepted:
        return (
            f"⚖️ 【抗告棄却】\n"
            f"対象事件: {case.case_number}\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📋 【抗告審査結果】\n{review_text}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"抗告理由に該当しないため、抗告は棄却されました。"
        )

    # Kokoku accepted — create new case in higher court
    await db.update_case_status(case.id, CaseStatus.KOKOKU_APPEALED)

    # Determine next court level
    if case.court_level == CourtLevel.DISTRICT:
        next_level = CourtLevel.HIGH
    elif case.court_level == CourtLevel.HIGH:
        next_level = CourtLevel.SUPREME
    elif case.court_level == CourtLevel.SUPREME:
        return "❌ 最高裁判所の和解決定に対してはこれ以上の抗告はできません。"
    else:
        next_level = CourtLevel.HIGH

    initial_phase = CivilPhase.ORAL_ARGUMENT if case.case_type == CaseType.CIVIL else CriminalPhase.EVIDENCE_EXAMINATION

    kokoku_text = (
        f"【抗告審】原審事件番号: {case.case_number}\n"
        f"和解内容: {case.verdict_text or case.complaint_text}\n\n"
        f"抗告理由: {reason}"
    )

    new_case = await db.create_case(
        case_type=case.case_type,
        court_level=next_level,
        phase=initial_phase,
        plaintiff_id=case.plaintiff_id,
        defendant_id=case.defendant_id,
        group_id=case.group_id,
        complaint_text=kokoku_text,
        parent_case_id=case.id,
        case_subtype="KOKOKU",
    )

    await db.add_case_log(new_case.id, initial_phase, "SYSTEM",
                          f"抗告認容。原審事件番号: {case.case_number}")

    # AI reviews the new case for consistency with appeal/jokoku pattern
    new_case.phase = initial_phase
    try:
        appeal_review = await ai_engine.judge_review_complaint(new_case)
    except AIResponseError:
        appeal_review = "（抗告審受理審査の生成に失敗しました。手続きは進行可能です。）"
    await db.add_case_log(new_case.id, initial_phase, "JUDGE", appeal_review)

    court_name = {
        "DISTRICT": "地方裁判所", "HIGH": "高等裁判所", "SUPREME": "最高裁判所"
    }.get(next_level, next_level)

    return (
        f"⚖️ 【抗告認容】\n"
        f"新事件番号: {new_case.case_number}\n"
        f"審級: {court_name}（抗告審）\n"
        f"原審事件番号: {case.case_number}\n"
        f"抗告理由: {reason}\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 【抗告審査結果】\n{review_text}\n\n"
        f"📋 【抗告審受理審査】\n{appeal_review}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"和解決定が取り消され、上級審にて審理を再開します。\n"
        f"/弁論 で主張を追加してください。"
    )
