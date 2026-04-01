"""Command parsing and routing for LINE messages."""

import logging
from typing import Optional

import database as db
import case_manager
from line_client import resolve_mention

logger = logging.getLogger(__name__)


def parse_command(text: str) -> tuple[str, str]:
    """Parse a command string into (command, args).

    Examples:
        "/訴状 @被告 内容" -> ("/訴状", "@被告 内容")
        "/判決" -> ("/判決", "")
        "/答弁 内容" -> ("/答弁", "内容")
    """
    text = text.strip()
    if not text.startswith("/"):
        return ("", text)

    parts = text.split(None, 1)
    command = parts[0]
    args = parts[1] if len(parts) > 1 else ""
    return (command, args)


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
            return await handle_settlement_proposal(group_id)
        elif command == "/和解同意":
            return await handle_settlement_accept(group_id, user_id)
        elif command == "/和解拒否":
            return await handle_settlement_reject(group_id, user_id)
        elif command == "/起訴判断":
            return await handle_prosecution_decision(group_id)
        elif command == "/罪状認否":
            return await handle_arraignment(group_id, user_id, args)
        elif command == "/次へ":
            return await handle_next_phase(group_id)
        elif command == "/最終陳述":
            return await handle_final_statement(group_id, user_id, args)
        elif command == "/判決":
            return await handle_verdict(group_id)
        elif command == "/控訴":
            return await handle_appeal(group_id, user_id)
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

    # Remove mention text from args to get complaint content
    complaint_text = args
    # Try to remove the @mention part
    mention = event.get("message", {}).get("mention", {})
    mentionees = mention.get("mentionees", [])
    if mentionees:
        # The mention text occupies a range in the original text
        # We just use everything after the mention as the complaint
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
    """Handle /答弁 内容"""
    if not args:
        return "❌ 答弁の内容を記載してください。\n使い方: /答弁 答弁の内容"

    case = await db.get_latest_active_case(group_id)
    if not case:
        return "❌ 進行中の事件がありません。"

    return await case_manager.submit_answer(case.id, user_id, args)


async def handle_argument(group_id: str, user_id: str, args: str) -> str:
    """Handle /弁論 内容"""
    if not args:
        return "❌ 弁論の内容を記載してください。\n使い方: /弁論 弁論の内容"

    case = await db.get_latest_active_case(group_id)
    if not case:
        return "❌ 進行中の事件がありません。"

    return await case_manager.submit_argument(case.id, user_id, args)


async def handle_evidence(group_id: str, user_id: str, args: str) -> str:
    """Handle /証拠 内容"""
    if not args:
        return "❌ 証拠の内容を記載してください。\n使い方: /証拠 証拠の説明"

    case = await db.get_latest_active_case(group_id)
    if not case:
        return "❌ 進行中の事件がありません。"

    return await case_manager.submit_evidence(case.id, user_id, args)


async def handle_settlement_proposal(group_id: str) -> str:
    """Handle /和解"""
    case = await db.get_latest_active_case(group_id)
    if not case:
        return "❌ 進行中の事件がありません。"

    return await case_manager.propose_settlement(case.id)


async def handle_settlement_accept(group_id: str, user_id: str) -> str:
    """Handle /和解同意"""
    case = await db.get_latest_active_case(group_id)
    if not case:
        return "❌ 進行中の事件がありません。"

    return await case_manager.accept_settlement(case.id, user_id)


async def handle_settlement_reject(group_id: str, user_id: str) -> str:
    """Handle /和解拒否"""
    case = await db.get_latest_active_case(group_id)
    if not case:
        return "❌ 進行中の事件がありません。"

    return await case_manager.reject_settlement(case.id, user_id)


async def handle_prosecution_decision(group_id: str) -> str:
    """Handle /起訴判断"""
    case = await db.get_latest_active_case(group_id)
    if not case:
        return "❌ 進行中の事件がありません。"

    return await case_manager.decide_prosecution(case.id)


async def handle_arraignment(group_id: str, user_id: str, args: str) -> str:
    """Handle /罪状認否 認める|否認"""
    if not args:
        return "❌ 認否を入力してください。\n使い方: /罪状認否 認める または /罪状認否 否認"

    case = await db.get_latest_active_case(group_id)
    if not case:
        return "❌ 進行中の事件がありません。"

    return await case_manager.arraignment(case.id, user_id, args)


async def handle_next_phase(group_id: str) -> str:
    """Handle /次へ"""
    case = await db.get_latest_active_case(group_id)
    if not case:
        return "❌ 進行中の事件がありません。"

    return await case_manager.proceed_criminal_phase(case.id)


async def handle_final_statement(group_id: str, user_id: str, args: str) -> str:
    """Handle /最終陳述 内容"""
    if not args:
        return "❌ 最終陳述の内容を記載してください。\n使い方: /最終陳述 最終陳述の内容"

    case = await db.get_latest_active_case(group_id)
    if not case:
        return "❌ 進行中の事件がありません。"

    return await case_manager.final_statement(case.id, user_id, args)


async def handle_verdict(group_id: str) -> str:
    """Handle /判決"""
    case = await db.get_latest_active_case(group_id)
    if not case:
        return "❌ 進行中の事件がありません。"

    from models import CaseType
    if case.case_type == CaseType.CIVIL:
        return await case_manager.render_civil_verdict(case.id)
    else:
        return await case_manager.render_criminal_verdict(case.id)


async def handle_appeal(group_id: str, user_id: str) -> str:
    """Handle /控訴"""
    # Look for cases with verdict
    cases = await db.get_active_cases(group_id)
    verdict_case = None
    for c in cases:
        from models import CivilPhase, CriminalPhase
        if c.phase in (CivilPhase.VERDICT, CriminalPhase.VERDICT):
            verdict_case = c
            break

    if not verdict_case:
        return "❌ 控訴可能な判決がありません。"

    return await case_manager.file_appeal(verdict_case.id, user_id)


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

    case = await db.get_latest_active_case(group_id)
    if not case:
        return "❌ 進行中の事件がありません。"
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


def handle_help() -> str:
    """Return help text."""
    return (
        "⚖️ 【AI司法システム ヘルプ】\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📋 【民事事件】\n"
        "  /訴状 @被告 内容 — 訴状を提出\n"
        "  /答弁 内容 — 答弁書を提出\n"
        "  /和解 — 和解勧告を求める\n"
        "  /和解同意 — 和解案に同意\n"
        "  /和解拒否 — 和解案を拒否\n\n"
        "🔍 【刑事事件】\n"
        "  /告訴 @被疑者 内容 — 告訴する\n"
        "  /起訴判断 — 検察官の起訴判断を求める\n"
        "  /罪状認否 認める|否認 — 罪状認否\n"
        "  /次へ — 次のフェーズに進む\n"
        "  /最終陳述 内容 — 被告人最終陳述\n\n"
        "📢 【共通】\n"
        "  /弁論 内容 — 口頭弁論・主張\n"
        "  /証拠 内容 — 証拠を提出\n"
        "  /判決 — 判決を求める\n"
        "  /控訴 — 控訴する\n"
        "  /事件一覧 — 進行中の事件一覧\n"
        "  /事件詳細 [番号] — 事件の詳細\n"
        "  /判例 [キーワード] — 判例を検索\n"
        "  /ヘルプ — このメッセージ\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📌 三審制: 地裁→高裁→最高裁\n"
        "📌 控訴期限: 判決から14日以内"
    )
