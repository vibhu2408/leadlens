"""SQLite persistence layer.

SQLite in WAL mode is plenty for a single-instance deployment (thousands of
leads per batch, concurrent reads while a background job writes). Every query
goes through this module, so swapping to Postgres (Cloud SQL / RDS) later means
changing this file only.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

DB_PATH = os.environ.get("LEADLENS_DB", os.path.join(os.path.dirname(__file__), "..", "leadlens.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    preset      TEXT NOT NULL,
    icp_json    TEXT NOT NULL,
    enrich      INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL,          -- queued | processing | done | error
    stage       TEXT,
    total       INTEGER NOT NULL DEFAULT 0,
    processed   INTEGER NOT NULL DEFAULT 0,
    stats_json  TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    data_json       TEXT NOT NULL,
    domain          TEXT,
    dup_count       INTEGER NOT NULL DEFAULT 0,
    email_status    TEXT NOT NULL DEFAULT 'missing',
    email_flags     TEXT NOT NULL DEFAULT '[]',
    enrichment_json TEXT NOT NULL DEFAULT '{}',
    score           INTEGER NOT NULL DEFAULT 0,
    tier            TEXT NOT NULL DEFAULT 'D',
    reasons_json    TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'new',
    opener          TEXT
);
CREATE INDEX IF NOT EXISTS idx_leads_batch ON leads(batch_id, score DESC);

-- Cross-batch caches: a domain scraped once is reused for 7 days by every batch.
CREATE TABLE IF NOT EXISTS enrichment_cache (
    domain      TEXT PRIMARY KEY,
    data_json   TEXT NOT NULL,
    fetched_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mx_cache (
    domain      TEXT PRIMARY KEY,
    has_mx      INTEGER NOT NULL,
    checked_at  REAL NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connection() as c:
        c.executescript(SCHEMA)


# ---------- batches ----------

def create_batch(batch_id: str, name: str, preset: str, icp: dict, enrich: bool) -> None:
    with connection() as c:
        c.execute(
            "INSERT INTO batches (id, name, created_at, preset, icp_json, enrich, status, stage) "
            "VALUES (?, ?, ?, ?, ?, ?, 'queued', 'Queued')",
            (batch_id, name, time.time(), preset, json.dumps(icp), int(enrich)),
        )


def update_batch(batch_id: str, **fields: Any) -> None:
    if "icp" in fields:
        fields["icp_json"] = json.dumps(fields.pop("icp"))
    if "stats" in fields:
        fields["stats_json"] = json.dumps(fields.pop("stats"))
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connection() as c:
        c.execute(f"UPDATE batches SET {cols} WHERE id = ?", (*fields.values(), batch_id))


def _batch_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["icp"] = json.loads(d.pop("icp_json"))
    d["stats"] = json.loads(d.pop("stats_json"))
    d["enrich"] = bool(d["enrich"])
    return d


def get_batch(batch_id: str) -> dict | None:
    with connection() as c:
        row = c.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    return _batch_row(row) if row else None


def list_batches() -> list[dict]:
    with connection() as c:
        rows = c.execute("SELECT * FROM batches ORDER BY created_at DESC LIMIT 50").fetchall()
    return [_batch_row(r) for r in rows]


def delete_batch(batch_id: str) -> None:
    with connection() as c:
        c.execute("DELETE FROM leads WHERE batch_id = ?", (batch_id,))
        c.execute("DELETE FROM batches WHERE id = ?", (batch_id,))


# ---------- leads ----------

def insert_leads(batch_id: str, leads: list[dict]) -> None:
    with connection() as c:
        c.executemany(
            "INSERT INTO leads (batch_id, data_json, domain, dup_count) VALUES (?, ?, ?, ?)",
            [(batch_id, json.dumps(l["data"]), l.get("domain"), l.get("dup_count", 0)) for l in leads],
        )


def _lead_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["data"] = json.loads(d.pop("data_json"))
    d["email_flags"] = json.loads(d["email_flags"])
    d["enrichment"] = json.loads(d.pop("enrichment_json"))
    d["reasons"] = json.loads(d.pop("reasons_json"))
    return d


def get_leads(batch_id: str) -> list[dict]:
    with connection() as c:
        rows = c.execute(
            "SELECT * FROM leads WHERE batch_id = ? ORDER BY score DESC, id", (batch_id,)
        ).fetchall()
    return [_lead_row(r) for r in rows]


def get_lead(lead_id: int) -> dict | None:
    with connection() as c:
        row = c.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    return _lead_row(row) if row else None


def update_lead(lead_id: int, **fields: Any) -> None:
    mapping = {"enrichment": "enrichment_json", "reasons": "reasons_json"}
    out: dict[str, Any] = {}
    for k, v in fields.items():
        col = mapping.get(k, k)
        out[col] = json.dumps(v) if isinstance(v, (dict, list)) else v
    cols = ", ".join(f"{k} = ?" for k in out)
    with connection() as c:
        c.execute(f"UPDATE leads SET {cols} WHERE id = ?", (*out.values(), lead_id))


def update_leads_bulk(rows: list[tuple[int, dict]]) -> None:
    """Write many leads' computed fields in one transaction."""
    with connection() as c:
        for lead_id, f in rows:
            c.execute(
                "UPDATE leads SET data_json=?, email_status=?, email_flags=?, enrichment_json=?, score=?, tier=?, "
                "reasons_json=? WHERE id=?",
                (
                    json.dumps(f["data"]), f["email_status"], json.dumps(f["email_flags"]), json.dumps(f["enrichment"]),
                    f["score"], f["tier"], json.dumps(f["reasons"]), lead_id,
                ),
            )


# ---------- caches ----------

ENRICH_TTL = 7 * 24 * 3600
MX_TTL = 3 * 24 * 3600


def cache_get_enrichment(domain: str) -> dict | None:
    with connection() as c:
        row = c.execute("SELECT data_json, fetched_at FROM enrichment_cache WHERE domain = ?", (domain,)).fetchone()
    if row and time.time() - row["fetched_at"] < ENRICH_TTL:
        return json.loads(row["data_json"])
    return None


def cache_put_enrichment(domain: str, data: dict) -> None:
    with connection() as c:
        c.execute(
            "INSERT OR REPLACE INTO enrichment_cache (domain, data_json, fetched_at) VALUES (?, ?, ?)",
            (domain, json.dumps(data), time.time()),
        )


def cache_get_mx(domain: str) -> bool | None:
    with connection() as c:
        row = c.execute("SELECT has_mx, checked_at FROM mx_cache WHERE domain = ?", (domain,)).fetchone()
    if row and time.time() - row["checked_at"] < MX_TTL:
        return bool(row["has_mx"])
    return None


def cache_put_mx(domain: str, has_mx: bool) -> None:
    with connection() as c:
        c.execute(
            "INSERT OR REPLACE INTO mx_cache (domain, has_mx, checked_at) VALUES (?, ?, ?)",
            (domain, int(has_mx), time.time()),
        )
