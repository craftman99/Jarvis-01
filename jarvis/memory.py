"""Jarvis's long-term memory: posts, the approval queue, interactions, metrics and notes (SQLite)."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    text TEXT NOT NULL,
    media_url TEXT,
    reply_to_remote_id TEXT,          -- set when this post is a reply to a mention/comment
    interaction_id INTEGER,
    status TEXT NOT NULL,             -- pending_approval | scheduled | published | failed | rejected
    scheduled_at TEXT,                -- UTC ISO-8601
    published_at TEXT,
    remote_id TEXT,
    url TEXT,
    error TEXT,
    rationale TEXT,                   -- why Jarvis wrote it (shown in the approval queue)
    meta TEXT,                        -- JSON extras, e.g. YouTube title/tags/file
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    author TEXT,
    text TEXT,
    kind TEXT,                        -- mention | reply | comment
    status TEXT NOT NULL DEFAULT 'new', -- new | handled | ignored | flagged
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(platform, remote_id)
);
CREATE TABLE IF NOT EXISTS metrics (
    post_id INTEGER PRIMARY KEY,
    likes INTEGER DEFAULT 0,
    reposts INTEGER DEFAULT 0,
    replies INTEGER DEFAULT 0,
    impressions INTEGER DEFAULT 0,
    fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,               -- paper | live
    market TEXT NOT NULL,             -- stock | crypto | dex
    asset TEXT NOT NULL,              -- AAPL, DOGE/USD, or a token contract address
    symbol TEXT NOT NULL,             -- display name
    chain TEXT,                       -- dex tokens: solana | ethereum | base
    side TEXT NOT NULL,               -- buy | sell
    qty REAL,
    price REAL,
    usd REAL NOT NULL,
    status TEXT NOT NULL,             -- pending_approval | filled | rejected | failed
    reason TEXT,
    stop_loss_pct REAL,
    take_profit_pct REAL,
    broker_order_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    filled_at TEXT
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Memory:
    def __init__(self, db_path: str):
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # Shared between the scheduler thread and the Telegram thread.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(posts)")}
            if "meta" not in cols:  # databases created before YouTube support
                self._conn.execute("ALTER TABLE posts ADD COLUMN meta TEXT")
                self._conn.commit()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # ---- posts -------------------------------------------------------------
    def add_post(self, platform: str, text: str, status: str, *, media_url: str | None = None,
                 scheduled_at: datetime | None = None, reply_to_remote_id: str | None = None,
                 interaction_id: int | None = None, rationale: str | None = None,
                 meta: dict | None = None) -> int:
        cur = self._exec(
            "INSERT INTO posts (platform, text, media_url, reply_to_remote_id, interaction_id, status,"
            " scheduled_at, rationale, meta, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (platform, text, media_url, reply_to_remote_id, interaction_id, status,
             iso(scheduled_at) if scheduled_at else None, rationale,
             json.dumps(meta) if meta else None, iso(utcnow())),
        )
        return cur.lastrowid

    def get_post(self, post_id: int) -> dict | None:
        rows = self._all("SELECT * FROM posts WHERE id = ?", (post_id,))
        return rows[0] if rows else None

    def update_post(self, post_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE posts SET {cols} WHERE id = ?", (*fields.values(), post_id))

    def list_posts(self, status: str | None = None, platform: str | None = None, limit: int = 20) -> list[dict]:
        sql, params = "SELECT * FROM posts WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if platform:
            sql += " AND platform = ?"
            params.append(platform)
        sql += " ORDER BY COALESCE(scheduled_at, created_at) DESC LIMIT ?"
        params.append(limit)
        return self._all(sql, tuple(params))

    def due_posts(self, now: datetime | None = None) -> list[dict]:
        return self._all(
            "SELECT * FROM posts WHERE status = 'scheduled' AND (scheduled_at IS NULL OR scheduled_at <= ?)"
            " ORDER BY scheduled_at",
            (iso(now or utcnow()),),
        )

    def count_original_posts_between(self, platform: str, start: datetime, end: datetime) -> int:
        """Original (non-reply) posts planned or published for a platform in a time window."""
        rows = self._all(
            "SELECT COUNT(*) AS n FROM posts WHERE platform = ? AND reply_to_remote_id IS NULL"
            " AND status IN ('pending_approval','scheduled','published')"
            " AND COALESCE(scheduled_at, published_at, created_at) >= ?"
            " AND COALESCE(scheduled_at, published_at, created_at) < ?",
            (platform, iso(start), iso(end)),
        )
        return rows[0]["n"]

    # ---- interactions ------------------------------------------------------
    def add_interaction(self, platform: str, remote_id: str, author: str, text: str, kind: str) -> int | None:
        """Store a mention/comment. Returns the new id, or None if already known."""
        cur = self._exec(
            "INSERT OR IGNORE INTO interactions (platform, remote_id, author, text, kind, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (platform, remote_id, author, text, kind, iso(utcnow())),
        )
        return cur.lastrowid if cur.rowcount else None

    def get_interaction(self, interaction_id: int) -> dict | None:
        rows = self._all("SELECT * FROM interactions WHERE id = ?", (interaction_id,))
        return rows[0] if rows else None

    def list_interactions(self, status: str | None = "new", limit: int = 20) -> list[dict]:
        if status:
            return self._all("SELECT * FROM interactions WHERE status = ? ORDER BY id LIMIT ?", (status, limit))
        return self._all("SELECT * FROM interactions ORDER BY id DESC LIMIT ?", (limit,))

    def set_interaction_status(self, interaction_id: int, status: str, note: str | None = None) -> None:
        self._exec("UPDATE interactions SET status = ?, note = COALESCE(?, note) WHERE id = ?",
                   (status, note, interaction_id))

    # ---- metrics -----------------------------------------------------------
    def save_metrics(self, post_id: int, likes: int = 0, reposts: int = 0, replies: int = 0,
                     impressions: int = 0) -> None:
        self._exec(
            "INSERT INTO metrics (post_id, likes, reposts, replies, impressions, fetched_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(post_id) DO UPDATE SET likes=excluded.likes, reposts=excluded.reposts,"
            " replies=excluded.replies, impressions=excluded.impressions, fetched_at=excluded.fetched_at",
            (post_id, likes, reposts, replies, impressions, iso(utcnow())),
        )

    def performance(self, since: datetime, limit: int = 10) -> list[dict]:
        """Published original posts since a date, best-performing first."""
        return self._all(
            "SELECT p.id, p.platform, p.text, p.published_at, p.url,"
            " COALESCE(m.likes,0) AS likes, COALESCE(m.reposts,0) AS reposts,"
            " COALESCE(m.replies,0) AS replies, COALESCE(m.impressions,0) AS impressions,"
            " COALESCE(m.likes,0) + 2*COALESCE(m.reposts,0) + 3*COALESCE(m.replies,0) AS score"
            " FROM posts p LEFT JOIN metrics m ON m.post_id = p.id"
            " WHERE p.status = 'published' AND p.reply_to_remote_id IS NULL AND p.published_at >= ?"
            " ORDER BY score DESC LIMIT ?",
            (iso(since), limit),
        )

    def queued_video_files(self) -> set[str]:
        """Video files already handed to YouTube posts (any status), so they aren't queued twice."""
        rows = self._all("SELECT meta FROM posts WHERE platform = 'youtube' AND meta IS NOT NULL")
        return {json.loads(r["meta"]).get("file", "") for r in rows}

    # ---- trades --------------------------------------------------------------
    def add_trade(self, **fields) -> int:
        fields.setdefault("created_at", iso(utcnow()))
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        return self._exec(f"INSERT INTO trades ({cols}) VALUES ({marks})", tuple(fields.values())).lastrowid

    def get_trade(self, trade_id: int) -> dict | None:
        rows = self._all("SELECT * FROM trades WHERE id = ?", (trade_id,))
        return rows[0] if rows else None

    def update_trade(self, trade_id: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE trades SET {cols} WHERE id = ?", (*fields.values(), trade_id))

    def list_trades(self, mode: str | None = None, status: str | None = None, limit: int = 500) -> list[dict]:
        sql, params = "SELECT * FROM trades WHERE 1=1", []
        if mode:
            sql += " AND mode = ?"
            params.append(mode)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id LIMIT ?"
        params.append(limit)
        return self._all(sql, tuple(params))

    # ---- small key/value state (kill switch, alert baselines) --------------
    def get_state(self, key: str, default=None):
        rows = self._all("SELECT value FROM state WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_state(self, key: str, value) -> None:
        self._exec("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value)))

    # ---- notes (long-term facts Jarvis learns) -----------------------------
    def remember(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO notes (key, value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key.strip().lower(), value, iso(utcnow())),
        )

    def forget(self, key: str) -> bool:
        return self._exec("DELETE FROM notes WHERE key = ?", (key.strip().lower(),)).rowcount > 0

    def notes(self) -> list[dict]:
        return self._all("SELECT key, value, updated_at FROM notes ORDER BY key")
