"""Wires feeds, market tracking, the fair-value model and output together."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import aiohttp

from .book import OrderBook
from .config import Config
from .db import Database
from .fairvalue import BasisEstimator, VolEstimator, annualize, fair_up_probability
from .feeds import ChainlinkFeed, CoinbaseFeed, OracleTick, SpotTick
from .market_ws import MarketChannel
from .markets import MarketWindow, SeriesTracker
from .resolution import ResolutionWatcher
from .rest import ClobClient, CoinbaseRest, GammaClient, HttpError
from .util import fmt_hms, fmt_mmss, sleep_or_stop

log = logging.getLogger(__name__)


@dataclass
class FairValue:
    ts: float
    tau: float
    spot: float  # Coinbase price used by the model
    spot_adj: float  # after Coinbase->Chainlink basis correction (if enabled)
    strike: float
    strike_src: str
    sigma: float  # per sqrt(second)
    basis: float | None
    p_up: float


class App:
    def __init__(self, cfg: Config, mode: str = "watch") -> None:
        self.cfg = cfg
        self.mode = mode
        self.stop = asyncio.Event()
        self._cond_to_slug: dict[str, str] = {}

    # --- lifecycle ------------------------------------------------------------
    async def run(self) -> None:
        cfg = self.cfg
        self.db = Database(cfg.general.db_path)
        self.db.log_event("INFO", "start", None, {"mode": self.mode})
        try:
            async with aiohttp.ClientSession(
                trust_env=True, headers={"User-Agent": cfg.general.user_agent}
            ) as session:
                self._build(session)
                await self._bootstrap_vol()
                await self._supervise()
        finally:
            self.db.log_event("INFO", "stop", None, {"mode": self.mode})
            self.db.close()

    def _build(self, session: aiohttp.ClientSession) -> None:
        cfg = self.cfg
        t = cfg.general.http_timeout_s
        self.gamma = GammaClient(session, cfg.endpoints.gamma, t)
        self.clob = ClobClient(session, cfg.endpoints.clob, t)
        self.cb_rest = CoinbaseRest(session, cfg.endpoints.coinbase_rest, t)
        m = cfg.model
        self.vol = VolEstimator(m.vol_lookback_min * 60.0, m.vol_sample_s, m.vol_min_live_s, m.vol_floor_annual)
        self.basis = BasisEstimator(m.basis_halflife_s)
        self.coinbase = CoinbaseFeed(session, cfg.endpoints.coinbase_ws, cfg.spot, on_tick=self._on_spot)
        self.chainlink = (
            ChainlinkFeed(session, cfg.endpoints.rtds_ws, cfg.chainlink, cfg.spot.history_s, on_tick=self._on_oracle)
            if cfg.chainlink.enabled
            else None
        )
        self.channel = MarketChannel(session, cfg.endpoints.market_ws, cfg.polymarket_ws, on_resolved=self._on_ws_resolved)
        self.resolver = ResolutionWatcher(cfg.resolution, self.gamma, self._on_resolved)
        for slug, market_id, end_ts in self.db.unresolved_markets(time.time()):
            self.resolver.add(slug, market_id, end_ts)
        self.trackers = [
            SeriesTracker(
                s, cfg, self.gamma, self.clob, self.channel, self.coinbase, self.chainlink, self.db,
                on_ended=self._on_window_ended, on_discovered=self._on_discovered,
            )
            for s in cfg.markets.series
            if s.enabled
        ]

    async def _supervise(self) -> None:
        coros = {
            "coinbase": self.coinbase.run(self.stop),
            "polymarket": self.channel.run(self.stop),
            "resolver": self.resolver.run(self.stop),
            "vol_sampler": self._vol_sampler(),
            "maintenance": self._maintenance(),
            "output": self._output_loop(),
        }
        if self.chainlink is not None:
            coros["chainlink"] = self.chainlink.run(self.stop)
        for tr in self.trackers:
            coros[f"tracker:{tr.s.name}"] = tr.run(self.stop)
        tasks = {asyncio.create_task(c, name=n): n for n, c in coros.items()}
        stop_task = asyncio.create_task(self.stop.wait())
        try:
            done, _ = await asyncio.wait([stop_task, *tasks], return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t is not stop_task:
                    exc = t.exception()
                    log.critical("component %s exited unexpectedly: %r", tasks[t], exc)
                    self.db.log_event("CRITICAL", "component_died", None, f"{tasks[t]}: {exc!r}")
            self.stop.set()
            _, pending = await asyncio.wait(tasks, timeout=5.0)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            stop_task.cancel()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, stop_task, return_exceptions=True)

    async def _bootstrap_vol(self) -> None:
        if not self.cfg.model.vol_bootstrap_candles:
            return
        try:
            candles = await self.cb_rest.candles(self.cfg.spot.product_id, 60)
        except HttpError as e:
            log.warning("vol bootstrap from Coinbase candles failed (%s); waiting for live data", e)
            return
        now = time.time()
        closed = [c for t, c in candles if t + 60 <= now]  # drop the in-progress bar
        closes = closed[-(int(self.cfg.model.vol_lookback_min) + 1):]
        n = self.vol.set_bootstrap_from_closes(closes, 60.0)
        if n and self.vol.bootstrap_var is not None:
            log.info("vol bootstrap: %d one-minute returns, sigma %.1f%%/yr", n, 100 * annualize(self.vol.bootstrap_var ** 0.5))

    # --- feed callbacks -------------------------------------------------------
    def _on_spot(self, tick: SpotTick) -> None:
        pass  # stage 2 evaluates signals here

    def _on_oracle(self, tick: OracleTick) -> None:
        last = self.coinbase.history.last()
        if last is None or last[0] < tick.obs_ts - 2.0:
            return  # Coinbase not current enough to align with this observation
        cb = self.coinbase.price_at(tick.obs_ts)
        if cb is not None:
            self.basis.update(tick.obs_ts, cb - tick.value)

    def _on_discovered(self, w: MarketWindow) -> None:
        self._cond_to_slug[w.condition_id] = w.slug

    def _on_window_ended(self, w: MarketWindow) -> None:
        self.resolver.add(w.slug, w.market_id, w.end_ts)
        log.info(
            "%s ended: chainlink start %s end %s -> predicted %s",
            w.slug, w.s0_chainlink, w.end_chainlink, w.chainlink_predicted_outcome() or "?",
        )

    def _on_ws_resolved(self, msg: dict) -> None:
        slug = self._cond_to_slug.get(str(msg.get("market", "")))
        outcome = str(msg.get("winning_outcome", ""))
        if slug and outcome:
            self.db.set_ws_resolution(slug, outcome)

    def _on_resolved(self, slug: str, outcome: str, detail: dict) -> None:
        self.db.set_resolution(slug, outcome, detail)
        row = self.db.conn.execute("SELECT chainlink_predicted FROM markets WHERE slug=?", (slug,)).fetchone()
        predicted = row[0] if row else None
        if predicted and predicted != outcome:
            log.warning("RESOLVED %s: %s, but our Chainlink boundary prices predicted %s", slug, outcome, predicted)
            self.db.log_event("WARN", "oracle_mismatch", slug, {"resolved": outcome, "predicted": predicted})
        else:
            log.info("RESOLVED %s: %s (chainlink predicted %s)", slug, outcome, predicted or "n/a")

    # --- model ----------------------------------------------------------------
    def fair_value(self, w: MarketWindow, now: float) -> tuple[FairValue | None, str]:
        """Fair P(Up) for window `w` at `now`, or (None, reason)."""
        tick = self.coinbase.last
        if tick is None:
            return None, "no spot"
        if now - tick.recv_ts > self.cfg.spot.stale_s:
            return None, "spot stale"
        if now < w.start_ts:
            return None, "not started"
        est = self.vol.estimate()
        if est is None:
            return None, "vol warm-up"
        m = self.cfg.model
        basis: float | None = None
        if m.strike_source == "chainlink":
            strike = w.s0_chainlink
            spot_adj = tick.price
            if m.basis_correction:
                if self.basis.mean is None:
                    return None, "basis warm-up"
                basis = self.basis.mean
                spot_adj = tick.price - basis
        else:
            strike = w.s0_coinbase
            spot_adj = tick.price
        if strike is None:
            return None, f"S0 {w.s0_status}"
        tau = max(w.end_ts - now, 0.0)
        p = fair_up_probability(spot_adj, strike, est.sigma, tau, m.basis_noise_bps / 1e4)
        return FairValue(now, tau, tick.price, spot_adj, strike, m.strike_source, est.sigma, basis, p), ""

    # --- periodic tasks -------------------------------------------------------
    async def _vol_sampler(self) -> None:
        step = self.cfg.model.vol_sample_s
        next_t = time.time()
        while not self.stop.is_set():
            now = time.time()
            tick = self.coinbase.last
            if tick is not None and now - tick.recv_ts <= self.cfg.spot.stale_s:
                self.vol.add_sample(now, tick.price)
            next_t += step
            if next_t < now:
                next_t = now + step
            await sleep_or_stop(self.stop, next_t - time.time())

    async def _maintenance(self) -> None:
        while not self.stop.is_set():
            await self.channel.maintain()
            await sleep_or_stop(self.stop, 1.0)

    async def _output_loop(self) -> None:
        cfg = self.cfg.watch
        last_snap = 0.0
        last_summary = time.time()
        while not self.stop.is_set():
            now = time.time()
            if self.mode == "watch":
                print(self.render_watch(now), flush=True)
            if now - last_snap >= cfg.snapshot_interval_s:
                self._snapshot(now)
                last_snap = now
            if now - last_summary >= 60.0:
                self._log_minute_summary()
                last_summary = now
            await sleep_or_stop(self.stop, cfg.print_interval_s)

    def _log_minute_summary(self) -> None:
        agree, total = self.db.oracle_check()
        log.info(
            "feeds: coinbase %s (%d msgs, %d reconnects) | chainlink %s | polymarket %s (%d msgs, %d reconnects, %d resyncs) | oracle check %d/%d | pending resolutions %d",
            "up" if self.coinbase.connected else "DOWN", self.coinbase.messages, max(0, self.coinbase.connects - 1),
            ("up" if self.chainlink.connected else "DOWN") if self.chainlink else "off",
            "up" if self.channel.connected else "DOWN", self.channel.messages, max(0, self.channel.connects - 1),
            self.channel.resyncs, agree, total, len(self.resolver.pending),
        )

    def _snapshot(self, now: float) -> None:
        for tr in self.trackers:
            w = tr.current(now)
            if w is None:
                continue
            fv, _ = self.fair_value(w, now)
            up = self.channel.books.get(w.up_token)
            dn = self.channel.books.get(w.down_token)
            ub, ua = _top(up)
            db_, da = _top(dn)
            tick = self.coinbase.last
            cl = self.chainlink.last if self.chainlink else None
            self.db.insert_snapshot({
                "ts": now, "slug": w.slug, "tau": w.end_ts - now,
                "spot": tick.price if tick else None,
                "spot_adj": fv.spot_adj if fv else None,
                "strike": fv.strike if fv else None,
                "sigma_annual": annualize(fv.sigma) if fv else None,
                "basis": self.basis.mean,
                "p_up": fv.p_up if fv else None,
                "up_bid": ub[0] if ub else None, "up_bid_sz": ub[1] if ub else None,
                "up_ask": ua[0] if ua else None, "up_ask_sz": ua[1] if ua else None,
                "down_bid": db_[0] if db_ else None, "down_bid_sz": db_[1] if db_ else None,
                "down_ask": da[0] if da else None, "down_ask_sz": da[1] if da else None,
                "spot_age_ms": (now - tick.recv_ts) * 1000 if tick else None,
                "oracle_age_ms": (now - cl.recv_ts) * 1000 if cl else None,
                "book_age_ms": (now - up.recv_ts) * 1000 if up and up.ready else None,
            })

    # --- stage 1 display ----------------------------------------------------------
    def render_watch(self, now: float) -> str:
        tick = self.coinbase.last
        cl = self.chainlink.last if self.chainlink else None
        est = self.vol.estimate()
        parts = [fmt_hms(now) + "Z"]
        if tick:
            parts.append(
                f"CB {tick.price:,.2f} (lag {1000 * (tick.recv_ts - tick.exch_ts):.0f}ms, age {1000 * (now - tick.recv_ts):.0f}ms)"
            )
        else:
            parts.append("CB --")
        if self.chainlink is not None:
            if cl:
                parts.append(
                    f"CL {cl.value:,.2f} (lag {1000 * (cl.recv_ts - cl.obs_ts):.0f}ms, age {1000 * (now - cl.recv_ts):.0f}ms)"
                )
            else:
                parts.append("CL --")
            if self.basis.mean is not None:
                parts.append(f"basis {self.basis.mean:+.2f}±{self.basis.std:.2f}")
        if est:
            src = "boot+live" if est.used_bootstrap else "live"
            parts.append(f"σ {100 * annualize(est.sigma):.1f}%/yr ({src} {est.live_coverage_s / 60:.0f}m{', floored' if est.floored else ''})")
        else:
            parts.append("σ warming up")
        parts.append(f"PM {'up' if self.channel.connected else 'DOWN'}")
        lines = ["  ".join(parts)]
        for tr in self.trackers:
            w = tr.current(now)
            if w is None:
                lines.append(f"  {tr.s.name:<8} discovering…")
                continue
            lines.append("  " + self._render_window(w, now))
        return "\n".join(lines)

    def _render_window(self, w: MarketWindow, now: float) -> str:
        fv, why = self.fair_value(w, now)
        up = self.channel.books.get(w.up_token)
        dn = self.channel.books.get(w.down_token)
        flag = "" if w.rules_ok else " [RULES?]"
        head = f"{w.series:<8} τ {fmt_mmss(w.end_ts - now)}"
        if fv:
            s0 = f"S0 {fv.strike:,.2f}[{'CL' if fv.strike_src == 'chainlink' else 'CB'}] S* {fv.spot_adj:,.2f}"
            fair = f"fair Up {fv.p_up:.3f} Dn {1 - fv.p_up:.3f}"
        else:
            s0 = f"S0 {w.s0_chainlink:,.2f}" if w.s0_chainlink else "S0 --"
            fair = f"fair -- ({why})"
        books = f"Up {_fmt_book(up)}  Dn {_fmt_book(dn)}"
        edge = ""
        if fv and w.fee:
            e = []
            for name, book, p in (("Up", up, fv.p_up), ("Dn", dn, 1.0 - fv.p_up)):
                ask = book.best_ask() if book and book.ready else None
                if ask:
                    e.append(f"{name} {100 * (p - ask[0] - w.fee.fee_per_share(ask[0])):+.1f}¢")
            if e:
                edge = "edge after fee: " + " ".join(e)
        return " | ".join(x for x in (head + flag, s0, fair, books, edge) if x)


def _top(book: OrderBook | None) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    if book is None or not book.ready:
        return None, None
    return book.best_bid(), book.best_ask()


def _fmt_book(book: OrderBook | None) -> str:
    if book is None or not book.ready:
        return "(no book)"
    b, a = book.best_bid(), book.best_ask()
    bs = f"{b[0]:.2f}x{b[1]:.0f}" if b else "--"
    as_ = f"{a[0]:.2f}x{a[1]:.0f}" if a else "--"
    sync = "" if book.mismatch_since is None else "≠"
    return f"{bs}/{as_}{sync}"
