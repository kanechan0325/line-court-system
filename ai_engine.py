"""Claude API integration for each trial phase."""

import json
import re
import logging
import httpx
from typing import Optional

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from models import CaseRecord


class AIResponseError(Exception):
    """Raised when Claude API call fails (timeout, HTTP error, etc.)."""
    pass


from ai_personas import (
    get_judge_review_prompt,
    get_judge_issue_organization_prompt,
    get_judge_settlement_prompt,
    get_judge_verdict_prompt,
    get_prosecutor_investigation_prompt,
    get_prosecutor_charge_decision_prompt,
    get_prosecutor_closing_prompt,
    get_defense_rights_notification_prompt,
    get_defense_opening_prompt,
    get_defense_closing_prompt,
    get_clerk_record_prompt,
    get_judge_prompt,
    get_prosecutor_prompt,
    get_defense_prompt,
    get_prosecutor_summary_request_prompt,
    get_judge_summary_order_prompt,
    get_judge_jokoku_review_prompt,
    get_judge_retrial_review_prompt,
    get_judge_kokoku_review_prompt,
)

logger = logging.getLogger(__name__)

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"

# Shared httpx client for Claude API
_claude_client: Optional[httpx.AsyncClient] = None


async def _get_claude_client() -> httpx.AsyncClient:
    """Get or create the shared Claude API client."""
    global _claude_client
    if _claude_client is None or _claude_client.is_closed:
        _claude_client = httpx.AsyncClient(
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=120.0,
        )
    return _claude_client


async def close_claude_client():
    """Close the shared Claude API client."""
    global _claude_client
    if _claude_client and not _claude_client.is_closed:
        await _claude_client.aclose()
        _claude_client = None


async def call_claude(system_prompt: str, user_message: str, max_tokens: int = 4096) -> str:
    """Call Claude API with retry and return the response text."""
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
    }

    client = await _get_claude_client()
    last_error = None

    for attempt in range(3):
        try:
            resp = await client.post(CLAUDE_API_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
            content_blocks = data.get("content", [])
            text_parts = [b["text"] for b in content_blocks if b.get("type") == "text"]
            result = "\n".join(text_parts)
            if not result.strip():
                raise AIResponseError("AIから空の応答が返されました。")
            return result
        except httpx.TimeoutException:
            logger.warning(f"Claude API timeout (attempt {attempt + 1}/3)")
            last_error = AIResponseError("AI応答がタイムアウトしました。しばらく待ってから再度お試しください。")
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            logger.error(f"Claude API HTTP error: {status} {e.response.text}")
            if status == 429 or status >= 500:
                # Retryable errors
                last_error = AIResponseError(f"AI APIエラーが発生しました（{status}）。")
                import asyncio
                await asyncio.sleep(2 ** attempt)
                continue
            raise AIResponseError(f"AI APIエラーが発生しました（{status}）。")
        except AIResponseError:
            raise
        except Exception as e:
            logger.error(f"Claude API error: {e}")
            raise AIResponseError("AI応答の取得に失敗しました。")

    raise last_error or AIResponseError("AI応答の取得に失敗しました。")


def _extract_json_by_brace_counting(text: str, start: int) -> Optional[dict]:
    """Extract a JSON object starting at the given position using brace counting."""
    brace_count = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            brace_count += 1
        elif text[i] == "}":
            brace_count -= 1
            if brace_count == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _extract_json_from_text(text: str, key_hint: str) -> Optional[dict]:
    """Extract a JSON object from AI response text, looking for a specific key."""
    # Try to find JSON in ```json ... ``` blocks (use brace counting for nested objects)
    block_match = re.search(r"```json\s*", text)
    if block_match:
        # Find the opening brace after ```json
        rest = text[block_match.end():]
        brace_start = rest.find("{")
        if brace_start != -1:
            result = _extract_json_by_brace_counting(rest, brace_start)
            if result is not None:
                return result

    # Try to find raw JSON object by key hint
    json_match = re.search(rf'\{{\s*"{key_hint}"\s*:', text, re.DOTALL)
    if json_match:
        result = _extract_json_by_brace_counting(text, json_match.start())
        if result is not None:
            return result

    return None


def parse_verdict_json(text: str) -> Optional[dict]:
    """Extract verdict JSON from AI response text."""
    return _extract_json_from_text(text, "verdict")


def parse_prosecution_json(text: str) -> Optional[dict]:
    """Extract prosecution decision JSON from AI response text."""
    return _extract_json_from_text(text, "decision")


# --- Judge functions ---

async def judge_review_complaint(case: CaseRecord) -> str:
    """Judge reviews a complaint for acceptance."""
    system = get_judge_review_prompt(case)
    user_msg = f"訴状/告訴状の内容:\n{case.complaint_text}\n\n受理審査を行ってください。"
    return await call_claude(system, user_msg)


async def judge_organize_issues(case: CaseRecord, logs: str) -> str:
    """Judge organizes the issues of the case."""
    system = get_judge_issue_organization_prompt(case, logs)
    user_msg = "これまでの当事者の主張を踏まえ、争点を整理してください。"
    return await call_claude(system, user_msg)


async def judge_settlement_proposal(case: CaseRecord, logs: str) -> str:
    """Judge proposes a settlement."""
    system = get_judge_settlement_prompt(case, logs)
    user_msg = "和解案を提示してください。"
    return await call_claude(system, user_msg)


async def judge_render_verdict(
    case: CaseRecord, logs: str, evidence: str, precedents: str
) -> tuple[str, Optional[dict]]:
    """Judge renders a verdict. Returns (full_text, parsed_json_or_none)."""
    system = get_judge_verdict_prompt(case, logs, evidence, precedents)
    user_msg = "判決を言い渡してください。"
    response = await call_claude(system, user_msg, max_tokens=6000)
    verdict_data = parse_verdict_json(response)
    return response, verdict_data


async def judge_respond(case: CaseRecord, user_input: str, context: str = "") -> str:
    """General judge response for oral arguments etc."""
    system = get_judge_prompt(case, context)
    return await call_claude(system, user_input)


# --- Prosecutor functions ---

async def prosecutor_investigate(case: CaseRecord) -> str:
    """Prosecutor conducts an investigation."""
    system = get_prosecutor_investigation_prompt(case)
    user_msg = f"告訴内容:\n{case.complaint_text}\n\n捜査を開始してください。"
    return await call_claude(system, user_msg)


async def prosecutor_decide_charge(case: CaseRecord, investigation: str) -> tuple[str, bool, str]:
    """Prosecutor decides whether to prosecute.

    Returns (response, is_prosecuted, procedure).
    procedure is "SUMMARY" or "FORMAL".
    """
    system = get_prosecutor_charge_decision_prompt(case, investigation)
    user_msg = "捜査結果に基づき、起訴/不起訴の判断を行ってください。"
    response = await call_claude(system, user_msg)

    procedure = "FORMAL"

    # Try JSON-based detection first
    decision_data = parse_prosecution_json(response)
    if decision_data and "decision" in decision_data:
        is_prosecuted = decision_data["decision"] == "PROSECUTE"
        procedure = decision_data.get("procedure", "FORMAL")
        return response, is_prosecuted, procedure

    # Fallback: text-based detection — check for NOT_PROSECUTE keywords first
    logger.warning("Prosecution decision JSON not found, falling back to text detection")
    not_prosecute_keywords = ["不起訴", "起訴猶予", "起訴しない", "嫌疑不十分", "嫌疑なし"]
    if any(kw in response for kw in not_prosecute_keywords):
        is_prosecuted = False
    else:
        is_prosecuted = "起訴" in response
    # Check for summary procedure in text fallback
    if "略式" in response:
        procedure = "SUMMARY"
    return response, is_prosecuted, procedure


async def prosecutor_opening(case: CaseRecord) -> str:
    """Prosecutor's opening statement."""
    system = get_prosecutor_prompt(case, "冒頭陳述を行ってください。")
    user_msg = (
        "検察官として冒頭陳述を行ってください。\n"
        "1. 公訴事実の要旨\n"
        "2. 立証予定の事実\n"
        "3. 証拠の概要"
    )
    return await call_claude(system, user_msg)


async def prosecutor_closing(case: CaseRecord, logs: str, evidence: str) -> str:
    """Prosecutor's closing argument (論告求刑)."""
    system = get_prosecutor_closing_prompt(case, logs, evidence)
    user_msg = "論告求刑を行ってください。"
    return await call_claude(system, user_msg)


# --- Defense functions ---

async def defense_rights_notification(case: CaseRecord) -> str:
    """Defense attorney notifies defendant of their rights."""
    system = get_defense_rights_notification_prompt(case)
    user_msg = "被告人に権利を告知し、今後の手続きを説明してください。"
    return await call_claude(system, user_msg)


async def defense_opening(case: CaseRecord) -> str:
    """Defense attorney's opening statement."""
    system = get_defense_opening_prompt(case)
    user_msg = "弁護側の冒頭陳述を行ってください。"
    return await call_claude(system, user_msg)


async def defense_closing(case: CaseRecord, logs: str, evidence: str) -> str:
    """Defense attorney's closing argument."""
    system = get_defense_closing_prompt(case, logs, evidence)
    user_msg = "最終弁論を行ってください。"
    return await call_claude(system, user_msg)


async def defense_respond(case: CaseRecord, user_input: str) -> str:
    """General defense response."""
    system = get_defense_prompt(case)
    return await call_claude(system, user_input)


# --- Clerk functions ---

async def clerk_create_record(case: CaseRecord, phase: str, content: str) -> str:
    """Clerk creates a court record."""
    system = get_clerk_record_prompt(case, phase, content)
    user_msg = "調書を作成してください。"
    return await call_claude(system, user_msg, max_tokens=2000)


# --- Opening procedure (刑事冒頭手続き) ---

async def opening_procedure(case: CaseRecord) -> str:
    """Generate the opening procedure for criminal cases."""
    system = get_judge_prompt(
        case,
        "刑事裁判の冒頭手続きを行います。"
    )
    user_msg = """冒頭手続きとして以下を行ってください：
1. 人定質問（被告人の氏名確認）
2. 起訴状の朗読
3. 黙秘権の告知（憲法第38条、刑事訴訟法第311条）
4. 被告人に罪状認否の機会を与える旨の告知

※被告人の罪状認否は次のステップで行います。"""
    return await call_claude(system, user_msg)


# --- Defendant questioning ---

async def defendant_questioning_prompt(case: CaseRecord, logs: str) -> str:
    """Generate questions for defendant questioning phase."""
    system = get_judge_prompt(
        case,
        "被告人質問を行います。裁判官として質問を行ってください。"
    )
    user_msg = f"""【審理記録】
{logs}

被告人質問を行ってください。以下の観点から質問してください：
1. 事件の経緯に関する質問
2. 動機に関する質問
3. 反省・更生の意思に関する質問
4. 被害者への謝罪の意思に関する質問

※被告人の回答は次のステップで受け付けます。"""
    return await call_claude(system, user_msg)


# --- Summary Trial functions ---

async def judge_render_summary_order(case: CaseRecord, logs: str) -> tuple[str, Optional[dict]]:
    """Judge renders a summary order (略式命令). Returns (full_text, parsed_json_or_none)."""
    system = get_judge_summary_order_prompt(case, logs)
    user_msg = "書面審理に基づき略式命令を発してください。"
    response = await call_claude(system, user_msg)
    verdict_data = parse_verdict_json(response)
    return response, verdict_data


async def prosecutor_summary_request(case: CaseRecord, investigation: str) -> tuple[str, Optional[dict]]:
    """Prosecutor requests summary prosecution. Returns (response, parsed_json_or_none)."""
    system = get_prosecutor_summary_request_prompt(case, investigation)
    user_msg = "略式命令の請求を検討してください。"
    response = await call_claude(system, user_msg)
    data = _extract_json_from_text(response, "summary_eligible")
    return response, data


# --- Jokoku Appeal functions ---

async def judge_review_jokoku(case: CaseRecord, original_verdict: str, jokoku_reason: str) -> str:
    """Judge reviews a jokoku appeal (上告審受理審査)."""
    system = get_judge_jokoku_review_prompt(case, original_verdict, jokoku_reason)
    user_msg = f"上告理由:\n{jokoku_reason}\n\n上告審受理審査を行ってください。"
    return await call_claude(system, user_msg)


# --- Retrial functions ---

def parse_review_decision_json(text: str) -> Optional[dict]:
    """Parse retrial/kokoku review decision JSON. Expects {"decision": "ACCEPT"|"REJECT", ...}."""
    return _extract_json_from_text(text, "decision")


async def judge_review_retrial(case: CaseRecord, retrial_reason: str) -> tuple[str, bool]:
    """Judge reviews a retrial request. Returns (response, is_accepted)."""
    system = get_judge_retrial_review_prompt(case, retrial_reason)
    user_msg = f"再審請求理由:\n{retrial_reason}\n\n再審事由の審査を行ってください。"
    response = await call_claude(system, user_msg)
    data = parse_review_decision_json(response)
    is_accepted = data is not None and data.get("decision") == "ACCEPT"
    return response, is_accepted


# --- Kokoku Appeal functions ---

async def judge_review_kokoku(case: CaseRecord, kokoku_reason: str) -> tuple[str, bool]:
    """Judge reviews a kokoku appeal. Returns (response, is_accepted)."""
    system = get_judge_kokoku_review_prompt(case, kokoku_reason)
    user_msg = f"抗告理由:\n{kokoku_reason}\n\n抗告の審査を行ってください。"
    response = await call_claude(system, user_msg)
    data = parse_review_decision_json(response)
    is_accepted = data is not None and data.get("decision") == "ACCEPT"
    return response, is_accepted
