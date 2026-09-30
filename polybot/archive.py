"""Daily archive: every trade, signal, skip and market window of a day as CSV
files (open them in Excel), plus a plain-text summary.

    data/archive/
        README.txt              what each file and column means
        all_trades.csv          every paper trade ever made
        2026-09-30/
            summary.txt         the day in numbers, results by entry price, skip reasons
            trades.csv          every trade entered that day, with its result
            signals.csv         recorded opportunities: all signals, skips sampled
            windows.csv         one row per market window: prices, outcome, counts
            log.csv             the execution log

Everything is rebuilt from the SQLite database, which stays the source of
truth; deleting the archive loses nothing. Reads use their own read-only
connection so this can run in a worker thread while the bot keeps writing.
"""

from __future__ import annotations

import csv
import math
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Outcome of each evaluation (engine counters), in the order they are checked.
REASONS: list[tuple[str, str, str]] = [
    ("checked", "checks", "times the bot compared fair value with the best ask"),
    ("no_edge", "ask >= fair", "ask was not below fair value: nothing to do"),
    ("in_flight", "order in flight", "an order for this side was already on its way"),
    ("below_fee", "edge < fee", "ask below fair value, but by less than the taker fee"),
    ("below_buffer", "edge <= buffer", "edge after fee and slippage not above the safety buffer"),
    ("too_late", "too late", "inside the no-new-entries period at the end of the window"),
    ("window_cap", "window cap", "max spend per window already reached"),
    ("no_cash", "no cash", "bankroll fully committed to open trades"),
    ("no_depth", "no depth", "not enough displayed size at an acceptable price"),
    ("signal", "signals", "all checks passed: a paper order was sent"),
    ("filled", "filled", "the paper order filled after the simulated latency"),
    ("fill_skipped", "missed at fill", "the book changed during the latency and the order did not fill"),
]

PRICE_BUCKETS = [(0.0, 0.05), (0.05, 0.20), (0.20, 0.50), (0.50, 0.80), (0.80, 0.95), (0.95, 1.0001)]

README = """POLYBOT ARCHIVE (paper trading only; no real orders were ever placed)

One folder per day (days in {tz}). The bot refreshes today's and yesterday's
folders every {interval:g} minutes while it runs, and fills in missing days when it
starts. Open the .csv files in Excel or Google Sheets. Everything here is rebuilt
from data/paperbot.sqlite, so the archive can be deleted safely.
To refresh it by hand:  python -m polybot archive

all_trades.csv   every paper trade ever made by the main wallet (same columns as trades.csv)
whatif_trades.csv  trades of the latency what-if wallets ("whatif-0ms" etc.): the same
                 rules as the main wallet, orders filled after a different delay, each
                 with its own bankroll. Compare them with `python -m polybot report`.

<day>/summary.txt
    The day in numbers. "BY ENTRY PRICE" compares how often trades at each
    price actually won with how often the model said they would ("model said").
    If cheap contracts win much less often than the model said, the model
    overrates long shots. "WHY OPPORTUNITIES WERE SKIPPED" counts every check,
    not a sample.

<day>/trades.csv   one row per paper trade entered that day
    signal_time / fill_time   when the signal fired / when it filled (after the latency)
    seconds_left              time left in the market window at the signal
    shares, avg_price         shares bought and average price paid per share
    signal_ask                best ask when the signal fired
    cost_usd, fee_usd         shares x price, and the taker fee
    total_paid_usd            what left the bankroll (cost + fee)
    fair_at_signal/fill       model probability this side wins, at signal and at fill
    edge_at_entry             fair value - price - fee per share (what we expected to make per share)
    status                    OPEN (window running), PENDING (waiting for Polymarket), WON, LOST
    outcome                   Up or Down, as resolved by Polymarket
    payout_usd                $1 per share if won, else 0
    pnl_usd                   payout - total paid
    pnl_zero_fee_usd          the same trade with no fee
    book_levels               [price, shares, fee] for each order-book level filled

<day>/signals.csv   opportunities the bot recorded
    Every signal is here. Skips are sampled (at most one per market side every
    few seconds); windows.csv has the full counts.
    seconds_left, btc_price, start_price, vol_annual   model inputs
    fair_value          model probability that this side wins
    best_ask, ask_size  cheapest offer and its size
    vwap                average price walking the book for our order size
    fee_per_share       taker fee per share at that price
    edge_before_fee     fair_value - best_ask
    edge_after_fee      edge_before_fee - fee_per_share
    net_edge            fair_value - vwap - fee_per_share - slippage allowance
    buffer              net_edge must be above this to trade
    decision            filled, skipped, or pending (still in flight)
    reason              why it was skipped

<day>/windows.csv   one row per market window that started that day
    start_price / end_price     Chainlink price used for the window's start / end
    start_offset_s / end_offset_s  seconds between that report and the boundary
    polymarket_price_to_beat    Polymarket's own published start price, when available
    our_outcome / polymarket_outcome / agree   outcome our prices imply vs the official one
    then one column per evaluation outcome (see summary.txt), and the window's trades and P&L

<day>/log.csv   the execution log shown on the dashboard
"""


@dataclass
class ArchiveResult:
    days: list[str] = field(default_factory=list)
    locked: list[str] = field(default_factory=list)  # files open in another program (e.g. Excel)


def _fmt_time(ts: float | None, tz: ZoneInfo) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M:%S")


def _r(x: Any, nd: int = 4) -> Any:
    if x is None:
        return ""
    if isinstance(x, float):
        if not math.isfinite(x):
            return ""
        return round(x, nd)
    return x


def day_bounds(day: date, tz: ZoneInfo) -> tuple[float, float]:
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    nxt = day + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
    return start.timestamp(), end.timestamp()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _wallet_sql(conn: sqlite3.Connection, table: str, main: bool) -> str:
    """SQL condition selecting the main wallet's rows (or the what-if wallets')."""
    has = any(r[1] == "wallet" for r in conn.execute(f"PRAGMA table_info({table})"))
    if not has:  # database from before v0.4: everything is the main wallet
        return "1=1" if main else "0=1"
    return "COALESCE(wallet, 'main') = 'main'" if main else "COALESCE(wallet, 'main') != 'main'"


def _write_csv(path: Path, header: list[str], rows: list[list[Any]], result: ArchiveResult) -> None:
    """Write via a temp file so a half-written file never replaces a good one.
    utf-8-sig so Excel reads the symbols (¢, τ, ≤) correctly."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    _replace(tmp, path, result)


def _write_text(path: Path, text: str, result: ArchiveResult) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8-sig")
    _replace(tmp, path, result)


def _replace(tmp: Path, path: Path, result: ArchiveResult) -> None:
    try:
        os.replace(tmp, path)
    except PermissionError:  # Windows: the file is open in Excel; keep the old one
        result.locked.append(str(path))
        try:
            tmp.unlink()
        except OSError:
            pass


TRADE_HEADER = [
    "trade_id", "signal_time", "fill_time", "market", "series", "side", "window_end", "seconds_left",
    "shares", "avg_price", "signal_ask", "cost_usd", "fee_usd", "total_paid_usd", "fair_at_signal",
    "fair_at_fill", "edge_at_entry", "latency_ms", "status", "outcome", "payout_usd", "pnl_usd",
    "pnl_zero_fee_usd", "settled_time", "book_levels",
]


def _trade_rows(rows: list[sqlite3.Row], tz: ZoneInfo) -> list[list[Any]]:
    out = []
    for t in rows:
        paid = (t["cost"] or 0.0) + ((t["fee"] or 0.0) if t["fee_in"] == "collateral" else 0.0)
        out.append([
            t["id"], _fmt_time(t["ts_signal"], tz), _fmt_time(t["ts_fill"], tz), t["slug"], t["series"], t["side"],
            _fmt_time(t["window_end"], tz), _r((t["window_end"] or 0) - (t["ts_signal"] or 0), 1),
            _r(t["shares"], 2), _r(t["avg_price"]), _r(t["signal_ask"]), _r(t["cost"]), _r(t["fee"], 5), _r(paid),
            _r(t["fair_signal"]), _r(t["fair_fill"]), _r(t["edge_entry"]), _r(t["latency_ms"], 0), t["status"],
            t["outcome"] or "", _r(t["payout"]), _r(t["pnl"]), _r(t["pnl_zero_fee"]), _fmt_time(t["settled_ts"], tz),
            t["levels"] or "",
        ])
    return out


def _counts_by_slug(conn: sqlite3.Connection, slugs: list[str]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {s: {} for s in slugs}
    if not slugs or not _has_table(conn, "opportunity_counts"):
        return out
    q = f"SELECT slug, reason, SUM(n) FROM opportunity_counts WHERE slug IN ({','.join('?' for _ in slugs)}) GROUP BY slug, reason"
    for slug, reason, n in conn.execute(q, slugs):
        out[slug][reason] = int(n or 0)
    return out


def _money(x: float) -> str:
    return f"-${-x:,.2f}" if x < 0 else f"+${x:,.2f}"


def _summary(day: date, tzname: str, now: float, tz: ZoneInfo, trades: list[sqlite3.Row],
             windows: list[sqlite3.Row], counts: dict[str, dict[str, int]], n_signals: int,
             whatif: dict[str, list[sqlite3.Row]] | None = None) -> str:
    settled = [t for t in trades if t["status"] in ("WON", "LOST")]
    won = [t for t in settled if t["status"] == "WON"]
    net = sum(t["pnl"] or 0.0 for t in settled)
    zero = sum(t["pnl_zero_fee"] or 0.0 for t in settled)
    fees = sum(t["fee"] or 0.0 for t in trades)
    lines = [
        f"POLYBOT PAPER TRADING - {day.isoformat()} ({tzname})   simulated fills only; no real orders",
        f"archived {_fmt_time(now, tz)}",
        "",
        "TRADES ENTERED THIS DAY",
        f"  trades            {len(trades)}   (settled {len(settled)}: won {len(won)}, lost {len(settled) - len(won)};"
        f" open/pending {len(trades) - len(settled)})",
    ]
    if settled:
        lines += [
            f"  win rate          {100 * len(won) / len(settled):.1f}%",
            f"  net P&L           {_money(net)}   (after fees, settled trades)",
            f"  zero-fee P&L      {_money(zero)}   (the same trades with no fees)",
        ]
    lines.append(f"  fees paid         ${fees:,.2f}   (all trades entered this day)")
    lines += [
        "",
        "BY ENTRY PRICE  (settled trades; 'model said' = average fair value at the signal)",
        f"  {'price':<10} {'trades':>6} {'won':>5} {'won %':>7} {'model said':>11} {'net P&L':>11} {'zero-fee':>11}",
    ]
    for lo, hi in PRICE_BUCKETS:
        b = [t for t in settled if lo <= (t["avg_price"] or 0.0) < hi]
        label = f"{lo * 100:.0f}-{min(hi, 1.0) * 100:.0f}c"
        if not b:
            lines.append(f"  {label:<10} {0:>6}")
            continue
        w = sum(1 for t in b if t["status"] == "WON")
        model = sum((t["fair_signal"] or 0.0) for t in b) / len(b)
        lines.append(
            f"  {label:<10} {len(b):>6} {w:>5} {100 * w / len(b):>6.1f}% {100 * model:>10.1f}% "
            f"{_money(sum(t['pnl'] or 0.0 for t in b)):>11} {_money(sum(t['pnl_zero_fee'] or 0.0 for t in b)):>11}"
        )
    lines += [
        "  A price range where 'won %' is well below 'model said' is where the model is too optimistic.",
        "  With few trades per row the gap is mostly noise; trades in the same window move together.",
        "",
        "WHY OPPORTUNITIES WERE SKIPPED  (every check, not a sample; windows that started this day; counted since v0.3.0)",
    ]
    total: dict[str, int] = {}
    for c in counts.values():
        for k, v in c.items():
            total[k] = total.get(k, 0) + v
    if total:
        for key, label, desc in REASONS:
            lines.append(f"  {label:<16} {total.get(key, 0):>10,}   {desc}")
    else:
        lines.append("  no counts recorded (counting started in v0.3.0)")
    lines.append(f"  {n_signals:,} opportunities are listed in signals.csv (skips there are sampled)")
    agree = [w for w in windows if w["chainlink_predicted"] and w["resolved_outcome"]]
    ok = sum(1 for w in agree if w["chainlink_predicted"] == w["resolved_outcome"])
    traded = len({t["slug"] for t in trades})
    lines += [
        "",
        "MARKET WINDOWS",
        f"  windows started   {len(windows)}   (traded in {traded})",
        f"  outcome check     {ok}/{len(agree)} windows: our Chainlink start/end prices gave Polymarket's result",
        f"  start price missed {sum(1 for w in windows if w['s0_status'] == 'missed')}",
        "",
    ]
    if whatif:
        lines += [
            "LATENCY WHAT-IF  (same rules as the main wallet, orders filled after each delay; trades entered this day)",
            f"  {'delay':<10} {'trades':>6} {'won':>9} {'net P&L':>11} {'zero-fee':>11}",
        ]
        for name in sorted(whatif, key=lambda n: int("".join(c for c in n if c.isdigit()) or 0)):
            ts_ = whatif[name]
            st = [t for t in ts_ if t["status"] in ("WON", "LOST")]
            won = sum(1 for t in st if t["status"] == "WON")
            lines.append(f"  {name.removeprefix('whatif-'):<10} {len(ts_):>6} {f'{won}/{len(st)}':>9} "
                         f"{_money(sum(t['pnl'] or 0.0 for t in st)):>11} {_money(sum(t['pnl_zero_fee'] or 0.0 for t in st)):>11}")
        lines += ["  Details: whatif_trades.csv in the archive folder; the full comparison is in `python -m polybot report`.", ""]
    return "\n".join(lines)


def export_day(db_path: str, day: date, tzname: str, root: Path, now: float, result: ArchiveResult | None = None) -> ArchiveResult:
    result = result or ArchiveResult()
    tz = ZoneInfo(tzname)
    t0, t1 = day_bounds(day, tz)
    folder = root / day.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        tmain, smain = _wallet_sql(conn, "trades", True), _wallet_sql(conn, "signals", True)
        trades = conn.execute(f"SELECT * FROM trades WHERE ts_signal >= ? AND ts_signal < ? AND {tmain} ORDER BY id",
                              (t0, t1)).fetchall()
        signals = conn.execute(f"SELECT * FROM signals WHERE ts >= ? AND ts < ? AND {smain} ORDER BY ts", (t0, t1)).fetchall()
        whatif: dict[str, list[sqlite3.Row]] = {}
        for t in conn.execute(f"SELECT * FROM trades WHERE ts_signal >= ? AND ts_signal < ? AND "
                              f"{_wallet_sql(conn, 'trades', False)} ORDER BY id", (t0, t1)):
            whatif.setdefault(t["wallet"], []).append(t)
        windows = conn.execute("SELECT * FROM markets WHERE start_ts >= ? AND start_ts < ? ORDER BY start_ts, series", (t0, t1)).fetchall()
        log = conn.execute("SELECT ts, tag, msg, ref FROM exec_log WHERE ts >= ? AND ts < ? ORDER BY ts, id", (t0, t1)).fetchall()
        slugs = [w["slug"] for w in windows]
        counts = _counts_by_slug(conn, slugs)
        by_slug: dict[str, list[sqlite3.Row]] = {}
        if slugs:
            q = f"SELECT * FROM trades WHERE slug IN ({','.join('?' for _ in slugs)}) AND {tmain}"
            for t in conn.execute(q, slugs):
                by_slug.setdefault(t["slug"], []).append(t)
    finally:
        conn.close()

    _write_csv(folder / "trades.csv", TRADE_HEADER, _trade_rows(trades, tz), result)
    _write_csv(folder / "signals.csv", [
        "signal_id", "time", "market", "series", "side", "seconds_left", "btc_price", "start_price", "vol_annual",
        "fair_value", "best_ask", "ask_size", "vwap", "fee_per_share", "edge_before_fee", "edge_after_fee",
        "slippage", "net_edge", "buffer", "shares", "usd", "decision", "reason", "trade_id",
    ], [[
        s["id"], _fmt_time(s["ts"], tz), s["slug"], s["series"], s["side"], _r(s["tau"], 1), _r(s["spot"], 2),
        _r(s["s0"], 2), _r(s["sigma_annual"]), _r(s["fair"]), _r(s["best_ask"]), _r(s["best_ask_size"], 2),
        _r(s["vwap"]), _r(s["fee_ps"], 5), _r(s["edge_gross"]), _r(s["edge_after_fee"]), _r(s["slippage"]),
        _r(s["net_edge"]), _r(s["threshold"]), _r(s["shares"], 2), _r(s["usd"]), s["decision"] or "",
        s["reason"] or "", s["trade_id"] if s["trade_id"] is not None else "",
    ] for s in signals], result)

    win_rows = []
    for w in windows:
        c = counts.get(w["slug"], {})
        ts_ = by_slug.get(w["slug"], [])
        settled = [t for t in ts_ if t["status"] in ("WON", "LOST")]
        pred, res = w["chainlink_predicted"], w["resolved_outcome"]
        win_rows.append([
            _fmt_time(w["start_ts"], tz), _fmt_time(w["end_ts"], tz), w["series"], w["slug"],
            _r(w["s0_chainlink"], 2), _r(w["s0_chainlink_ts"] - w["start_ts"], 3) if w["s0_chainlink_ts"] else "",
            w["s0_status"] or "", _r(w["ptb_polymarket"], 2),
            _r(w["end_chainlink"], 2), _r(w["end_chainlink_ts"] - w["end_ts"], 3) if w["end_chainlink_ts"] else "",
            w["end_status"] or "", pred or "", res or "", ("yes" if pred == res else "NO") if pred and res else "",
            *[c.get(k, 0) for k, _, _ in REASONS],
            len(ts_), len(ts_) - len(settled),
            _r(sum((t["cost"] or 0.0) + ((t["fee"] or 0.0) if t["fee_in"] == "collateral" else 0.0) for t in ts_)),
            _r(sum(t["fee"] or 0.0 for t in ts_), 5),
            _r(sum(t["pnl"] or 0.0 for t in settled)) if settled else "",
            _r(sum(t["pnl_zero_fee"] or 0.0 for t in settled)) if settled else "",
        ])
    _write_csv(folder / "windows.csv", [
        "window_start", "window_end", "series", "market", "start_price", "start_offset_s", "start_status",
        "polymarket_price_to_beat", "end_price", "end_offset_s", "end_status", "our_outcome", "polymarket_outcome",
        "agree", *[k for k, _, _ in REASONS], "trades", "trades_open", "paid_usd", "fees_usd", "pnl_usd",
        "pnl_zero_fee_usd",
    ], win_rows, result)
    _write_csv(folder / "log.csv", ["time", "tag", "message", "ref"],
               [[_fmt_time(r["ts"], tz), r["tag"], r["msg"], r["ref"] or ""] for r in log], result)
    _write_text(folder / "summary.txt", _summary(day, tzname, now, tz, trades, windows, counts, len(signals), whatif), result)
    result.days.append(day.isoformat())
    return result


def first_day(db_path: str, tz: ZoneInfo) -> date | None:
    conn = _connect(db_path)
    try:
        firsts = [conn.execute(q).fetchone()[0] for q in (
            "SELECT MIN(ts_signal) FROM trades", "SELECT MIN(ts) FROM signals", "SELECT MIN(start_ts) FROM markets",
        )]
    finally:
        conn.close()
    firsts = [f for f in firsts if f is not None]
    return datetime.fromtimestamp(min(firsts), tz).date() if firsts else None


def run_archive(db_path: str, root: str | Path, tzname: str, now: float, interval_min: float = 15.0,
                all_days: bool = False, only: date | None = None) -> ArchiveResult:
    """Refresh today and yesterday, and write any day that has no folder yet
    (or every day with `all_days`, or just `only`)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    tz = ZoneInfo(tzname)
    result = ArchiveResult()
    _write_text(root / "README.txt", README.format(tz=tzname, interval=interval_min), result)
    if not Path(db_path).exists():
        return result
    today = datetime.fromtimestamp(now, tz).date()
    if only is not None:
        days = [only]
    else:
        start = first_day(db_path, tz)
        if start is None:
            return result
        days = []
        d = start
        while d <= today:
            if all_days or d >= today - timedelta(days=1) or not (root / d.isoformat() / "summary.txt").exists():
                days.append(d)
            d += timedelta(days=1)
    for d in days:
        export_day(db_path, d, tzname, root, now, result)
    conn = _connect(db_path)
    try:
        all_trades = conn.execute(f"SELECT * FROM trades WHERE {_wallet_sql(conn, 'trades', True)} ORDER BY id").fetchall()
        whatif = conn.execute(f"SELECT * FROM trades WHERE {_wallet_sql(conn, 'trades', False)} ORDER BY wallet, id").fetchall()
    finally:
        conn.close()
    _write_csv(root / "all_trades.csv", TRADE_HEADER, _trade_rows(all_trades, tz), result)
    if whatif:
        _write_csv(root / "whatif_trades.csv", ["wallet", *TRADE_HEADER],
                   [[t["wallet"], *row] for t, row in zip(whatif, _trade_rows(whatif, tz))], result)
    return result
