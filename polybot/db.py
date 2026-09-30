"""SQLite persistence. Everything the bot sees or decides is logged here."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

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
    resolved_outcome TEXT, resolved_at REAL, resolution_detail TEXT,
    ptb_polymarket REAL, ptb_seen_at REAL
);

-- One row per series per second: spot, oracle, fair value and top of book,
-- plus a zlib-compressed JSON ladder (top N levels, feed ages) for replay.
CREATE TABLE IF NOT EXISTS snapshots_1s (
    ts REAL, series TEXT, slug TEXT, tau REAL,
    spot REAL, chainlink REAL, basis REAL, sigma_annual REAL, s0 REAL, fair_up REAL,
    up_bid REAL, up_ask REAL, down_bid REAL, down_ask REAL,
    blob BLOB
);
CREATE INDEX IF NOT EXISTS snapshots_1s_ts ON snapshots_1s(ts);

CREATE TABLE IF NOT EXISTS candles_1m (
    ts REAL PRIMARY KEY, open REAL, high REAL, low REAL, close REAL, ticks INTEGER, source TEXT
);

-- Every evaluated opportunity: taken signals and (throttled) skips.
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY, ts REAL, slug TEXT, series TEXT, side TEXT, token TEXT,
    tau REAL, spot REAL, s0 REAL, sigma_annual REAL, fair REAL,
    best_ask REAL, best_ask_size REAL, vwap REAL, fee_ps REAL,
    edge_gross REAL, edge_after_fee REAL, slippage REAL, net_edge REAL, threshold REAL,
    shares REAL, usd REAL, decision TEXT, reason TEXT, trade_id INTEGER
);
CREATE INDEX IF NOT EXISTS signals_ts ON signals(ts);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY, signal_id INTEGER, slug TEXT, series TEXT, side TEXT, token TEXT,
    ts_signal REAL, ts_fill REAL, latency_ms REAL,
    shares REAL, shares_held REAL, avg_price REAL, signal_ask REAL, signal_vwap REAL,
    cost REAL, fee REAL, fee_in TEXT, fair_signal REAL, fair_fill REAL, edge_entry REAL,
    levels TEXT, status TEXT, window_end REAL, outcome TEXT, payout REAL,
    pnl REAL, pnl_zero_fee REAL, settled_ts REAL
);

CREATE TABLE IF NOT EXISTS exec_log (
    id INTEGER PRIMARY KEY, ts REAL, tag TEXT, msg TEXT, ref TEXT
);
CREATE INDEX IF NOT EXISTS exec_log_ts ON exec_log(ts);

-- How every evaluation of each window+side ended (full counts; the signals
-- table only samples skips). reason: checked, no_edge, in_flight, below_fee,
-- below_buffer, too_late, window_cap, no_cash, no_depth, signal, filled, fill_skipped.
CREATE TABLE IF NOT EXISTS opportunity_counts (
    slug TEXT, side TEXT, reason TEXT, n INTEGER,
    PRIMARY KEY (slug, side, reason)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, ts REAL, level TEXT, kind TEXT, slug TEXT, detail TEXT
);
"""


TRADE_COLS = (
    "id", "signal_id", "slug", "series", "side", "token", "ts_signal", "ts_fill", "latency_ms",
    "shares", "shares_held", "avg_price", "signal_ask", "signal_vwap", "cost", "fee", "fee_in",
    "fair_signal", "fair_fill", "edge_entry", "levels", "status", "window_end", "outcome", "payout",
    "pnl", "pnl_zero_fee", "settled_ts", "wallet",
)
SIGNAL_COLS = (
    "id", "ts", "slug", "series", "side", "token", "tau", "spot", "s0", "sigma_annual", "fair",
    "best_ask", "best_ask_size", "vwap", "fee_ps", "edge_gross", "edge_after_fee", "slippage",
    "net_edge", "threshold", "shares", "usd", "decision", "reason", "trade_id", "wallet",
)


class Database:
    def __init__(self, path: str, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only
        if read_only:
            self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
            return
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None)  # autocommit
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        # Databases created by earlier versions: add columns that didn't exist yet.
        self._ensure_columns("markets", {"ptb_polymarket": "REAL", "ptb_seen_at": "REAL"})
        # Which paper wallet a row belongs to: NULL/'main' = the main wallet,
        # 'whatif-<N>ms' = a latency what-if wallet.
        self._ensure_columns("trades", {"wallet": "TEXT"})
        self._ensure_columns("signals", {"wallet": "TEXT"})
        self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '2')")

    def _ensure_columns(self, table: str, cols: dict[str, str]) -> None:
        have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols.items():
            if name not in have:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal"):
            try:
                total += Path(self.path + suffix).stat().st_size
            except OSError:
                pass
        return total

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

    def set_price_to_beat(self, slug: str, ptb: float, seen_at: float | None) -> None:
        self.conn.execute("UPDATE markets SET ptb_polymarket=?, ptb_seen_at=? WHERE slug=?", (ptb, seen_at, slug))

    def s0_check(self, tolerance: float = 0.5) -> dict[str, Any]:
        """Our Chainlink start price vs Polymarket's published price to beat."""
        rows = self.conn.execute(
            "SELECT s0_chainlink - ptb_polymarket FROM markets WHERE s0_chainlink IS NOT NULL AND ptb_polymarket IS NOT NULL"
        ).fetchall()
        diffs = sorted(abs(r[0]) for r in rows)
        with_ptb = self.conn.execute("SELECT COUNT(*) FROM markets WHERE ptb_polymarket IS NOT NULL").fetchone()[0]
        return {
            "compared": len(diffs),
            "matched": sum(1 for d in diffs if d <= tolerance),
            "median_abs_diff": diffs[len(diffs) // 2] if diffs else None,
            "max_abs_diff": diffs[-1] if diffs else None,
            "windows_with_ptb": with_ptb,
        }

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

    # --- paper trading -----------------------------------------------------------
    def insert_signal(self, row: dict[str, Any]) -> int:
        cols = [c for c in SIGNAL_COLS if c != "id" and c in row]
        cur = self.conn.execute(
            f"INSERT INTO signals({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
            tuple(row[c] for c in cols),
        )
        return int(cur.lastrowid)

    def update_signal(self, signal_id: int, **fields: Any) -> None:
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE signals SET {sets} WHERE id=?", (*fields.values(), signal_id))

    def insert_trade(self, row: dict[str, Any]) -> int:
        cols = [c for c in TRADE_COLS if c != "id" and c in row]
        cur = self.conn.execute(
            f"INSERT INTO trades({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
            tuple(row[c] for c in cols),
        )
        return int(cur.lastrowid)

    def update_trade(self, trade_id: int, **fields: Any) -> None:
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE trades SET {sets} WHERE id=?", (*fields.values(), trade_id))

    def _cols(self, table: str) -> set[str]:
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def all_trades(self, wallet: str = "main") -> list[dict[str, Any]]:
        if "wallet" not in self._cols("trades"):  # read-only view of a database from before v0.4
            cols = [c for c in TRADE_COLS if c != "wallet"]
            rows = [dict(zip(cols, r)) for r in self.conn.execute(f"SELECT {','.join(cols)} FROM trades ORDER BY id")]
            return [dict(r, wallet="main") for r in rows] if wallet == "main" else []
        cur = self.conn.execute(
            f"SELECT {','.join(TRADE_COLS)} FROM trades WHERE COALESCE(wallet, 'main') = ? ORDER BY id", (wallet,)
        )
        rows = [dict(zip(TRADE_COLS, r)) for r in cur.fetchall()]
        for r in rows:
            r["wallet"] = r["wallet"] or "main"
        return rows

    def wallets(self) -> list[str]:
        """The current run's wallets: "main" and the what-if wallets."""
        if "wallet" not in self._cols("trades"):
            return ["main"]
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT COALESCE(wallet, 'main') AS w FROM trades "
            "WHERE wallet IS NULL OR wallet = 'main' OR wallet LIKE 'whatif-%' ORDER BY 1")]

    def meta_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def meta_set(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))

    def start_run(self, run: int, bankroll: float, now: float) -> dict[str, Any] | None:
        """Make `run` the current run. If the database holds trades from a
        different run, keep them under that run's name ("run<N>", what-if
        wallets as "run<N>/whatif-...") so the wallets start empty. Returns a
        summary of the archived run, or None if nothing changed."""
        cur = self.meta_get("current_run")
        old = int(cur) if cur is not None else 1  # databases from before runs existed hold run 1
        if old == run:
            if cur is None:
                self.meta_set("current_run", run)
                self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (f"run{run}:bankroll", str(bankroll)))
                self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (f"run{run}:started", repr(now)))
            return None
        n, net, zero = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl), 0), COALESCE(SUM(pnl_zero_fee), 0) FROM trades "
            "WHERE COALESCE(wallet, 'main') = 'main'"
        ).fetchone()
        n_whatif = self.conn.execute("SELECT COUNT(*) FROM trades WHERE wallet LIKE 'whatif-%'").fetchone()[0]
        if n == 0 and n_whatif == 0:  # nothing to keep (e.g. a new install): just record the run
            self.meta_set("current_run", run)
            self.meta_set(f"run{run}:bankroll", bankroll)
            self.meta_set(f"run{run}:started", repr(now))
            return None
        name = f"run{old}"
        self.conn.execute("BEGIN")
        try:
            self.conn.execute("UPDATE trades SET wallet = ? WHERE COALESCE(wallet, 'main') = 'main'", (name,))
            self.conn.execute("UPDATE trades SET wallet = ? || '/' || wallet WHERE wallet LIKE 'whatif-%'", (name,))
            self.conn.execute("DELETE FROM meta WHERE key LIKE 'wallet_since:%'")
            self.meta_set(f"{name}:ended", repr(now))
            self.meta_set("current_run", run)
            self.meta_set(f"run{run}:bankroll", bankroll)
            self.meta_set(f"run{run}:started", repr(now))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"archived": name, "trades": n, "net_pnl": net, "zero_fee_pnl": zero,
                "bankroll": self.meta_get(f"{name}:bankroll")}

    def past_runs(self) -> list[str]:
        if "wallet" not in self._cols("trades"):
            return []
        names = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT wallet FROM trades WHERE wallet LIKE 'run%' AND wallet NOT LIKE '%/%'")]
        return sorted(names, key=lambda n: int("".join(c for c in n if c.isdigit()) or 0))

    def wallet_since(self, wallet: str, now: float | None = None) -> float | None:
        """When a what-if wallet first ran (recorded once, on first start)."""
        key = f"wallet_since:{wallet}"
        if now is not None and not self.read_only:
            self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (key, repr(now)))
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return float(row[0]) if row else None

    def signals_between(self, t0: float, t1: float) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            f"SELECT {','.join(SIGNAL_COLS)} FROM signals WHERE ts >= ? AND ts < ? "
            "AND COALESCE(wallet, 'main') = 'main' ORDER BY ts", (t0, t1)
        )
        return [dict(zip(SIGNAL_COLS, r)) for r in cur.fetchall()]

    def add_opportunity_counts(self, items: Iterable[tuple[tuple[str, str, str], int]]) -> None:
        self.conn.executemany(
            "INSERT INTO opportunity_counts(slug, side, reason, n) VALUES (?,?,?,?) "
            "ON CONFLICT(slug, side, reason) DO UPDATE SET n = n + excluded.n",
            [(slug, side, reason, n) for (slug, side, reason), n in items],
        )

    def insert_exec_log(self, ts: float, tag: str, msg: str, ref: str | None) -> None:
        self.conn.execute("INSERT INTO exec_log(ts, tag, msg, ref) VALUES (?,?,?,?)", (ts, tag, msg, ref))

    def exec_log_between(self, t0: float, t1: float, limit: int | None = None) -> list[tuple[float, str, str, str | None]]:
        q = "SELECT ts, tag, msg, ref FROM exec_log WHERE ts >= ? AND ts < ? ORDER BY ts"
        rows = self.conn.execute(q, (t0, t1)).fetchall()
        return rows[-limit:] if limit else rows

    # --- recorder -------------------------------------------------------------------
    def insert_snapshot_1s(self, row: tuple) -> None:
        self.conn.execute(
            """INSERT INTO snapshots_1s(ts, series, slug, tau, spot, chainlink, basis, sigma_annual, s0,
                   fair_up, up_bid, up_ask, down_bid, down_ask, blob) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            row,
        )

    def upsert_candle(self, ts: float, o: float, h: float, l: float, c: float, ticks: int, source: str) -> None:
        self.conn.execute(
            """INSERT INTO candles_1m(ts, open, high, low, close, ticks, source) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(ts) DO UPDATE SET open=excluded.open, high=excluded.high, low=excluded.low,
                   close=excluded.close, ticks=excluded.ticks, source=excluded.source
               WHERE candles_1m.source != 'ticks' OR excluded.source = 'ticks'""",
            (ts, o, h, l, c, ticks, source),
        )

    def candles_between(self, t0: float, t1: float) -> list[tuple]:
        return self.conn.execute(
            "SELECT ts, open, high, low, close, ticks, source FROM candles_1m WHERE ts >= ? AND ts < ? ORDER BY ts",
            (t0, t1),
        ).fetchall()

    def oracle_check(self) -> tuple[int, int]:
        """(agreements, comparisons) between our Chainlink-based predicted
        outcome and Polymarket's posted resolution."""
        row = self.conn.execute(
            """SELECT SUM(chainlink_predicted = resolved_outcome), COUNT(*) FROM markets
               WHERE chainlink_predicted IS NOT NULL AND resolved_outcome IS NOT NULL"""
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0)
