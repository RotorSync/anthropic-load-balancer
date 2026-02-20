"""
SQLite storage for request tracking and usage analytics.
"""
import asyncio
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default database path
DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "usage.db"


@dataclass
class RequestRecord:
    """A single request record."""
    id: Optional[int]
    timestamp: datetime
    client_id: str
    subscription: str
    model: str
    input_tokens: int
    output_tokens: int
    status_code: int
    latency_ms: int


@dataclass 
class ClientStats:
    """Aggregated stats for a client."""
    client_id: str
    total_requests: int
    total_input_tokens: int
    total_output_tokens: int
    last_seen: datetime
    current_subscription: Optional[str] = None
    active_requests: int = 0


@dataclass
class UsageStats:
    """Usage statistics for a time period."""
    period: str  # "day", "week", "month"
    start_time: datetime
    end_time: datetime
    total_requests: int
    total_input_tokens: int
    total_output_tokens: int
    by_client: dict  # client_id -> {requests, input_tokens, output_tokens}
    by_subscription: dict  # subscription -> {requests, input_tokens, output_tokens}


class UsageStorage:
    """
    SQLite-based storage for usage tracking.
    
    Thread-safe via connection-per-call pattern.
    """
    
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self._ensure_db_dir()
        self._init_schema()
        self._lock = asyncio.Lock()
        logger.info(f"Usage storage initialized at {self.db_path}")
    
    def _ensure_db_dir(self):
        """Ensure the database directory exists."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
    
    @contextmanager
    def _get_conn(self):
        """Get a database connection (context manager)."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def _init_schema(self):
        """Initialize database schema."""
        with self._get_conn() as conn:
            conn.executescript("""
                -- Request log table
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    subscription TEXT NOT NULL,
                    model TEXT DEFAULT '',
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    status_code INTEGER DEFAULT 0,
                    latency_ms INTEGER DEFAULT 0
                );
                
                -- Indexes for common queries
                CREATE INDEX IF NOT EXISTS idx_requests_timestamp 
                    ON requests(timestamp);
                CREATE INDEX IF NOT EXISTS idx_requests_client 
                    ON requests(client_id, timestamp);
                CREATE INDEX IF NOT EXISTS idx_requests_subscription 
                    ON requests(subscription, timestamp);
                
                -- Client tracking table (for last-seen and active state)
                CREATE TABLE IF NOT EXISTS clients (
                    client_id TEXT PRIMARY KEY,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    total_requests INTEGER DEFAULT 0
                );
                
                -- Daily aggregates for faster queries
                CREATE TABLE IF NOT EXISTS daily_usage (
                    date TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    subscription TEXT NOT NULL,
                    requests INTEGER DEFAULT 0,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    PRIMARY KEY (date, client_id, subscription)
                );
            """)
        logger.debug("Database schema initialized")
    
    async def record_request(
        self,
        client_id: str,
        subscription: str,
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        status_code: int = 200,
        latency_ms: int = 0,
    ):
        """Record a completed request."""
        async with self._lock:
            now = datetime.utcnow()
            today = now.strftime("%Y-%m-%d")
            timestamp = now.isoformat()
            
            with self._get_conn() as conn:
                # Insert request record
                conn.execute("""
                    INSERT INTO requests 
                    (timestamp, client_id, subscription, model, input_tokens, output_tokens, status_code, latency_ms)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (timestamp, client_id, subscription, model, input_tokens, output_tokens, status_code, latency_ms))
                
                # Update client tracking
                conn.execute("""
                    INSERT INTO clients (client_id, first_seen, last_seen, total_requests)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(client_id) DO UPDATE SET
                        last_seen = excluded.last_seen,
                        total_requests = total_requests + 1
                """, (client_id, timestamp, timestamp))
                
                # Update daily aggregate
                conn.execute("""
                    INSERT INTO daily_usage (date, client_id, subscription, requests, input_tokens, output_tokens)
                    VALUES (?, ?, ?, 1, ?, ?)
                    ON CONFLICT(date, client_id, subscription) DO UPDATE SET
                        requests = requests + 1,
                        input_tokens = input_tokens + excluded.input_tokens,
                        output_tokens = output_tokens + excluded.output_tokens
                """, (today, client_id, subscription, input_tokens, output_tokens))
    
    async def get_clients(self, active_minutes: int = 5) -> list[ClientStats]:
        """Get all known clients with stats."""
        async with self._lock:
            cutoff = (datetime.utcnow() - timedelta(minutes=active_minutes)).isoformat()
            
            with self._get_conn() as conn:
                # Get all clients with their totals
                rows = conn.execute("""
                    SELECT 
                        c.client_id,
                        c.total_requests,
                        c.last_seen,
                        COALESCE(SUM(d.input_tokens), 0) as total_input,
                        COALESCE(SUM(d.output_tokens), 0) as total_output
                    FROM clients c
                    LEFT JOIN daily_usage d ON c.client_id = d.client_id
                    GROUP BY c.client_id
                    ORDER BY c.last_seen DESC
                """).fetchall()
                
                clients = []
                for row in rows:
                    last_seen = datetime.fromisoformat(row["last_seen"])
                    clients.append(ClientStats(
                        client_id=row["client_id"],
                        total_requests=row["total_requests"],
                        total_input_tokens=row["total_input"],
                        total_output_tokens=row["total_output"],
                        last_seen=last_seen,
                    ))
                
                return clients
    
    async def get_usage(self, period: str = "day") -> UsageStats:
        """
        Get usage statistics for a time period.
        
        Args:
            period: "day", "week", or "month"
        """
        async with self._lock:
            now = datetime.utcnow()
            
            if period == "day":
                start = now - timedelta(days=1)
            elif period == "week":
                start = now - timedelta(weeks=1)
            elif period == "month":
                start = now - timedelta(days=30)
            else:
                start = now - timedelta(days=1)
            
            start_date = start.strftime("%Y-%m-%d")
            
            with self._get_conn() as conn:
                # Overall totals
                totals = conn.execute("""
                    SELECT 
                        COALESCE(SUM(requests), 0) as total_requests,
                        COALESCE(SUM(input_tokens), 0) as total_input,
                        COALESCE(SUM(output_tokens), 0) as total_output
                    FROM daily_usage
                    WHERE date >= ?
                """, (start_date,)).fetchone()
                
                # By client
                by_client_rows = conn.execute("""
                    SELECT 
                        client_id,
                        SUM(requests) as requests,
                        SUM(input_tokens) as input_tokens,
                        SUM(output_tokens) as output_tokens
                    FROM daily_usage
                    WHERE date >= ?
                    GROUP BY client_id
                    ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
                """, (start_date,)).fetchall()
                
                by_client = {
                    row["client_id"]: {
                        "requests": row["requests"],
                        "input_tokens": row["input_tokens"],
                        "output_tokens": row["output_tokens"],
                    }
                    for row in by_client_rows
                }
                
                # By subscription
                by_sub_rows = conn.execute("""
                    SELECT 
                        subscription,
                        SUM(requests) as requests,
                        SUM(input_tokens) as input_tokens,
                        SUM(output_tokens) as output_tokens
                    FROM daily_usage
                    WHERE date >= ?
                    GROUP BY subscription
                    ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
                """, (start_date,)).fetchall()
                
                by_subscription = {
                    row["subscription"]: {
                        "requests": row["requests"],
                        "input_tokens": row["input_tokens"],
                        "output_tokens": row["output_tokens"],
                    }
                    for row in by_sub_rows
                }
                
                return UsageStats(
                    period=period,
                    start_time=start,
                    end_time=now,
                    total_requests=totals["total_requests"],
                    total_input_tokens=totals["total_input"],
                    total_output_tokens=totals["total_output"],
                    by_client=by_client,
                    by_subscription=by_subscription,
                )
    
    async def get_client_usage(self, client_id: str, period: str = "day") -> dict:
        """Get detailed usage for a specific client."""
        async with self._lock:
            now = datetime.utcnow()
            
            if period == "day":
                start = now - timedelta(days=1)
            elif period == "week":
                start = now - timedelta(weeks=1)
            elif period == "month":
                start = now - timedelta(days=30)
            else:
                start = now - timedelta(days=1)
            
            start_date = start.strftime("%Y-%m-%d")
            
            with self._get_conn() as conn:
                # Daily breakdown
                daily = conn.execute("""
                    SELECT 
                        date,
                        SUM(requests) as requests,
                        SUM(input_tokens) as input_tokens,
                        SUM(output_tokens) as output_tokens
                    FROM daily_usage
                    WHERE client_id = ? AND date >= ?
                    GROUP BY date
                    ORDER BY date
                """, (client_id, start_date)).fetchall()
                
                # By subscription
                by_sub = conn.execute("""
                    SELECT 
                        subscription,
                        SUM(requests) as requests,
                        SUM(input_tokens) as input_tokens,
                        SUM(output_tokens) as output_tokens
                    FROM daily_usage
                    WHERE client_id = ? AND date >= ?
                    GROUP BY subscription
                """, (client_id, start_date)).fetchall()
                
                return {
                    "client_id": client_id,
                    "period": period,
                    "daily": [
                        {
                            "date": row["date"],
                            "requests": row["requests"],
                            "input_tokens": row["input_tokens"],
                            "output_tokens": row["output_tokens"],
                        }
                        for row in daily
                    ],
                    "by_subscription": {
                        row["subscription"]: {
                            "requests": row["requests"],
                            "input_tokens": row["input_tokens"],
                            "output_tokens": row["output_tokens"],
                        }
                        for row in by_sub
                    },
                }
    
    async def cleanup_old_requests(self, days: int = 90):
        """Remove request records older than specified days (keeps aggregates)."""
        async with self._lock:
            cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
            
            with self._get_conn() as conn:
                result = conn.execute("""
                    DELETE FROM requests WHERE timestamp < ?
                """, (cutoff,))
                
                deleted = result.rowcount
                if deleted > 0:
                    logger.info(f"Cleaned up {deleted} old request records")
                
                return deleted
