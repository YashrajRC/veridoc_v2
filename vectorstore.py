"""
Vector store (SQLite + numpy).

There is no dedicated vector database in the environment, so this is a small,
honest substitute: passages and their embedding vectors live in a SQLite table
(vectors are stored as float32 blobs), and search is brute-force cosine
similarity in numpy. For a loan file (a few hundred passages per case) this is
instant. It can be swapped for a managed vector DB later without touching the
callers — only `add_passages` and `search` would change.
"""
from __future__ import annotations

import sqlite3
import threading

import numpy as np

import config

_LOCK = threading.Lock()


def _connect() -> sqlite3.Connection:
    config.VEC_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.VEC_DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_vs() -> None:
    with _LOCK:
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS passages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL,
                    item_id TEXT,
                    doc_key TEXT,
                    page INTEGER,
                    status TEXT,
                    text TEXT NOT NULL,
                    dim INTEGER NOT NULL,
                    vec BLOB NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pass_case "
                         "ON passages (case_id)")
            conn.commit()
        finally:
            conn.close()


def clear_case(case_id: str) -> None:
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("DELETE FROM passages WHERE case_id = ?", (case_id,))
            conn.commit()
        finally:
            conn.close()


def add_passages(case_id: str, rows: list[dict]) -> int:
    """rows: list of {doc_key, page, text, item_id?, status?, vec: list[float]}."""
    with _LOCK:
        conn = _connect()
        try:
            n = 0
            for r in rows:
                v = np.asarray(r["vec"], dtype=np.float32)
                if v.ndim != 1 or v.shape[0] == 0:
                    continue
                conn.execute(
                    "INSERT INTO passages "
                    "(case_id, item_id, doc_key, page, status, text, dim, vec) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (case_id, r.get("item_id", ""), r.get("doc_key", ""),
                     r.get("page"), r.get("status", ""), r.get("text", ""),
                     int(v.shape[0]), v.tobytes()),
                )
                n += 1
            conn.commit()
            return n
        finally:
            conn.close()


def all_passages(case_id: str) -> list[dict]:
    """Every stored passage (text + metadata, no vector) for a case. Used by the
    lexical half of hybrid search, which scores raw text and does not need the
    embeddings."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT item_id, doc_key, page, status, text "
            "FROM passages WHERE case_id = ?", (case_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count(case_id: str) -> int:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM passages WHERE case_id = ?",
            (case_id,)).fetchone()
        return int(row["n"]) if row else 0
    finally:
        conn.close()


def search(case_id: str, query_vec, k: int = 8) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT item_id, doc_key, page, status, text, dim, vec "
            "FROM passages WHERE case_id = ?", (case_id,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return []

    q = np.asarray(query_vec, dtype=np.float32)
    qn = float(np.linalg.norm(q))
    if qn == 0.0:
        return []
    q = q / qn
    dim = q.shape[0]

    mats, metas = [], []
    for r in rows:
        if r["dim"] != dim:  # vectors from a different embedding model: skip
            continue
        mats.append(np.frombuffer(r["vec"], dtype=np.float32))
        metas.append(r)
    if not mats:
        return []

    matrix = np.vstack(mats)
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = 1.0
    matrix = matrix / norms[:, None]
    sims = matrix @ q

    order = np.argsort(-sims)[:max(1, k)]
    out = []
    for i in order:
        r = metas[int(i)]
        out.append({
            "doc_key": r["doc_key"], "page": r["page"], "text": r["text"],
            "item_id": r["item_id"], "status": r["status"],
            "score": round(float(sims[int(i)]), 4),
        })
    return out
