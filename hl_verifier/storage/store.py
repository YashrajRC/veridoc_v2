"""
Append-only decision store (SQLite, standard library).

Decisions are never updated in place: re-deciding a line inserts a new row, and
"current state" is the most recent row per (case, item). This preserves a full
audit trail. All queries are parameterised. A module-level lock serialises
writes; reads open their own short-lived connection.
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Optional

from hl_verifier import config
from hl_verifier.models import Decision

_WRITE_LOCK = threading.Lock()


def _connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    with _WRITE_LOCK:
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    note TEXT,
                    ai_status_at_decision TEXT,
                    ai_finding_at_decision TEXT,
                    evidence_doc TEXT,
                    evidence_page INTEGER,
                    evidence_snippet TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_decisions_case "
                "ON decisions (case_id, item_id, id)"
            )
            conn.commit()
        finally:
            conn.close()


def record_decision(d: Decision) -> int:
    with _WRITE_LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO decisions
                  (case_id, item_id, action, reviewer, note,
                   ai_status_at_decision, ai_finding_at_decision,
                   evidence_doc, evidence_page, evidence_snippet, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (d.case_id, d.item_id,
                 d.action.value if hasattr(d.action, "value") else d.action,
                 d.reviewer, d.note, d.ai_status_at_decision,
                 d.ai_finding_at_decision, d.evidence_doc, d.evidence_page,
                 d.evidence_snippet, d.created_at),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()


def latest_decision_per_item(case_id: str) -> dict[str, dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT d.* FROM decisions d
            JOIN (
                SELECT item_id, MAX(id) AS max_id
                FROM decisions WHERE case_id = ?
                GROUP BY item_id
            ) m ON d.item_id = m.item_id AND d.id = m.max_id
            WHERE d.case_id = ?
            """,
            (case_id, case_id),
        ).fetchall()
        return {r["item_id"]: dict(r) for r in rows}
    finally:
        conn.close()


def history_for_case(case_id: str, item_id: Optional[str] = None) -> list[dict]:
    conn = _connect()
    try:
        if item_id:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE case_id = ? AND item_id = ? "
                "ORDER BY id DESC",
                (case_id, item_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE case_id = ? ORDER BY id DESC",
                (case_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
