import os
from dotenv import load_dotenv

load_dotenv()


def _require_env(key: str) -> str:
    """Get a required environment variable or raise ValueError."""
    value = os.getenv(key, "").strip()
    if not value:
        raise ValueError(
            f"必須環境変数 {key} が設定されていません。.env ファイルまたは環境変数を確認してください。"
        )
    return value


# LINE Messaging API
LINE_CHANNEL_ACCESS_TOKEN = _require_env("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = _require_env("LINE_CHANNEL_SECRET")

# Claude API
ANTHROPIC_API_KEY = _require_env("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

# Database
DATABASE_URL = _require_env("DATABASE_URL")

# Render (optional)
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")

# Constants
MAX_LINE_MESSAGE_LENGTH = 5000
MAX_LINE_MESSAGES_PER_REPLY = 5
APPEAL_DEADLINE_DAYS = 14
KOKOKU_DEADLINE_DAYS = 7  # 即時抗告の期限（民訴法332条: 1週間）
# Self-ping interval: Render free tier sleeps after 15min of inactivity
SELF_PING_INTERVAL_SECONDS = 600
