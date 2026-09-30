"""Paper-trading engine: signals, simulated taker fills, settlement.

Nothing in here can place a real order. It reads the local copy of the public
order book, decides what a taker order *would* have done, and records it.

Fill model (honest by construction):
- On each spot update, per live window and side: fair value vs. best ask.
  Signal when  fair - VWAP(size) - fee/share - slippage_allowance > buffer.
- The order "arrives" `latency_ms` later and fills against the book as it is
  then: walk the asks up to limit = signal ask + max_slippage, never more
  than the displayed size (minus size our own earlier paper fills already
  took), never more than the budget. Optionally skip if the ask moved.
- Fees per match: shares * rate * (p(1-p))^exponent, rounded to 5 dp.
- Positions are held to resolution and settled from Polymarket's posted
  outcome. A zero-fee shadow P&L is tracked for the same trades.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from .fairvalue import annualize
from .fees import FeeModel
from .metrics import summarize

if TYPE_CHECKING:
    from .app import App, FairValue
    from .markets import MarketWindow

log = logging.getLogger(__name__)

OPEN_STATES = ("OPEN", "PENDING")


def floor2(x: float) -> float:
    """Polymarket sizes have 2 decimals; never round a size up."""
    return math.floor(x * 100 + 1e-9) / 100


def cents(x: float) -> str:
    v = 100 * x
    return f"{0.0 if abs(v) < 0.05 else v:.1f}¢"  # no "-0.0¢" from float dust


@dataclass
class Trade:
    id: int
    signal_id: int
    slug: str
    series: str
    side: str
    token: str
    ts_signal: float
    ts_fill: float
    latency_ms: float
    shares: float
    shares_held: float
    avg_price: float
    signal_ask: float
    signal_vwap: float
    cost: float
    fee: float
    fee_in: str
    fair_signal: float
    fair_fill: float | None
    edge_entry: float
    levels: list = field(default_factory=list)
    status: str = "OPEN"
    window_end: float = 0.0
    outcome: str | None = None
    payout: float | None = None
    pnl: float | None = None
    pnl_zero_fee: float | None = None
    settled_ts: float | None = None
    wallet: str = "main"

    @property
    def cash_out(self) -> float:
        return self.cost + (self.fee if self.fee_in == "collateral" else 0.0)

    @classmethod
    def from_row(cls, r: dict[str, Any]) -> "Trade":
        r = dict(r)
        r["levels"] = json.loads(r.get("levels") or "[]")
        r["wallet"] = r.get("wallet") or "main"
        return cls(**r)

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["levels"] = json.dumps(self.levels)
        return d

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("token", None)
        return d


def walk_asks(
    asks: list[tuple[float, float]] | tuple,
    *,
    max_shares: float,
    limit_price: float,
    budget: float,
    fee: FeeModel,
    fee_in_collateral: bool,
    hidden: dict[float, float] | None = None,
) -> list[tuple[float, float, float]]:
    """Take liquidity from the ask ladder (best first). Returns [(price, shares, fee)].
    Never exceeds displayed size (minus `hidden`), the limit price, or the budget
    (cost plus fee when fees are paid in collateral)."""
    fills: list[tuple[float, float, float]] = []
    remaining = max_shares
    spent = 0.0
    for price, size in asks:
        if price > limit_price + 1e-9 or remaining < 0.01:
            break
        avail = size - (hidden or {}).get(price, 0.0)
        if avail < 0.01:
            continue
        per_share = price + (fee.fee_per_share(price) if fee_in_collateral else 0.0)
        afford = (budget - spent) / per_share if per_share > 0 else 0.0
        take = floor2(min(avail, remaining, afford))
        if take < 0.01:
            break
        f = fee.match_fee(take, price)
        fills.append((price, take, f))
        spent += take * price + (f if fee_in_collateral else 0.0)
        remaining -= take
    return fills


class _NullCounter(dict):
    """Counter stand-in for what-if wallets: counting is the main wallet's job."""

    def __missing__(self, key: Any) -> int:
        return 0

    def __setitem__(self, key: Any, value: Any) -> None:
        pass


_NULL_COUNTS = _NullCounter()


@dataclass
class _Pending:
    signal_id: int
    slug: str
    side: str
    usd: float


class PaperEngine:
    """One paper wallet. The main wallet uses [sim] and reports to the
    dashboard and execution log. What-if wallets ("whatif-<N>ms") apply the
    same rules with a different order delay and their own bankroll; they are
    quiet (their trades go to the database only) and don't count skips."""

    def __init__(self, app: "App", wallet: str = "main", latency_ms: float | None = None,
                 starting_bankroll: float | None = None) -> None:
        self.app = app
        self.cfg = app.cfg
        self.db = app.db
        self.wallet = wallet
        self.main = wallet == "main"
        self.latency_ms = self.cfg.sim.latency_ms if latency_ms is None else float(latency_ms)
        self._starting = self.cfg.sim.starting_bankroll if starting_bankroll is None else starting_bankroll
        self.since: float | None = None  # what-if wallets: when this wallet first ran
        self.trades: list[Trade] = [Trade.from_row(r) for r in self.db.all_trades(wallet)]
        self.pending: dict[int, _Pending] = {}  # signal id -> reserved order in flight
        self._inflight: set[tuple[str, str]] = set()
        self._last_skip: dict[tuple[str, str], float] = {}
        self._hidden: dict[str, list[tuple[float, float, float]]] = {}  # token -> [(price, shares, ts)]
        self._tasks: set[asyncio.Task] = set()
        self.version = 0
        self._stats_cache: tuple[tuple, dict] | None = None
        self.signals_taken = 0
        self.skips_logged = 0
        # Every evaluation, counted by outcome (not throttled like the skip log);
        # flushed to the opportunity_counts table for the archive.
        self._counts: Counter[tuple[str, str, str]] = Counter()

    # --- accounting -------------------------------------------------------------------
    @property
    def starting(self) -> float:
        return self._starting

    def realized_pnl(self) -> float:
        return sum(t.pnl or 0.0 for t in self.trades if t.status in ("WON", "LOST"))

    def open_trades(self) -> list[Trade]:
        return [t for t in self.trades if t.status in OPEN_STATES]

    def cash(self) -> float:
        reserved = sum(p.usd for p in self.pending.values())
        return self.starting + self.realized_pnl() - sum(t.cash_out for t in self.open_trades()) - reserved

    def window_spent(self, slug: str) -> float:
        spent = sum(t.cash_out for t in self.trades if t.slug == slug)
        return spent + sum(p.usd for p in self.pending.values() if p.slug == slug)

    def stats(self, now: float) -> dict[str, Any]:
        day_key = int((now - 0) // 60)  # recompute at least once a minute (day rollover)
        key = (self.version, day_key)
        if self._stats_cache is None or self._stats_cache[0] != key:
            self._stats_cache = (key, summarize(self.trades, self.starting, self.cfg.dashboard.timezone, now))
        return self._stats_cache[1]

    def _changed(self) -> None:
        self.version += 1

    # --- startup ------------------------------------------------------------------------
    def reconcile_on_start(self) -> None:
        """Settle open trades whose market was already resolved (e.g. while down)."""
        for t in self.open_trades():
            row = self.db.conn.execute("SELECT resolved_outcome FROM markets WHERE slug=?", (t.slug,)).fetchone()
            if row and row[0]:
                self.settle(t.slug, row[0])

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for sid in list(self.pending):
            self.db.update_signal(sid, decision="skipped", reason="shutdown before fill")
        self.pending.clear()
        self.flush_counts()

    def _log(self, tag: str, msg: str, ref: str | None = None) -> None:
        if self.main:
            self.app.exec_log(tag, msg, ref=ref)

    def flush_counts(self) -> None:
        if self._counts:
            self.db.add_opportunity_counts(self._counts.items())
            self._counts.clear()

    # --- signal evaluation ---------------------------------------------------------------
    def evaluate(self, now: float) -> None:
        for tracker in self.app.trackers:
            w = tracker.current(now)
            if w is None or not w.rules_ok or w.fee is None:
                continue
            fv, _ = self.app.fair_value(w, now)
            if fv is None:
                continue
            for side in ("Up", "Down"):
                self._evaluate_side(w, fv, side, now)

    def _evaluate_side(self, w: "MarketWindow", fv: "FairValue", side: str, now: float) -> None:
        token = w.up_token if side == "Up" else w.down_token
        book = self.app.channel.books.get(token)
        if book is None or not book.ready:
            return
        best = book.best_ask()
        if best is None:
            return
        ask, ask_size = best
        fair = fv.p_up if side == "Up" else 1.0 - fv.p_up
        edge_gross = fair - ask
        counts = self._counts if self.main else _NULL_COUNTS
        counts[(w.slug, side, "checked")] += 1
        if edge_gross <= 0:
            counts[(w.slug, side, "no_edge")] += 1
            return  # ask is not below fair value: not an opportunity
        key = (w.slug, side)
        if key in self._inflight:
            counts[(w.slug, side, "in_flight")] += 1
            return  # this opportunity already has an order on its way
        sim, st = self.cfg.sim, self.cfg.strategy
        fee = w.fee
        assert fee is not None
        coll = self.cfg.fees.buy_fee_in == "collateral"
        fee_ask = fee.fee_per_share(ask)
        edge_after_fee = edge_gross - fee_ask
        tau = w.end_ts - now

        budget = min(sim.max_trade_usd, sim.max_window_usd - self.window_spent(w.slug), self.cash())
        per_share = ask + (fee_ask if coll else 0.0)
        target = floor2(budget / per_share) if budget > 0 else 0.0
        fills = walk_asks(
            book.view().asks, max_shares=target, limit_price=ask + sim.max_slippage, budget=max(budget, 0.0),
            fee=fee, fee_in_collateral=coll, hidden=self._hidden_for(token, now),
        )
        shares = sum(s for _, s, _ in fills)
        vwap = sum(p * s for p, s, _ in fills) / shares if shares else ask
        fee_ps = sum(f for _, _, f in fills) / shares if shares else fee_ask
        slippage = (vwap - ask) + st.slippage_allowance
        net_edge = fair - vwap - fee_ps - st.slippage_allowance

        record = {
            "ts": now, "slug": w.slug, "series": w.series, "side": side, "token": token, "tau": tau,
            "spot": fv.spot, "s0": fv.strike, "sigma_annual": annualize(fv.sigma), "fair": fair,
            "best_ask": ask, "best_ask_size": ask_size, "vwap": vwap, "fee_ps": fee_ps,
            "edge_gross": edge_gross, "edge_after_fee": edge_after_fee, "slippage": slippage,
            "net_edge": net_edge, "threshold": st.safety_buffer, "shares": shares,
            "usd": sum(p * s for p, s, _ in fills) + (sum(f for _, _, f in fills) if coll else 0.0),
        }
        label = f"{side.upper()} {w.series}"

        reason = None
        if net_edge <= st.safety_buffer:
            if edge_gross <= fee_ask:
                reason, rkey = f"edge {cents(edge_gross)} < fee {cents(fee_ask)}", "below_fee"
            else:
                reason = (f"edge {cents(edge_after_fee)} after fee − slippage {cents(slippage)}"
                          f" ≤ buffer {cents(st.safety_buffer)}")
                rkey = "below_buffer"
        elif tau < st.min_seconds_remaining:
            reason, rkey = f"τ {tau:.0f}s < {st.min_seconds_remaining:.0f}s cutoff", "too_late"
        elif budget < ask:
            if sim.max_window_usd - self.window_spent(w.slug) < ask:
                reason, rkey = "window cap reached", "window_cap"
            else:
                reason, rkey = f"not enough cash (${self.cash():.2f})", "no_cash"
        elif shares < max(w.min_order_size, 0.01):
            reason = (f"only {shares:g} sh fillable ≤ {ask + sim.max_slippage:.2f} (min {w.min_order_size:g})"
                      if shares else "displayed size already taken by our earlier paper fill")
            rkey = "no_depth"

        if reason is not None:
            counts[(w.slug, side, rkey)] += 1
            if self.main and now - self._last_skip.get(key, 0.0) >= st.skip_log_interval_s:
                self._last_skip[key] = now
                record.update(decision="skipped", reason=reason)
                sid = self.db.insert_signal(record)
                self.skips_logged += 1
                self._log("SKIP", f"skip {label}: {reason}", ref=f"sig:{sid}")
                self._publish_signal(sid, record)
            return

        # A real signal: reserve the cash and send the (paper) order.
        counts[(w.slug, side, "signal")] += 1
        record.update(decision="pending", reason=None, wallet=self.wallet)
        sid = self.db.insert_signal(record)
        self.signals_taken += 1
        self.pending[sid] = _Pending(sid, w.slug, side, record["usd"])
        self._inflight.add(key)
        self._log(
            "SIG",
            f"{label} {shares:g} sh @ ask {ask:.2f} · fair {fair:.3f} · edge {cents(edge_after_fee)} after fee"
            f" · net {cents(net_edge)} > {cents(st.safety_buffer)} · sending ({self.latency_ms:.0f}ms)",
            ref=f"sig:{sid}",
        )
        self._publish_signal(sid, record)
        task = asyncio.get_running_loop().create_task(self._execute(sid, record, w, fv))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _hidden_for(self, token: str, now: float) -> dict[float, float]:
        ttl = self.cfg.sim.liquidity_memory_s
        entries = [e for e in self._hidden.get(token, []) if now - e[2] < ttl]
        self._hidden[token] = entries
        out: dict[float, float] = {}
        for price, shares, _ in entries:
            out[price] = out.get(price, 0.0) + shares
        return out

    # --- fills ---------------------------------------------------------------------------------
    async def _execute(self, sid: int, sig: dict[str, Any], w: "MarketWindow", fv: "FairValue") -> None:
        key = (w.slug, sig["side"])
        try:
            await asyncio.sleep(self.latency_ms / 1000.0)
            self._fill(sid, sig, w)
        finally:
            self.pending.pop(sid, None)
            self._inflight.discard(key)

    def _skip_fill(self, sid: int, sig: dict[str, Any], reason: str) -> None:
        if self.main:
            self._counts[(sig["slug"], sig["side"], "fill_skipped")] += 1
        self.db.update_signal(sid, decision="skipped", reason=reason)
        sig.update(decision="skipped", reason=reason)
        self._log("SKIP", f"skip {sig['side'].upper()} {sig['series']}: {reason}", ref=f"sig:{sid}")
        self._publish_signal(sid, sig)

    def _fill(self, sid: int, sig: dict[str, Any], w: "MarketWindow") -> None:
        sim = self.cfg.sim
        now = time.time()
        lat = self.latency_ms
        if now >= w.end_ts:
            return self._skip_fill(sid, sig, "window closed during latency")
        book = self.app.channel.books.get(sig["token"])
        if book is None or not book.ready:
            return self._skip_fill(sid, sig, "order book unavailable at fill time")
        asks = book.view().asks
        if not asks:
            return self._skip_fill(sid, sig, "no asks at fill time")
        if sim.adverse_move == "skip" and asks[0][0] > sig["best_ask"] + 1e-9:
            return self._skip_fill(sid, sig, f"ask moved {sig['best_ask']:.2f}→{asks[0][0]:.2f} during {lat:.0f}ms")
        assert w.fee is not None
        coll = self.cfg.fees.buy_fee_in == "collateral"
        limit = sig["best_ask"] + sim.max_slippage
        fills = walk_asks(
            asks, max_shares=sig["shares"], limit_price=limit, budget=sig["usd"] + 1e-9,
            fee=w.fee, fee_in_collateral=coll, hidden=self._hidden_for(sig["token"], now),
        )
        shares = round(sum(s for _, s, _ in fills), 2)
        if sim.order_type == "FOK" and shares + 1e-9 < sig["shares"]:
            return self._skip_fill(sid, sig, f"FOK: only {shares:g}/{sig['shares']:g} sh ≤ {limit:.2f} after {lat:.0f}ms")
        if shares < max(w.min_order_size, 0.01):
            return self._skip_fill(sid, sig, f"only {shares:g} sh ≤ {limit:.2f} after {lat:.0f}ms (min {w.min_order_size:g})")

        cost = sum(p * s for p, s, _ in fills)
        fee = sum(f for _, _, f in fills)
        avg = cost / shares
        held = shares if coll else shares - sum(f / p for p, _, f in fills)
        fair = sig["fair"]
        fv_now, _ = self.app.fair_value(w, now)
        fair_fill = None
        if fv_now is not None:
            fair_fill = fv_now.p_up if sig["side"] == "Up" else 1.0 - fv_now.p_up
        t = Trade(
            id=0, signal_id=sid, slug=w.slug, series=w.series, side=sig["side"], token=sig["token"],
            ts_signal=sig["ts"], ts_fill=now, latency_ms=(now - sig["ts"]) * 1000.0,
            shares=shares, shares_held=held, avg_price=avg, signal_ask=sig["best_ask"], signal_vwap=sig["vwap"],
            cost=cost, fee=fee, fee_in=self.cfg.fees.buy_fee_in, fair_signal=fair, fair_fill=fair_fill,
            edge_entry=fair - avg - fee / shares, levels=[list(x) for x in fills],
            status="OPEN", window_end=w.end_ts, wallet=self.wallet,
        )
        t.id = self.db.insert_trade(t.to_row())
        self.trades.append(t)
        if self.main:
            self._counts[(w.slug, sig["side"], "filled")] += 1
        self.db.update_signal(sid, decision="filled", reason=None, trade_id=t.id)
        sig.update(decision="filled", reason=None, trade_id=t.id)
        for p, s, _ in fills:
            self._hidden.setdefault(sig["token"], []).append((p, s, now))
        self._changed()
        moved = "" if abs(avg - sig["best_ask"]) < 1e-9 else f" (signal ask {sig['best_ask']:.2f})"
        self._log(
            "FILL",
            f"#{t.id} {t.side.upper()} {shares:g} sh @ {avg:.3f}{moved} · fair {fair:.2f} · "
            f"edge {cents(t.edge_entry)} after fee · fee ${fee:.3f} · filled",
            ref=f"trade:{t.id}",
        )
        self._publish_signal(sid, sig)
        self._publish_trade(t)

    # --- lifecycle / settlement -------------------------------------------------------------
    def maintain(self, now: float) -> None:
        for t in self.trades:
            if t.status == "OPEN" and now >= t.window_end:
                t.status = "PENDING"
                self.db.update_trade(t.id, status="PENDING")
                self._changed()
                self._publish_trade(t)

    def settle(self, slug: str, outcome: str) -> None:
        now = time.time()
        for t in self.trades:
            if t.slug != slug or t.status not in OPEN_STATES:
                continue
            won = t.side == outcome
            t.outcome = outcome
            t.payout = t.shares_held if won else 0.0
            t.pnl = t.payout - t.cash_out
            t.pnl_zero_fee = (t.shares if won else 0.0) - t.cost
            t.status = "WON" if won else "LOST"
            t.settled_ts = now
            self.db.update_trade(
                t.id, status=t.status, outcome=outcome, payout=t.payout, pnl=t.pnl,
                pnl_zero_fee=t.pnl_zero_fee, settled_ts=now,
            )
            self._changed()
            self._log(
                "WIN" if won else "LOSS",
                f"#{t.id} {t.side.upper()} {t.shares:g} sh @ {t.avg_price:.3f} → {outcome.upper()} · "
                f"P&L {t.pnl:+.2f} (fee {t.fee:.3f}, zero-fee {t.pnl_zero_fee:+.2f})",
                ref=f"trade:{t.id}",
            )
            self._publish_trade(t)

    # --- output -----------------------------------------------------------------------------------
    def _publish_trade(self, t: Trade) -> None:
        if not self.main:
            return
        self.app.publish({"type": "trade", "trade": t.public()})
        self.app.publish({"type": "stats", "stats": self.stats(time.time())})

    def _publish_signal(self, sid: int, rec: dict[str, Any]) -> None:
        if not self.main:
            return
        d = {k: v for k, v in rec.items() if k != "token"}
        d["id"] = sid
        self.app.publish({"type": "signal", "signal": d})

    def status_line(self, now: float) -> str:
        s = self.stats(now)
        return (
            f"bankroll ${s['bankroll']:.2f} ({100 * s['bankroll_pct']:+.2f}%) | open {s['open_count']} "
            f"(${s['at_risk']:.2f} at risk) | trades today {s['today']['trades']} | "
            f"net P&L today {s['today']['net_pnl']:+.2f} (zero-fee {s['today']['zero_fee_pnl']:+.2f}) | "
            f"all-time net {s['net_pnl']:+.2f}"
        )
