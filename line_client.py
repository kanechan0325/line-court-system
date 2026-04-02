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

# Shared httpx client for connection reuse
_client: Optional[httpx.AsyncClient] = None


async def get_http_client() -> httpx.AsyncClient:
    """Get or create the shared HTTP client."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(headers=_headers, timeout=30.0)
    return _client


async def close_http_client():
    """Close the shared HTTP client."""
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


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
    if len(chunks) > MAX_LINE_MESSAGES_PER_REPLY:
        chunks = chunks[:MAX_LINE_MESSAGES_PER_REPLY - 1]
        chunks.append("（メッセージが長いため一部省略されました。/事件詳細 で確認できます。）")
    return [{"type": "text", "text": chunk} for chunk in chunks]


async def reply_message(reply_token: str, text: str) -> bool:
    """Send a reply message using the reply token. Returns True on success."""
    messages = _build_text_messages(text)
    client = await get_http_client()
    try:
        resp = await client.post(
            f"{LINE_API_BASE}/message/reply",
            json={"replyToken": reply_token, "messages": messages},
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
    """Send a push message to a user or group. Returns True on success. Retries once on failure."""
    import asyncio
    messages = _build_text_messages(text)
    client = await get_http_client()
    for attempt in range(2):
        try:
            resp = await client.post(
                f"{LINE_API_BASE}/message/push",
                json={"to": to, "messages": messages},
            )
            if resp.status_code == 200:
                return True
            logger.warning(f"Push failed (attempt {attempt + 1}): {resp.status_code} {resp.text}")
            if attempt == 0 and resp.status_code >= 500:
                await asyncio.sleep(1)
                continue
            return False
        except Exception as e:
            logger.error(f"Push error (attempt {attempt + 1}): {e}")
            if attempt == 0:
                await asyncio.sleep(1)
                continue
            return False
    return False


async def send_response(reply_token: str, group_id: str, text: str):
    """Try reply first, fall back to push if reply fails."""
    success = await reply_message(reply_token, text)
    if not success:
        logger.info("Reply failed, falling back to push message")
        await push_message(group_id, text)


async def get_profile(user_id: str) -> Optional[str]:
    """Get the display name of a user. Returns None on failure."""
    client = await get_http_client()
    try:
        resp = await client.get(
            f"{LINE_API_BASE}/profile/{user_id}",
            timeout=10.0,
        )
        if resp.status_code == 200:
            return resp.json().get("displayName")
        if resp.status_code in (401, 403):
            logger.error(f"Profile API auth error: {resp.status_code}")
        return None
    except Exception as e:
        logger.warning(f"Profile fetch error for {user_id}: {e}")
        return None


async def get_group_member_profile(group_id: str, user_id: str) -> Optional[str]:
    """Get the display name of a user in a group context."""
    client = await get_http_client()
    try:
        resp = await client.get(
            f"{LINE_API_BASE}/group/{group_id}/member/{user_id}",
            timeout=10.0,
        )
        if resp.status_code == 200:
            return resp.json().get("displayName")
        if resp.status_code in (401, 403):
            logger.error(f"Group member profile API auth error: {resp.status_code}")
        return None
    except Exception as e:
        logger.warning(f"Group member profile fetch error: {e}")
        return None


def resolve_mention(event: dict) -> Optional[str]:
    """Extract the first mentioned user ID from a LINE event."""
    mention = event.get("message", {}).get("mention", {})
    mentionees = mention.get("mentionees", [])
    for m in mentionees:
        if m.get("type") == "user" and m.get("userId"):
            return m["userId"]
    return None
