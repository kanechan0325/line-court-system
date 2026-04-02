"""FastAPI application — LINE Webhook endpoint and self-ping."""

import asyncio
import hashlib
import hmac
import base64
import logging
import time
from contextlib import asynccontextmanager
from collections import OrderedDict

import httpx
from fastapi import FastAPI, Request, HTTPException

from config import (
    LINE_CHANNEL_SECRET,
    RENDER_EXTERNAL_URL,
    SELF_PING_INTERVAL_SECONDS,
)
import database as db
from command_handler import handle_command
from line_client import send_response, push_message, close_http_client
from ai_engine import close_claude_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# --- Self-ping to prevent Render sleep ---

async def self_ping():
    """Ping own health endpoint periodically to prevent Render free tier sleep."""
    if not RENDER_EXTERNAL_URL:
        logger.warning("RENDER_EXTERNAL_URL not set — self-ping disabled. "
                       "Set this env var on Render to prevent free tier sleep.")
        return

    url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/health"
    backoff = 0
    async with httpx.AsyncClient() as client:
        while True:
            try:
                if backoff == 0:
                    # First ping after short delay to let server start
                    await asyncio.sleep(10)
                else:
                    await asyncio.sleep(SELF_PING_INTERVAL_SECONDS)
                resp = await client.get(url, timeout=10.0)
                logger.info(f"Self-ping: {resp.status_code}")
                backoff = 0
            except Exception as e:
                backoff = min(backoff + 1, 3)
                logger.warning(f"Self-ping failed (attempt backoff={backoff}): {e}")
                await asyncio.sleep(2 ** backoff)


# --- Lifecycle ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown."""
    # Startup
    await db.init_pool()
    ping_task = asyncio.create_task(self_ping())
    logger.info("Application started")

    yield

    # Shutdown
    ping_task.cancel()
    try:
        await ping_task
    except asyncio.CancelledError:
        pass

    # Cancel all group workers gracefully
    pending_events = sum(q.qsize() for q in _group_queues.values())
    if pending_events > 0:
        logger.warning(f"Shutting down with {pending_events} pending events in queues")
    for gid, task in list(_group_workers.items()):
        task.cancel()
    for gid, task in list(_group_workers.items()):
        try:
            await task
        except asyncio.CancelledError:
            pass

    await close_http_client()
    await close_claude_client()
    await db.close_pool()
    logger.info("Application shut down")


app = FastAPI(title="LINE AI司法システム", lifespan=lifespan)

# Per-group event queues for ordering
_group_queues: dict[str, asyncio.Queue] = {}
_group_workers: dict[str, asyncio.Task] = {}
_group_last_active: dict[str, float] = {}

# Lock for worker creation to prevent race conditions
_worker_lock = asyncio.Lock()

# Webhook deduplication (OrderedDict as LRU cache)
_seen_events: OrderedDict[str, float] = OrderedDict()
_SEEN_EVENTS_MAX = 1000
_SEEN_EVENTS_TTL = 300  # 5 minutes

# Reply token timeout threshold (seconds)
_REPLY_TOKEN_TIMEOUT = 30


def _is_duplicate_event(event: dict) -> bool:
    """Check and record webhook event ID for deduplication."""
    event_id = event.get("webhookEventId")
    if not event_id:
        return False

    now = time.time()

    # Clean expired entries
    while _seen_events:
        oldest_key, oldest_time = next(iter(_seen_events.items()))
        if now - oldest_time > _SEEN_EVENTS_TTL:
            _seen_events.pop(oldest_key)
        else:
            break

    if event_id in _seen_events:
        return True

    _seen_events[event_id] = now
    # Enforce max size
    while len(_seen_events) > _SEEN_EVENTS_MAX:
        _seen_events.popitem(last=False)

    return False


async def _group_worker(group_id: str):
    """Process events for a single group sequentially."""
    queue = _group_queues[group_id]
    idle_timeout = 300  # 5 minutes
    while True:
        try:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=idle_timeout)
            except asyncio.TimeoutError:
                # Idle timeout — clean up this worker
                break

            _group_last_active[group_id] = time.time()
            try:
                await process_event(event)
            except Exception as e:
                logger.error(f"Group worker error for {group_id}: {e}", exc_info=True)
            finally:
                queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Unexpected error in worker loop — log and continue
            logger.error(f"Group worker unexpected error for {group_id}: {e}", exc_info=True)
            await asyncio.sleep(1)

    # Cleanup on idle timeout
    _group_queues.pop(group_id, None)
    _group_workers.pop(group_id, None)
    _group_last_active.pop(group_id, None)
    logger.debug(f"Group worker for {group_id} cleaned up due to idle timeout")


async def _ensure_worker(group_id: str):
    """Ensure a worker exists for the group, restarting if crashed."""
    async with _worker_lock:
        existing = _group_workers.get(group_id)
        if existing and not existing.done():
            return

        if existing and existing.done():
            logger.warning(f"Restarting crashed worker for group {group_id}")

        if group_id not in _group_queues:
            _group_queues[group_id] = asyncio.Queue()

        _group_workers[group_id] = asyncio.create_task(_group_worker(group_id))


async def enqueue_event(event: dict):
    """Enqueue an event for sequential processing per group."""
    if _is_duplicate_event(event):
        logger.info(f"Duplicate webhook event skipped: {event.get('webhookEventId')}")
        return

    # Attach enqueue timestamp for reply token optimization
    event["_enqueued_at"] = time.time()

    source = event.get("source", {})
    group_id = source.get("groupId", source.get("userId", "unknown"))

    await _ensure_worker(group_id)
    _group_queues[group_id].put_nowait(event)


# --- Signature verification ---

def verify_signature(body: bytes, signature: str) -> bool:
    """Verify LINE webhook signature using HMAC-SHA256."""
    hash_value = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(hash_value).decode("utf-8")
    return hmac.compare_digest(signature, expected)


# --- Endpoints ---

@app.get("/health")
async def health():
    """Health check endpoint with DB connectivity verification."""
    try:
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ok", "service": "line-court-system"}
    except Exception as e:
        logger.warning(f"Health check DB error: {e}")
        return {"status": "degraded", "service": "line-court-system", "db": "unavailable"}


@app.post("/webhook")
async def callback(request: Request):
    """LINE Webhook callback endpoint."""
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        logger.warning(f"Invalid webhook signature from {request.client.host if request.client else 'unknown'}")
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        data = await request.json()
    except Exception:
        logger.warning("Invalid JSON in webhook request")
        raise HTTPException(status_code=400, detail="Bad Request")

    events = data.get("events", [])

    for event in events:
        await enqueue_event(event)

    return {"status": "ok"}


async def _send_with_token_check(event: dict, reply_token: str, to: str, text: str):
    """Send response, using push directly if reply token is likely expired."""
    enqueued_at = event.get("_enqueued_at", 0)
    elapsed = time.time() - enqueued_at if enqueued_at else 0

    if elapsed > _REPLY_TOKEN_TIMEOUT:
        # Token likely expired — skip reply attempt, use push directly
        logger.info(f"Reply token likely expired ({elapsed:.1f}s), using push")
        await push_message(to, text)
    else:
        await send_response(reply_token, to, text)


async def process_event(event: dict):
    """Process a single LINE webhook event."""
    try:
        event_type = event.get("type")
        if event_type != "message":
            return

        message = event.get("message", {})
        if message.get("type") != "text":
            return

        text = message.get("text", "").strip()
        if not text.startswith("/"):
            return

        source = event.get("source", {})
        source_type = source.get("type")
        reply_token = event.get("replyToken", "")

        # Only process group messages
        if source_type == "group":
            group_id = source.get("groupId", "")
            user_id = source.get("userId", "")
        elif source_type == "user":
            # 1:1 chat — respond with help
            user_id = source.get("userId", "")
            await _send_with_token_check(
                event, reply_token, user_id,
                "⚖️ AI司法システムはグループチャットでのみ使用できます。\n"
                "グループにBotを招待してご利用ください。"
            )
            return
        else:
            return

        if not group_id or not user_id:
            logger.warning("Missing group_id or user_id")
            return

        # Handle command
        response = await handle_command(event, group_id, user_id, reply_token)

        if response:
            await _send_with_token_check(event, reply_token, group_id, response)

    except Exception as e:
        logger.error(f"Event processing error: {e}", exc_info=True)
        # Try to send error message
        try:
            reply_token = event.get("replyToken", "")
            source = event.get("source", {})
            to = source.get("groupId") or source.get("userId", "")
            if to:
                await _send_with_token_check(
                    event, reply_token, to,
                    "❌ システムエラーが発生しました。しばらく待ってから再度お試しください。"
                )
        except Exception as e2:
            logger.error(f"Failed to send error message: {e2}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
