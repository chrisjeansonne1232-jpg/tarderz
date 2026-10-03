"""Replay a paper strategy on the bot's own recordings.

The bot stores the top of both order books every second (snapshots_1s) and
each market's official result (markets.resolved_outcome). This replays the
"near-certain side" strategy over those recordings with the same honest fill
rules as live paper trading:

- signal at second t: in the last part of a window, the best ask of one side
  is inside [lo, hi];
- the order "arrives" at the next recorded second and fills against THAT book,
  never above the signal price, never more than the displayed size (minus what
  our own earlier fills already took), never more than the per-trade and
  per-window limits;
- taker fee per the market's own fee parameters; payout $1 per share if the
  side won.

To avoid fooling ourselves with many settings, the settings are chosen on the
earlier 60% of the recorded markets and scored only on the later 40%.
Read-only: the database is opened read-only; nothing here can place an order.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics as st
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from .engine import floor2, walk_asks
from .fees import FeeModel


@dataclass(frozen=True)
class FavParams:
    lo: float          # buy when the best ask is >= lo ...
    hi: float          # ... and <= hi
    last_frac: float   # only in the final `last_frac` of the window

    def label(self) -> str:
        return f"{self.lo * 100:.0f}-{self.hi * 100:.0f}c, last {self.last_frac * 100:.0f}%"


GRID = [FavParams(lo, hi, f) for lo, hi in [(0.90, 0.94), (0.95, 0.97), (0.97, 0.99), (0.95, 0.99)]
        for f in (0.33, 0.15, 0.05)]


@dataclass
class Window:
    slug: str
    series: str
    start: float
    end: float
    outcome: str
    fee: FeeModel
    min_order: float
    rows: list = field(default_factory=list)  # (ts, tau, up_ask, down_ask, blob)
    _ladders: dict = field(default_factory=dict)

    def ladder(self, i: int) -> dict:
        if i not in self._ladders:
            self._ladders[i] = json.loads(zlib.decompress(self.rows[i][4]))
        return self._ladders[i]


def _windows(db_path: str, max_late_s: float) -> Iterator[Window]:
    """Resolved windows with their recorded seconds near the end, streamed in time order."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    try:
        meta = {}
        for slug, series, st_, et, out, rate, exp, mos in conn.execute(
            "SELECT slug, series, start_ts, end_ts, resolved_outcome, fee_rate, fee_exponent, min_order_size "
            "FROM markets WHERE resolved_outcome IS NOT NULL AND end_ts IS NOT NULL"
        ):
            if out not in ("Up", "Down"):
                continue
            meta[slug] = Window(slug, series, st_, et, out, FeeModel(rate or 0.0, exp or 1.0, "recorded"), mos or 0.0)
        if not meta:
            return
        first = min(w.start for w in meta.values())
        open_: dict[str, Window] = {}
        cur = conn.execute(
            "SELECT ts, slug, tau, up_ask, down_ask, blob FROM snapshots_1s WHERE ts >= ? AND tau IS NOT NULL "
            "AND tau <= ? ORDER BY ts", (first, max_late_s),
        )
        for ts, slug, tau, ua, da, blob in cur:
            w = meta.get(slug)
            if w is None or blob is None:
                continue
            open_.setdefault(slug, w).rows.append((ts, tau, ua, da, blob))
            for s in [s for s, ow in open_.items() if ts > ow.end + 5]:
                yield open_.pop(s)
        yield from open_.values()
    finally:
        conn.close()


def simulate(w: Window, p: FavParams, trade_usd: float, window_usd: float, min_secs: float,
             liquidity_memory_s: float) -> list[dict[str, Any]]:
    """Trades the strategy would have made in one window."""
    trades: list[dict[str, Any]] = []
    spent = 0.0
    taken: dict[tuple[str, float], list[tuple[float, float]]] = {}  # (side, price) -> [(ts, shares)]
    interval = w.end - w.start
    for i, (ts, tau, ua, da, _blob) in enumerate(w.rows[:-1]):
        if tau < min_secs or tau > p.last_frac * interval or spent >= window_usd - 1e-9:
            continue
        for side, ask in (("Up", ua), ("Down", da)):
            if ask is None or not (p.lo - 1e-9 <= ask <= p.hi + 1e-9) or spent >= window_usd - 1e-9:
                continue
            nts = w.rows[i + 1][0]
            if nts - ts > 3.0 or nts >= w.end:      # no book recorded right after the signal
                continue
            asks = [(a, s) for a, s in w.ladder(i + 1).get("ua" if side == "Up" else "da", [])]
            hidden: dict[float, float] = {}
            for (sd, price), hist in taken.items():
                if sd == side:
                    hidden[price] = sum(sh for t0, sh in hist if nts - t0 < liquidity_memory_s)
            budget = min(trade_usd, window_usd - spent)
            fills = walk_asks(asks, max_shares=floor2(budget / ask), limit_price=ask, budget=budget,
                              fee=w.fee, fee_in_collateral=True, hidden=hidden)
            shares = sum(s for _, s, _ in fills)
            if shares < max(w.min_order, 0.01):
                continue
            cost = sum(px * s for px, s, _ in fills)
            fee = sum(f for _, _, f in fills)
            spent += cost + fee
            for px, s, _ in fills:
                taken.setdefault((side, px), []).append((nts, s))
            won = side == w.outcome
            trades.append({"slug": w.slug, "series": w.series, "start": w.start, "ts": nts, "side": side,
                           "price": cost / shares, "shares": shares, "cost": cost, "fee": fee, "won": won,
                           "pnl": (shares if won else 0.0) - cost - fee, "pnl0": (shares if won else 0.0) - cost})
    return trades


def _stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    per: dict[str, float] = {}
    for t in trades:
        per[t["slug"]] = per.get(t["slug"], 0.0) + t["pnl"]
    v = list(per.values())
    m = st.mean(v) if v else None
    half = 1.96 * st.stdev(v) / math.sqrt(len(v)) if len(v) >= 2 else None
    return {"trades": len(trades), "windows": len(v), "lost": sum(1 for t in trades if not t["won"]),
            "lost_windows": sum(1 for x in v if x < 0), "net": sum(t["pnl"] for t in trades),
            "zero": sum(t["pnl0"] for t in trades), "fees": sum(t["fee"] for t in trades),
            "spent": sum(t["cost"] + t["fee"] for t in trades), "mean": m, "half": half}


def _fmt(s: dict[str, Any]) -> str:
    if not s["trades"]:
        return "no trades"
    rng = f"{s['mean']:+.2f} ± {s['half']:.2f}" if s["half"] is not None else f"{s['mean']:+.2f}"
    roi = 100 * s["net"] / s["spent"] if s["spent"] else 0.0
    return (f"{s['trades']:>5} trades in {s['windows']:>4} mkts, {s['lost_windows']:>3} mkts lost · net ${s['net']:>+8.2f} "
            f"({roi:+.2f}% of spent) · zero-fee ${s['zero']:>+8.2f} · per market ${rng}")


def run_favorite_backtest(db_path: str, trade_usd: float, window_usd: float, min_secs: float,
                          liquidity_memory_s: float, tz: str) -> str:
    max_late = max(p.last_frac for p in GRID) * 900 + 5
    results: dict[FavParams, list[dict[str, Any]]] = {p: [] for p in GRID}
    starts: list[float] = []
    n_windows = 0
    for w in _windows(db_path, max_late):
        n_windows += 1
        starts.append(w.start)
        for p in GRID:
            results[p].extend(simulate(w, p, trade_usd, window_usd, min_secs, liquidity_memory_s))
    lines = ["NEAR-CERTAIN SIDE: replay on this bot's own recorded order books (paper; nothing was traded)", ""]
    if n_windows < 20:
        lines.append(f"Only {n_windows} resolved markets with recorded order books so far: not enough to judge. "
                     "Let the bot run longer and try again.")
        return "\n".join(lines)
    starts.sort()
    cut = starts[int(len(starts) * 0.6)]
    zone = ZoneInfo(tz)
    day = lambda t: datetime.fromtimestamp(t, zone).strftime("%m-%d %H:%M")
    lines += [
        f"Recorded: {n_windows} resolved markets, {day(starts[0])} → {day(starts[-1])} ({tz}).",
        f"Rules: ${trade_usd:g} per trade, ${window_usd:g} per market, fill at the next recorded second against the book",
        "as it was then, never above the signal price, fees at each market's own rate.",
        f"Settings are chosen on the markets before {day(cut)} and scored only on the markets after it.",
        "",
        "ALL SETTINGS ON THE EARLIER 60% (where the choice is made):",
    ]
    train = {p: [t for t in tr if t["start"] < cut] for p, tr in results.items()}
    test = {p: [t for t in tr if t["start"] >= cut] for p, tr in results.items()}
    for p in GRID:
        lines.append(f"  {p.label():<22} {_fmt(_stats(train[p]))}")

    def score(p: FavParams) -> float:
        s = _stats(train[p])
        if s["windows"] < 10 or s["half"] is None:
            return -1e9
        return s["mean"] - s["half"]  # most reliably positive, not just the biggest

    best = max(GRID, key=score)
    s_test = _stats(test[best])
    lines += ["", f"CHOSEN ON THE EARLIER DATA: {best.label()}", f"  later 40% (unseen): {_fmt(s_test)}", ""]
    if not s_test["trades"] or s_test["half"] is None:
        verdict = "Not enough trades in the unseen part yet. Let it run longer."
    elif s_test["mean"] - s_test["half"] > 0:
        verdict = ("PASSES on unseen data: profitable after fees with the per-market range above zero. "
                   "Keep paper-testing it live before believing it.")
    elif s_test["mean"] + s_test["half"] < 0:
        verdict = "FAILS on unseen data: it loses after fees."
    else:
        verdict = "NOT PROVEN: on unseen data the range still includes zero."
    lines.append(f"VERDICT: {verdict}")
    lines += ["", "Every setting on the later 40% (for reference only; picking from this list would be cheating):"]
    for p in GRID:
        lines.append(f"  {p.label():<22} {_fmt(_stats(test[p]))}")
    by_series: dict[str, list] = {}
    for t in results[best]:
        by_series.setdefault(t["series"], []).append(t)
    lines += ["", f"Chosen setting, all recorded markets, by market type:"]
    for name, tr in sorted(by_series.items()):
        lines.append(f"  {name:<8} {_fmt(_stats(tr))}")
    lines += ["", "Limits: 1-second recordings (the live bot reacts faster); top 5 price levels only;",
              "a loss costs about 30-100 wins at these prices, so a few unlucky markets swing the result."]
    return "\n".join(lines)
