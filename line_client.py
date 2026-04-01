import httpx
import logging
from typing import Optional

from config import LINE_CHANNEL_ACCESS_TOKEN, MAX_LINE_MESSAGE_LENGTH, MAX_LINE_MESSAGES_PER_REPLY

logger = logging.getLogger(__name__)

LINE_API_BASE = "https://api.line.me/v2/bot"

_headers = {
    "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    "Content-Type": "application/json",
}


def split_message(text: str) -> list[str]:
    """Split a long message into chunks within LINE's character limit."""
    if len(text) <= MAX_LINE_MESSAGE_LENGTH:
        return [text]

    chunks = []
    while text:
        if len(text) <= MAX_LINE_MESSAGE_LENGTH:
            chunks.append(text)
            break

        # Try to split at a newline
        split_pos = text.rfind("\n", 0, MAX_LINE_MESSAGE_LENGTH)
        if split_pos == -1 or split_pos < MAX_LINE_MESSAGE_LENGTH // 2:
            # Fall back to splitting at the limit
            split_pos = MAX_LINE_MESSAGE_LENGTH

        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip("\n")

    return chunks


def _build_text_messages(text: str) -> list[dict]:
    """Build LINE text message objects, splitting if necessary."""
    chunks = split_message(text)
    # LINE allows max 5 messages per reply
    chunks = chunks[:MAX_LINE_MESSAGES_PER_REPLY]
    return [{"type": "text", "text": chunk} for chunk in chunks]


async def reply_message(reply_token: str, text: str) -> bool:
    """Send a reply message using the reply token. Returns True on success."""
    messages = _build_text_messages(text)
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{LINE_API_BASE}/message/reply",
                headers=_headers,
                json={"replyToken": reply_token, "messages": messages},
                timeout=30.0,
            )
            if resp.status_code == 200:
                return True
            logger.warning(f"Reply failed: {resp.status_code} {resp.text}")
            return False
        except httpx.TimeoutException:
            logger.warning("Reply timed out")
            return False
        except Exception as e:
            logger.error(f"Reply error: {e}")
            return False


async def push_message(to: str, text: str) -> bool:
    """Send a push message to a user or group. Returns True on success."""
    messages = _build_text_messages(text)
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{LINE_API_BASE}/message/push",
                headers=_headers,
                json={"to": to, "messages": messages},
                timeout=30.0,
            )
            if resp.status_code == 200:
                return True
            logger.warning(f"Push failed: {resp.status_code} {resp.text}")
            return False
        except Exception as e:
            logger.error(f"Push error: {e}")
            return False


async def send_response(reply_token: str, group_id: str, text: str):
    """Try reply first, fall back to push if reply fails."""
    success = await reply_message(reply_token, text)
    if not success:
        logger.info("Reply failed, falling back to push message")
        await push_message(group_id, text)


async def get_profile(user_id: str) -> Optional[str]:
    """Get the display name of a user. Returns None on failure."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{LINE_API_BASE}/profile/{user_id}",
                headers=_headers,
                timeout=10.0,
            )
            if resp.status_code == 200:
                return resp.json().get("displayName")
            return None
        except Exception:
            return None


async def get_group_member_profile(group_id: str, user_id: str) -> Optional[str]:
    """Get the display name of a user in a group context."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{LINE_API_BASE}/group/{group_id}/member/{user_id}",
                headers=_headers,
                timeout=10.0,
            )
            if resp.status_code == 200:
                return resp.json().get("displayName")
            return None
        except Exception:
            return None


def resolve_mention(event: dict) -> Optional[str]:
    """Extract the first mentioned user ID from a LINE event."""
    mention = event.get("message", {}).get("mention", {})
    mentionees = mention.get("mentionees", [])
    for m in mentionees:
        if m.get("type") == "user" and m.get("userId"):
            return m["userId"]
    return None
