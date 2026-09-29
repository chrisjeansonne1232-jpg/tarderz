"""SQLite persistence. Everything the bot sees or decides is logged here."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .markets import MarketWindow

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS markets (
    slug TEXT PRIMARY KEY,
    series TEXT, event_id TEXT, market_id TEXT, condition_id TEXT,
    question TEXT, description TEXT, resolution_source TEXT,
    start_ts REAL, end_ts REAL, up_token TEXT, down_token TEXT,
    tick_size REAL, min_order_size REAL, fees_enabled INTEGER,
    fee_rate REAL, fee_exponent REAL, fee_source TEXT,
    gamma_fee_rate REAL, gamma_fee_exponent REAL,
    rules_ok INTEGER, rules_notes TEXT, discovered_at REAL,
    s0_chainlink REAL, s0_chainlink_ts REAL, s0_coinbase REAL, s0_status TEXT,
    end_chainlink REAL, end_chainlink_ts REAL, end_coinbase REAL, end_status TEXT,
    chainlink_predicted TEXT, ws_resolved_outcome TEXT,
    resolved_outcome TEXT, resolved_at REAL, resolution_detail TEXT
);

CREATE TABLE IF NOT EXISTS fv_snapshots (
    id INTEGER PRIMARY KEY,
    ts REAL, slug TEXT, tau REAL,
    spot REAL, spot_adj REAL, strike REAL, sigma_annual REAL, basis REAL, p_up REAL,
    up_bid REAL, up_bid_sz REAL, up_ask REAL, up_ask_sz REAL,
    down_bid REAL, down_bid_sz REAL, down_ask REAL, down_ask_sz REAL,
    spot_age_ms REAL, oracle_age_ms REAL, book_age_ms REAL
);
CREATE INDEX IF NOT EXISTS fv_snapshots_slug ON fv_snapshots(slug);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, ts REAL, level TEXT, kind TEXT, slug TEXT, detail TEXT
);
"""


class Database:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None)  # autocommit
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')")

    def close(self) -> None:
        self.conn.close()

    def log_event(self, level: str, kind: str, slug: str | None, detail: Any) -> None:
        if not isinstance(detail, str):
            detail = json.dumps(detail, default=str)
        self.conn.execute(
            "INSERT INTO events(ts, level, kind, slug, detail) VALUES (?,?,?,?,?)",
            (time.time(), level, kind, slug, detail),
        )

    def upsert_market(self, w: "MarketWindow") -> None:
        fee = w.fee
        self.conn.execute(
            """INSERT INTO markets(slug, series, event_id, market_id, condition_id, question, description,
                   resolution_source, start_ts, end_ts, up_token, down_token, tick_size, min_order_size,
                   fees_enabled, fee_rate, fee_exponent, fee_source, gamma_fee_rate, gamma_fee_exponent,
                   rules_ok, rules_notes, discovered_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(slug) DO UPDATE SET
                   fee_rate=excluded.fee_rate, fee_exponent=excluded.fee_exponent,
                   fee_source=excluded.fee_source, rules_ok=excluded.rules_ok,
                   rules_notes=excluded.rules_notes, tick_size=excluded.tick_size""",
            (
                w.slug, w.series, w.event_id, w.market_id, w.condition_id, w.question, w.description,
                w.resolution_source, w.start_ts, w.end_ts, w.up_token, w.down_token, w.tick_size,
                w.min_order_size, None if w.fees_enabled is None else int(bool(w.fees_enabled)),
                fee.rate if fee else None, fee.exponent if fee else None, fee.source if fee else None,
                w.gamma_fee[0] if w.gamma_fee else None, w.gamma_fee[1] if w.gamma_fee else None,
                int(w.rules_ok), json.dumps(w.rules_notes), time.time(),
            ),
        )

    def update_boundaries(self, w: "MarketWindow") -> None:
        self.conn.execute(
            """UPDATE markets SET s0_chainlink=?, s0_chainlink_ts=?, s0_coinbase=?, s0_status=?,
                   end_chainlink=?, end_chainlink_ts=?, end_coinbase=?, end_status=?,
                   chainlink_predicted=? WHERE slug=?""",
            (
                w.s0_chainlink, w.s0_chainlink_ts, w.s0_coinbase, w.s0_status,
                w.end_chainlink, w.end_chainlink_ts, w.end_coinbase, w.end_status,
                w.chainlink_predicted_outcome(), w.slug,
            ),
        )

    def set_ws_resolution(self, slug: str, outcome: str) -> None:
        self.conn.execute("UPDATE markets SET ws_resolved_outcome=? WHERE slug=?", (outcome, slug))

    def set_resolution(self, slug: str, outcome: str, detail: dict) -> None:
        self.conn.execute(
            "UPDATE markets SET resolved_outcome=?, resolved_at=?, resolution_detail=? WHERE slug=?",
            (outcome, time.time(), json.dumps(detail, default=str), slug),
        )

    def unresolved_markets(self, before_ts: float) -> list[tuple[str, str, float]]:
        rows = self.conn.execute(
            "SELECT slug, market_id, end_ts FROM markets WHERE resolved_outcome IS NULL AND end_ts < ?",
            (before_ts,),
        ).fetchall()
        return [(r[0], r[1] or "", float(r[2])) for r in rows]

    def insert_snapshot(self, row: dict[str, Any]) -> None:
        cols = ",".join(row)
        qs = ",".join("?" for _ in row)
        self.conn.execute(f"INSERT INTO fv_snapshots({cols}) VALUES ({qs})", tuple(row.values()))

    def oracle_check(self) -> tuple[int, int]:
        """(agreements, comparisons) between our Chainlink-based predicted
        outcome and Polymarket's posted resolution."""
        row = self.conn.execute(
            """SELECT SUM(chainlink_predicted = resolved_outcome), COUNT(*) FROM markets
               WHERE chainlink_predicted IS NOT NULL AND resolved_outcome IS NOT NULL"""
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0)
