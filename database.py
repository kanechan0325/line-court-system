import asyncpg
import logging
from typing import Optional
from datetime import datetime, timedelta, timezone

from config import DATABASE_URL, APPEAL_DEADLINE_DAYS
from models import CaseRecord, EvidenceRecord, PrecedentRecord, CaseType, CourtLevel, generate_case_number

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


async def init_pool():
    """Initialize the database connection pool."""
    global _pool
    _pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=2, max_size=10,
        command_timeout=60,
    )
    await create_tables()
    logger.info("Database pool initialized")


async def close_pool():
    """Close the database connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("Database pool closed")


async def get_pool() -> asyncpg.Pool:
    """Get the connection pool, initializing if needed."""
    global _pool
    if _pool is None:
        await init_pool()
    return _pool


async def create_tables():
    """Create all required tables if they don't exist."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS cases (
                id SERIAL PRIMARY KEY,
                case_number TEXT UNIQUE NOT NULL,
                case_type TEXT NOT NULL,
                court_level TEXT NOT NULL DEFAULT 'DISTRICT',
                phase TEXT NOT NULL,
                plaintiff_id TEXT NOT NULL,
                defendant_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                complaint_text TEXT NOT NULL,
                answer_text TEXT,
                verdict_text TEXT,
                sentence TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW(),
                appeal_deadline TIMESTAMP,
                parent_case_id INTEGER REFERENCES cases(id),
                status TEXT DEFAULT 'ACTIVE'
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS evidence (
                id SERIAL PRIMARY KEY,
                case_id INTEGER REFERENCES cases(id),
                submitter_id TEXT NOT NULL,
                content TEXT NOT NULL,
                evidence_type TEXT DEFAULT 'DOCUMENT',
                submitted_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS precedents (
                id SERIAL PRIMARY KEY,
                case_number TEXT NOT NULL,
                case_type TEXT NOT NULL,
                summary TEXT NOT NULL,
                verdict TEXT NOT NULL,
                sentence TEXT,
                judgment_text TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS case_logs (
                id SERIAL PRIMARY KEY,
                case_id INTEGER REFERENCES cases(id),
                phase TEXT NOT NULL,
                actor TEXT NOT NULL,
                content TEXT NOT NULL,
                action_type TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Add action_type column if table already exists without it
        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'case_logs' AND column_name = 'action_type'
                ) THEN
                    ALTER TABLE case_logs ADD COLUMN action_type TEXT;
                END IF;
            END $$;
        """)
        # Add case_subtype column if table already exists without it
        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'cases' AND column_name = 'case_subtype'
                ) THEN
                    ALTER TABLE cases ADD COLUMN case_subtype TEXT;
                END IF;
            END $$;
        """)
        # Create indexes for performance
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_cases_group_id ON cases(group_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_cases_type_level ON cases(case_type, court_level)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(status)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_case_id ON evidence(case_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_case_logs_case_id ON case_logs(case_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_precedents_case_number ON precedents(case_number)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_cases_subtype ON cases(case_subtype)")
    logger.info("Database tables created/verified")


# --- Case CRUD ---

async def create_case(
    case_type: str,
    court_level: str,
    phase: str,
    plaintiff_id: str,
    defendant_id: str,
    group_id: str,
    complaint_text: str,
    parent_case_id: Optional[int] = None,
    case_subtype: Optional[str] = None,
    is_retrial: bool = False,
) -> CaseRecord:
    """Create a new case with atomic case number generation."""
    pool = await get_pool()

    # Retry loop for race condition on UNIQUE constraint
    for attempt in range(3):
        async with pool.acquire() as conn:
            # Atomic: get next sequence and insert in same transaction
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT COALESCE(MAX(id), 0) + 1 as seq FROM cases WHERE case_type = $1 AND court_level = $2",
                    case_type, court_level
                )
                seq = row["seq"]
                case_number = generate_case_number(
                    CaseType(case_type), CourtLevel(court_level), seq,
                    is_retrial=is_retrial,
                )
                try:
                    result = await conn.fetchrow(
                        """
                        INSERT INTO cases (
                            case_number, case_type, court_level, phase,
                            plaintiff_id, defendant_id, group_id, complaint_text,
                            parent_case_id, status, case_subtype
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'ACTIVE', $10)
                        RETURNING *
                        """,
                        case_number, case_type, court_level, phase,
                        plaintiff_id, defendant_id, group_id, complaint_text,
                        parent_case_id, case_subtype,
                    )
                    return _row_to_case(result)
                except Exception as e:
                    if "unique" in str(e).lower() and attempt < 2:
                        logger.warning(f"Case number collision, retrying: {e}")
                        continue
                    raise
    raise RuntimeError("Failed to generate unique case number after retries")


async def get_case(case_id: int) -> Optional[CaseRecord]:
    """Get a case by ID."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM cases WHERE id = $1", case_id)
    return _row_to_case(row) if row else None


async def get_case_by_number(case_number: str) -> Optional[CaseRecord]:
    """Get a case by case number."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM cases WHERE case_number = $1", case_number
        )
    return _row_to_case(row) if row else None


async def get_active_cases(group_id: str) -> list[CaseRecord]:
    """Get all active cases for a group."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM cases WHERE group_id = $1 AND status = 'ACTIVE' ORDER BY created_at DESC",
            group_id,
        )
    return [_row_to_case(row) for row in rows]


async def get_latest_active_case(group_id: str) -> Optional[CaseRecord]:
    """Get the most recent active case for a group."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM cases WHERE group_id = $1 AND status = 'ACTIVE' ORDER BY created_at DESC LIMIT 1",
            group_id,
        )
    return _row_to_case(row) if row else None


async def update_case_phase(case_id: int, phase: str):
    """Update the phase of a case."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE cases SET phase = $1, updated_at = NOW() WHERE id = $2",
            phase, case_id,
        )


async def update_case_answer(case_id: int, answer_text: str):
    """Update the answer text of a case."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE cases SET answer_text = $1, updated_at = NOW() WHERE id = $2",
            answer_text, case_id,
        )


async def update_case_verdict(
    case_id: int, verdict_text: str, sentence: Optional[str] = None
):
    """Update the verdict and sentence of a case."""
    appeal_deadline = datetime.now(timezone.utc) + timedelta(days=APPEAL_DEADLINE_DAYS)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE cases
            SET verdict_text = $1, sentence = $2, appeal_deadline = $3,
                updated_at = NOW()
            WHERE id = $4
            """,
            verdict_text, sentence, appeal_deadline, case_id,
        )


async def update_case_status(case_id: int, status: str):
    """Update the status of a case."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE cases SET status = $1, updated_at = NOW() WHERE id = $2",
            status, case_id,
        )


async def update_case_court_level(case_id: int, court_level: str):
    """Update the court level of a case."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE cases SET court_level = $1, updated_at = NOW() WHERE id = $2",
            court_level, case_id,
        )


async def get_closed_cases(group_id: str) -> list[CaseRecord]:
    """Get all closed/settled/appealed cases for a group (for retrial/kokoku candidates)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM cases WHERE group_id = $1 AND status IN "
            "('CLOSED', 'SETTLED', 'APPEALED', 'JOKOKU_APPEALED', 'KOKOKU_APPEALED') "
            "ORDER BY created_at DESC",
            group_id,
        )
    return [_row_to_case(row) for row in rows]


# --- Evidence CRUD ---

async def add_evidence(
    case_id: int, submitter_id: str, content: str, evidence_type: str = "DOCUMENT"
) -> EvidenceRecord:
    """Add evidence to a case."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO evidence (case_id, submitter_id, content, evidence_type)
            VALUES ($1, $2, $3, $4)
            RETURNING *
            """,
            case_id, submitter_id, content, evidence_type,
        )
    return _row_to_evidence(row)


async def get_evidence(case_id: int) -> list[EvidenceRecord]:
    """Get all evidence for a case."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM evidence WHERE case_id = $1 ORDER BY submitted_at",
            case_id,
        )
    return [_row_to_evidence(row) for row in rows]


# --- Precedent CRUD ---

async def save_precedent(
    case_number: str,
    case_type: str,
    summary: str,
    verdict: str,
    sentence: Optional[str] = None,
    judgment_text: Optional[str] = None,
) -> PrecedentRecord:
    """Save a case as a precedent."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO precedents (case_number, case_type, summary, verdict, sentence, judgment_text)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
            """,
            case_number, case_type, summary, verdict, sentence, judgment_text,
        )
    return _row_to_precedent(row)


async def search_precedents(keyword: str) -> list[PrecedentRecord]:
    """Search precedents by keyword in summary or judgment text."""
    # Escape LIKE wildcard characters in user input
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM precedents
            WHERE summary ILIKE $1 OR judgment_text ILIKE $1 OR case_number ILIKE $1
            ORDER BY created_at DESC
            LIMIT 10
            """,
            f"%{escaped}%",
        )
    return [_row_to_precedent(row) for row in rows]


async def get_all_precedents() -> list[PrecedentRecord]:
    """Get all precedents."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM precedents ORDER BY created_at DESC LIMIT 20"
        )
    return [_row_to_precedent(row) for row in rows]


# --- Case Logs ---

async def add_case_log(
    case_id: int, phase: str, actor: str, content: str,
    action_type: Optional[str] = None,
):
    """Add a log entry for a case."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO case_logs (case_id, phase, actor, content, action_type)
            VALUES ($1, $2, $3, $4, $5)
            """,
            case_id, phase, actor, content, action_type,
        )


async def get_case_logs(case_id: int) -> list[dict]:
    """Get all logs for a case."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM case_logs WHERE case_id = $1 ORDER BY created_at",
            case_id,
        )
    return [dict(row) for row in rows]


# --- Row mappers ---

def _row_to_case(row) -> CaseRecord:
    # case_subtype may not exist in older schemas before migration
    try:
        case_subtype = row["case_subtype"]
    except (KeyError, Exception):
        case_subtype = None
    return CaseRecord(
        id=row["id"],
        case_number=row["case_number"],
        case_type=row["case_type"],
        court_level=row["court_level"],
        phase=row["phase"],
        plaintiff_id=row["plaintiff_id"],
        defendant_id=row["defendant_id"],
        group_id=row["group_id"],
        complaint_text=row["complaint_text"],
        answer_text=row["answer_text"],
        verdict_text=row["verdict_text"],
        sentence=row["sentence"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        appeal_deadline=row["appeal_deadline"],
        parent_case_id=row["parent_case_id"],
        status=row["status"],
        case_subtype=case_subtype,
    )


def _row_to_evidence(row) -> EvidenceRecord:
    return EvidenceRecord(
        id=row["id"],
        case_id=row["case_id"],
        submitter_id=row["submitter_id"],
        content=row["content"],
        evidence_type=row["evidence_type"],
        submitted_at=row["submitted_at"],
    )


def _row_to_precedent(row) -> PrecedentRecord:
    return PrecedentRecord(
        id=row["id"],
        case_number=row["case_number"],
        case_type=row["case_type"],
        summary=row["summary"],
        verdict=row["verdict"],
        sentence=row["sentence"],
        judgment_text=row["judgment_text"],
        created_at=row["created_at"],
    )
