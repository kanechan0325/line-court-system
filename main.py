"""FastAPI application — LINE Webhook endpoint and self-ping."""

import asyncio
import hashlib
import hmac
import base64
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, HTTPException

from config import (
    LINE_CHANNEL_SECRET,
    RENDER_EXTERNAL_URL,
    SELF_PING_INTERVAL_SECONDS,
)
import database as db
from command_handler import handle_command
from line_client import send_response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# --- Self-ping to prevent Render sleep ---

async def self_ping():
    """Ping own health endpoint every 10 minutes to prevent Render free tier sleep."""
    if not RENDER_EXTERNAL_URL:
        logger.info("RENDER_EXTERNAL_URL not set, skipping self-ping")
        return

    url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/health"
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await asyncio.sleep(SELF_PING_INTERVAL_SECONDS)
                resp = await client.get(url, timeout=10.0)
                logger.debug(f"Self-ping: {resp.status_code}")
            except Exception as e:
                logger.warning(f"Self-ping failed: {e}")


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
    await db.close_pool()
    logger.info("Application shut down")


app = FastAPI(title="LINE AI司法システム", lifespan=lifespan)

# Per-group event queues for ordering
_group_queues: dict[str, asyncio.Queue] = {}
_group_workers: dict[str, asyncio.Task] = {}


async def _group_worker(group_id: str):
    """Process events for a single group sequentially."""
    queue = _group_queues[group_id]
    while True:
        event = await queue.get()
        try:
            await process_event(event)
        except Exception as e:
            logger.error(f"Group worker error for {group_id}: {e}", exc_info=True)
        finally:
            queue.task_done()


def enqueue_event(event: dict):
    """Enqueue an event for sequential processing per group."""
    source = event.get("source", {})
    group_id = source.get("groupId", source.get("userId", "unknown"))

    if group_id not in _group_queues:
        _group_queues[group_id] = asyncio.Queue()
        _group_workers[group_id] = asyncio.create_task(_group_worker(group_id))

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
    """Health check endpoint."""
    return {"status": "ok", "service": "line-court-system"}


@app.post("/webhook")
async def callback(request: Request):
    """LINE Webhook callback endpoint."""
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    events = data.get("events", [])

    for event in events:
        enqueue_event(event)

    return {"status": "ok"}


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
            await send_response(
                reply_token, user_id,
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
            await send_response(reply_token, group_id, response)

    except Exception as e:
        logger.error(f"Event processing error: {e}", exc_info=True)
        # Try to send error message
        try:
            reply_token = event.get("replyToken", "")
            source = event.get("source", {})
            to = source.get("groupId") or source.get("userId", "")
            if to:
                await send_response(
                    reply_token, to,
                    "❌ システムエラーが発生しました。しばらく待ってから再度お試しください。"
                )
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
