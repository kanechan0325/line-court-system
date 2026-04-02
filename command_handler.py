"""Command parsing and routing for LINE messages."""

import re
import logging
from typing import Optional

import database as db
import case_manager
from models import CaseType, CourtLevel, CivilPhase, CriminalPhase, get_phase_display
from line_client import resolve_mention

logger = logging.getLogger(__name__)

# Case number pattern: R8-(ワ)-001 etc.
CASE_NUMBER_PATTERN = re.compile(r"R8-\([ワネオわうあい][再]?\)-\d{3}")


def parse_command(text: str) -> tuple[str, str]:
    """Parse a command string into (command, args)."""
    text = text.strip()
    if not text.startswith("/"):
        return ("", text)

    parts = text.split(None, 1)
    command = parts[0]
    args = parts[1] if len(parts) > 1 else ""
    return (command, args)


def extract_case_number(args: str) -> tuple[Optional[str], str]:
    """Extract a case number from args if present. Returns (case_number, remaining_args)."""
    match = CASE_NUMBER_PATTERN.search(args)
    if match:
        case_number = match.group(0)
        remaining = args[:match.start()] + args[match.end():]
        return case_number, remaining.strip()
    return None, args


async def resolve_case(group_id: str, args: str) -> tuple[Optional[db.CaseRecord], str, Optional[str]]:
    """Resolve which case to operate on.

    Returns (case, remaining_args, error_message).
    If error_message is not None, case is None.
    """
    case_number, remaining_args = extract_case_number(args)

    if case_number:
        case = await db.get_case_by_number(case_number)
        if not case:
            return None, args, f"❌ 事件番号 {case_number} が見つかりません。"
        if case.group_id != group_id:
            return None, args, f"❌ 事件 {case_number} はこのグループの事件ではありません。"
        return case, remaining_args, None

    # No case number specified — auto-select
    cases = await db.get_active_cases(group_id)
    if not cases:
        return None, args, "❌ 進行中の事件がありません。"
    if len(cases) == 1:
        return cases[0], args, None

    # Multiple active cases — require explicit selection
    case_list = "\n".join(f"  📌 {c.case_number} ({get_phase_display(c.phase)})" for c in cases)
    return None, args, (
        f"❌ 複数の事件が進行中です。事件番号を指定してください。\n"
        f"━━━━━━━━━━━━━━━━━━\n{case_list}\n\n"
        f"例: /弁論 R8-(ワ)-001 弁論の内容"
    )


def check_party(case, user_id: str) -> Optional[str]:
    """Check if user is a party to the case. Returns error message or None."""
    if user_id not in (case.plaintiff_id, case.defendant_id):
        return "❌ あなたはこの事件の当事者ではありません。"
    return None


async def handle_command(
    event: dict, group_id: str, user_id: str, reply_token: str
) -> Optional[str]:
    """Route a command to the appropriate handler. Returns response text or None."""
    text = event.get("message", {}).get("text", "").strip()
    if not text.startswith("/"):
        return None

    command, args = parse_command(text)

    try:
        if command == "/訴状":
            return await handle_civil_complaint(event, group_id, user_id, args)
        elif command == "/告訴":
            return await handle_criminal_complaint(event, group_id, user_id, args)
        elif command == "/答弁":
            return await handle_answer(group_id, user_id, args)
        elif command == "/弁論":
            return await handle_argument(group_id, user_id, args)
        elif command == "/証拠":
            return await handle_evidence(group_id, user_id, args)
        elif command == "/和解":
            return await handle_settlement_proposal(group_id, user_id, args)
        elif command == "/和解同意":
            return await handle_settlement_accept(group_id, user_id, args)
        elif command == "/和解拒否":
            return await handle_settlement_reject(group_id, user_id, args)
        elif command == "/起訴判断":
            return await handle_prosecution_decision(group_id, user_id, args)
        elif command == "/罪状認否":
            return await handle_arraignment(group_id, user_id, args)
        elif command == "/次へ":
            return await handle_next_phase(group_id, user_id, args)
        elif command == "/最終陳述":
            return await handle_final_statement(group_id, user_id, args)
        elif command == "/判決":
            return await handle_verdict(group_id, args)
        elif command == "/控訴":
            return await handle_appeal(group_id, user_id, args)
        elif command == "/略式起訴":
            return await handle_summary_prosecution(group_id, user_id, args)
        elif command == "/略式同意":
            return await handle_summary_consent(group_id, user_id, args)
        elif command == "/略式拒否":
            return await handle_summary_reject(group_id, user_id, args)
        elif command == "/正式裁判":
            return await handle_formal_trial_request(group_id, user_id, args)
        elif command == "/上告":
            return await handle_jokoku_appeal(group_id, user_id, args)
        elif command == "/再審":
            return await handle_retrial(group_id, user_id, args)
        elif command == "/抗告":
            return await handle_kokoku(group_id, user_id, args)
        elif command == "/事件一覧":
            return await handle_case_list(group_id)
        elif command == "/事件詳細":
            return await handle_case_detail(group_id, args)
        elif command == "/判例":
            return await handle_precedent_search(args)
        elif command == "/ヘルプ":
            return handle_help()
        else:
            return f"❌ 不明なコマンドです: {command}\n/ヘルプ でコマンド一覧を確認できます。"
    except Exception as e:
        logger.error(f"Command handling error: {e}", exc_info=True)
        return f"❌ エラーが発生しました: {str(e)}"


# --- Command Handlers ---

async def handle_civil_complaint(
    event: dict, group_id: str, user_id: str, args: str
) -> str:
    """Handle /訴状 @被告 内容"""
    defendant_id = resolve_mention(event)
    if not defendant_id:
        return "❌ 被告をメンション(@)で指定してください。\n使い方: /訴状 @被告 訴状の内容"

    complaint_text = args
    mention = event.get("message", {}).get("mention", {})
    mentionees = mention.get("mentionees", [])
    if mentionees:
        for m in mentionees:
            display = m.get("text", "")
            if display and display in complaint_text:
                complaint_text = complaint_text.replace(display, "", 1).strip()

    if not complaint_text:
        return "❌ 訴状の内容を記載してください。\n使い方: /訴状 @被告 訴状の内容"

    if defendant_id == user_id:
        return "❌ 自分自身を訴えることはできません。"

    return await case_manager.file_civil_complaint(
        group_id, user_id, defendant_id, complaint_text
    )


async def handle_criminal_complaint(
    event: dict, group_id: str, user_id: str, args: str
) -> str:
    """Handle /告訴 @被疑者 内容"""
    suspect_id = resolve_mention(event)
    if not suspect_id:
        return "❌ 被疑者をメンション(@)で指定してください。\n使い方: /告訴 @被疑者 告訴の内容"

    complaint_text = args
    mention = event.get("message", {}).get("mention", {})
    mentionees = mention.get("mentionees", [])
    if mentionees:
        for m in mentionees:
            display = m.get("text", "")
            if display and display in complaint_text:
                complaint_text = complaint_text.replace(display, "", 1).strip()

    if not complaint_text:
        return "❌ 告訴の内容を記載してください。\n使い方: /告訴 @被疑者 告訴の内容"

    if suspect_id == user_id:
        return "❌ 自分自身を告訴することはできません。"

    return await case_manager.file_criminal_complaint(
        group_id, user_id, suspect_id, complaint_text
    )


async def handle_answer(group_id: str, user_id: str, args: str) -> str:
    """Handle /答弁 [事件番号] 内容 — defendant only"""
    case, remaining, err = await resolve_case(group_id, args)
    if err:
        return err

    if not remaining:
        return "❌ 答弁の内容を記載してください。\n使い方: /答弁 答弁の内容"

    party_err = check_party(case, user_id)
    if party_err:
        return party_err

    return await case_manager.submit_answer(case.id, user_id, remaining)


async def handle_argument(group_id: str, user_id: str, args: str) -> str:
    """Handle /弁論 [事件番号] 内容 — parties only"""
    case, remaining, err = await resolve_case(group_id, args)
    if err:
        return err

    if not remaining:
        return "❌ 弁論の内容を記載してください。\n使い方: /弁論 弁論の内容"

    party_err = check_party(case, user_id)
    if party_err:
        return party_err

    return await case_manager.submit_argument(case.id, user_id, remaining)


async def handle_evidence(group_id: str, user_id: str, args: str) -> str:
    """Handle /証拠 [事件番号] 内容 — parties only"""
    case, remaining, err = await resolve_case(group_id, args)
    if err:
        return err

    if not remaining:
        return "❌ 証拠の内容を記載してください。\n使い方: /証拠 証拠の説明"

    party_err = check_party(case, user_id)
    if party_err:
        return party_err

    return await case_manager.submit_evidence(case.id, user_id, remaining)


async def handle_settlement_proposal(group_id: str, user_id: str, args: str) -> str:
    """Handle /和解 [事件番号] — parties only"""
    case, _, err = await resolve_case(group_id, args)
    if err:
        return err

    party_err = check_party(case, user_id)
    if party_err:
        return party_err

    return await case_manager.propose_settlement(case.id)


async def handle_settlement_accept(group_id: str, user_id: str, args: str) -> str:
    """Handle /和解同意 [事件番号] — parties only"""
    case, _, err = await resolve_case(group_id, args)
    if err:
        return err

    return await case_manager.accept_settlement(case.id, user_id)


async def handle_settlement_reject(group_id: str, user_id: str, args: str) -> str:
    """Handle /和解拒否 [事件番号] — parties only"""
    case, _, err = await resolve_case(group_id, args)
    if err:
        return err

    return await case_manager.reject_settlement(case.id, user_id)


async def handle_prosecution_decision(group_id: str, user_id: str, args: str) -> str:
    """Handle /起訴判断 [事件番号] — parties only"""
    case, _, err = await resolve_case(group_id, args)
    if err:
        return err

    party_err = check_party(case, user_id)
    if party_err:
        return party_err

    return await case_manager.decide_prosecution(case.id)


async def handle_arraignment(group_id: str, user_id: str, args: str) -> str:
    """Handle /罪状認否 [事件番号] 認める|否認 — defendant only"""
    case, remaining, err = await resolve_case(group_id, args)
    if err:
        return err

    if not remaining:
        return "❌ 認否を入力してください。\n使い方: /罪状認否 認める または /罪状認否 否認"

    return await case_manager.arraignment(case.id, user_id, remaining)


async def handle_next_phase(group_id: str, user_id: str, args: str) -> str:
    """Handle /次へ [事件番号] — parties only"""
    case, _, err = await resolve_case(group_id, args)
    if err:
        return err

    party_err = check_party(case, user_id)
    if party_err:
        return party_err

    return await case_manager.proceed_criminal_phase(case.id)


async def handle_final_statement(group_id: str, user_id: str, args: str) -> str:
    """Handle /最終陳述 [事件番号] 内容 — defendant only"""
    case, remaining, err = await resolve_case(group_id, args)
    if err:
        return err

    if not remaining:
        return "❌ 最終陳述の内容を記載してください。\n使い方: /最終陳述 最終陳述の内容"

    return await case_manager.final_statement(case.id, user_id, remaining)


async def handle_verdict(group_id: str, args: str) -> str:
    """Handle /判決 [事件番号]"""
    case, _, err = await resolve_case(group_id, args)
    if err:
        return err

    if case.case_type == CaseType.CIVIL:
        return await case_manager.render_civil_verdict(case.id)
    else:
        return await case_manager.render_criminal_verdict(case.id)


async def handle_appeal(group_id: str, user_id: str, args: str) -> str:
    """Handle /控訴 [事件番号]"""
    # If case number specified, use it
    case_number, _ = extract_case_number(args)
    if case_number:
        case = await db.get_case_by_number(case_number)
        if not case:
            return f"❌ 事件番号 {case_number} が見つかりません。"
    else:
        # Look for cases with verdict
        cases = await db.get_active_cases(group_id)
        case = None
        for c in cases:
            if c.phase in (CivilPhase.VERDICT, CriminalPhase.VERDICT):
                case = c
                break
        if not case:
            return "❌ 控訴可能な判決がありません。"

    return await case_manager.file_appeal(case.id, user_id)


async def handle_case_list(group_id: str) -> str:
    """Handle /事件一覧"""
    return await case_manager.get_case_list(group_id)


async def handle_case_detail(group_id: str, args: str) -> str:
    """Handle /事件詳細 [事件番号]"""
    if args:
        case = await db.get_case_by_number(args.strip())
        if case:
            return await case_manager.get_case_detail(case.id)
        return f"❌ 事件番号 {args.strip()} が見つかりません。"

    case, _, err = await resolve_case(group_id, "")
    if err:
        return err
    return await case_manager.get_case_detail(case.id)


async def handle_precedent_search(args: str) -> str:
    """Handle /判例 [キーワード]"""
    if not args:
        precedents = await db.get_all_precedents()
        if not precedents:
            return "📚 判例データベースに判例がありません。"
        lines = ["📚 【判例一覧】\n━━━━━━━━━━━━━━━━━━"]
        for p in precedents:
            lines.append(f"\n📌 {p.case_number}: {p.summary}")
            lines.append(f"   判決: {p.verdict}")
        return "\n".join(lines)

    return await case_manager.search_precedents_cmd(args)


async def handle_summary_prosecution(group_id: str, user_id: str, args: str) -> str:
    """Handle /略式起訴 — accuser (plaintiff) only"""
    case, _, error = await resolve_case(group_id, args)
    if error:
        return error
    party_error = check_party(case, user_id)
    if party_error:
        return party_error
    if user_id == case.defendant_id:
        return "❌ 略式起訴は告訴人（原告側）のみが請求できます。"
    return await case_manager.request_summary_prosecution(case.id)


async def handle_summary_consent(group_id: str, user_id: str, args: str) -> str:
    """Handle /略式同意 — defendant only"""
    case, _, error = await resolve_case(group_id, args)
    if error:
        return error
    party_error = check_party(case, user_id)
    if party_error:
        return party_error
    return await case_manager.consent_summary(case.id, user_id)


async def handle_summary_reject(group_id: str, user_id: str, args: str) -> str:
    """Handle /略式拒否 — defendant only"""
    case, _, error = await resolve_case(group_id, args)
    if error:
        return error
    party_error = check_party(case, user_id)
    if party_error:
        return party_error
    return await case_manager.reject_summary(case.id, user_id)


async def handle_formal_trial_request(group_id: str, user_id: str, args: str) -> str:
    """Handle /正式裁判"""
    case, _, error = await resolve_case(group_id, args)
    if error:
        return error
    party_error = check_party(case, user_id)
    if party_error:
        return party_error
    return await case_manager.request_formal_trial(case.id, user_id)


async def handle_jokoku_appeal(group_id: str, user_id: str, args: str) -> str:
    """Handle /上告 [事件番号] 上告理由"""
    case_number, remaining = extract_case_number(args)
    if case_number:
        case = await db.get_case_by_number(case_number)
        if not case:
            return f"❌ 事件番号 {case_number} が見つかりません。"
    else:
        # Look for cases with verdict at HIGH court
        cases = await db.get_active_cases(group_id)
        case = None
        for c in cases:
            if c.phase in (CivilPhase.VERDICT, CriminalPhase.VERDICT) and c.court_level == CourtLevel.HIGH:
                case = c
                break
        if not case:
            return "❌ 上告可能な高裁判決がありません。"
        remaining = args

    return await case_manager.file_jokoku_appeal(case.id, user_id, remaining)


async def handle_retrial(group_id: str, user_id: str, args: str) -> str:
    """Handle /再審 事件番号 再審事由"""
    case_number, remaining = extract_case_number(args)
    if not case_number:
        return "❌ 再審請求には事件番号が必要です。\n使い方: /再審 R8-(わ)-001 再審事由"
    return await case_manager.file_retrial(case_number, group_id, user_id, remaining)


async def handle_kokoku(group_id: str, user_id: str, args: str) -> str:
    """Handle /抗告 事件番号 抗告理由"""
    case_number, remaining = extract_case_number(args)
    if not case_number:
        return "❌ 抗告には事件番号が必要です。\n使い方: /抗告 R8-(ワ)-001 抗告理由"
    return await case_manager.file_kokoku(case_number, group_id, user_id, remaining)


def handle_help() -> str:
    """Return help text."""
    return (
        "⚖️ 【AI司法システム ヘルプ】\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📋 【民事事件】\n"
        "  /訴状 @被告 内容 — 訴状を提出\n"
        "  /答弁 内容 — 答弁書を提出（被告のみ）\n"
        "  /和解 — 和解勧告を求める\n"
        "  /和解同意 — 和解案に同意（当事者のみ）\n"
        "  /和解拒否 — 和解案を拒否（当事者のみ）\n\n"
        "🔍 【刑事事件】\n"
        "  /告訴 @被疑者 内容 — 告訴する\n"
        "  /起訴判断 — 検察官の起訴判断を求める\n"
        "  /罪状認否 認める|否認 — 罪状認否（被告人のみ）\n"
        "  /次へ — 次のフェーズに進む\n"
        "  /最終陳述 内容 — 被告人最終陳述（被告人のみ）\n\n"
        "🔍 【略式手続（刑事）】\n"
        "  /略式起訴 — 略式命令を請求（罰金相当事件）\n"
        "  /略式同意 — 略式手続に同意（被疑者のみ）\n"
        "  /略式拒否 — 略式手続を拒否（被疑者のみ）\n"
        "  /正式裁判 — 略式命令後に正式裁判を請求\n\n"
        "📢 【共通】\n"
        "  /弁論 内容 — 口頭弁論・主張（当事者のみ）\n"
        "  /証拠 内容 — 証拠を提出（当事者のみ）\n"
        "  /判決 — 判決を求める\n\n"
        "📢 【不服申立】\n"
        "  /控訴 — 地裁判決に控訴（→高裁）\n"
        "  /上告 [事件番号] 理由 — 高裁判決に上告（→最高裁）\n"
        "  /再審 [事件番号] 理由 — 確定判決の再審請求\n"
        "  /抗告 [事件番号] 理由 — 和解決定への不服申立\n\n"
        "📢 【情報】\n"
        "  /事件一覧 — 進行中の事件一覧\n"
        "  /事件詳細 [番号] — 事件の詳細\n"
        "  /判例 [キーワード] — 判例を検索\n"
        "  /ヘルプ — このメッセージ\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📌 三審制: 地裁→高裁→最高裁\n"
        "📌 控訴/上告期限: 判決から14日以内\n"
        "📌 略式手続: 罰金・科料相当の軽微事件に利用可能\n"
        "📌 複数事件がある場合は事件番号を指定してください"
    )
