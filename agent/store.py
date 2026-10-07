"""Small SQLite store for agent PR lineage + outcomes.

Lives on a Docker volume (AGENT_DATA_DIR, default /data) so acceptance/edit/
rejection rates and time-to-merge survive container restarts — Prometheus
counters alone would reset on every redeploy. The PR description's hidden
lineage marker remains the source of truth; this is a queryable cache of it.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None
_CONN_PATH: str | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_prs (
    pr_number        INTEGER PRIMARY KEY,
    pr_url           TEXT,
    slug             TEXT,
    partner_name     TEXT,
    request_text     TEXT,
    prompt_version   TEXT,
    model            TEXT,
    fallback_used    INTEGER DEFAULT 0,
    trace_id         TEXT,
    trace_url        TEXT,
    branch           TEXT,
    agent_commit_sha TEXT,
    created_at       REAL,
    closed_at        REAL,
    outcome          TEXT DEFAULT 'open',
    closed_by        TEXT,
    cost_usd         REAL,
    cost_source      TEXT,
    extracted_spec   TEXT,
    corrected_spec   TEXT,
    correction_source TEXT,
    candidate_path   TEXT,
    candidate_pr_url TEXT,
    notes            TEXT,
    updated_at       REAL
);
"""

COLUMNS = [
    "pr_number", "pr_url", "slug", "partner_name", "request_text", "prompt_version",
    "model", "fallback_used", "trace_id", "trace_url", "branch", "agent_commit_sha",
    "created_at", "closed_at", "outcome", "closed_by", "cost_usd", "cost_source",
    "extracted_spec", "corrected_spec", "correction_source", "candidate_path",
    "candidate_pr_url", "notes", "updated_at",
]
_JSON_COLS = {"extracted_spec", "corrected_spec"}


def db_path() -> str:
    return os.getenv("AGENT_DB_PATH") or str(Path(os.getenv("AGENT_DATA_DIR", "/data")) / "agent.db")


def _conn() -> sqlite3.Connection:
    global _CONN, _CONN_PATH
    path = db_path()
    if _CONN is None or _CONN_PATH != path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        _CONN = sqlite3.connect(path, check_same_thread=False)
        _CONN.row_factory = sqlite3.Row
        _CONN.execute("PRAGMA journal_mode=WAL")
        _CONN.executescript(_SCHEMA)
        _CONN_PATH = path
    return _CONN


def upsert_pr(pr_number: int, **fields) -> None:
    fields = {k: v for k, v in fields.items() if k in COLUMNS and k != "pr_number"}
    for k in _JSON_COLS & fields.keys():
        if fields[k] is not None and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k])
    if "fallback_used" in fields:
        fields["fallback_used"] = int(bool(fields["fallback_used"]))
    fields["updated_at"] = time.time()
    cols = ["pr_number", *fields.keys()]
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{k}=excluded.{k}" for k in fields.keys())
    with _LOCK:
        c = _conn()
        c.execute(
            f"INSERT INTO agent_prs ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(pr_number) DO UPDATE SET {updates}",
            [pr_number, *fields.values()],
        )
        c.commit()


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in _JSON_COLS:
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                pass
    return d


def get_pr(pr_number: int) -> dict | None:
    with _LOCK:
        row = _conn().execute("SELECT * FROM agent_prs WHERE pr_number=?", (pr_number,)).fetchone()
    return _row_to_dict(row) if row else None


def all_prs() -> list[dict]:
    with _LOCK:
        rows = _conn().execute("SELECT * FROM agent_prs ORDER BY pr_number").fetchall()
    return [_row_to_dict(r) for r in rows]


def prs_needing_cost(max_age_seconds: float) -> list[dict]:
    cutoff = time.time() - max_age_seconds
    with _LOCK:
        rows = _conn().execute(
            "SELECT * FROM agent_prs WHERE trace_id IS NOT NULL "
            "AND (cost_source IS NULL OR cost_source != 'langfuse') AND created_at >= ?",
            (cutoff,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]
