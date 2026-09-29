"""Wires feeds, market tracking, the fair-value model, the paper engine and
output (terminal, SQLite, dashboard) together."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import zlib
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

import aiohttp

from .book import OrderBook
from .candles import CandleBuilder
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


class _ExecLogHandler(logging.Handler):
    """Mirror polybot warnings into the execution log (WARN / RECONNECT)."""

    def __init__(self, app: "App") -> None:
        super().__init__(level=logging.WARNING)
        self.app = app
        self._recent: dict[str, float] = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            now = time.time()
            if now - self._recent.get(msg, 0.0) < 5.0:
                return  # identical warning repeated within 5 s
            self._recent[msg] = now
            if len(self._recent) > 500:
                self._recent = {k: v for k, v in self._recent.items() if now - v < 60}
            tag = "RECONNECT" if record.name == "polybot.ws" else "WARN"
            self.app.exec_log(tag, msg, echo=False)
        except Exception:  # noqa: BLE001
            self.handleError(record)


class App:
    """mode: "watch" (terminal fair-value view, no trading) or "run" (paper trading)."""

    def __init__(self, cfg: Config, mode: str = "watch", dashboard: bool = False) -> None:
        self.cfg = cfg
        self.mode = mode
        self.dashboard_enabled = dashboard
        self.stop = asyncio.Event()
        self.started_at = time.time()
        self._cond_to_slug: dict[str, str] = {}
        self._subscribers: list[Callable[[dict], None]] = []
        self.log_ring: deque[tuple[float, str, str, str | None]] = deque(maxlen=cfg.dashboard.log_lines)
        self.candles = CandleBuilder()
        self.engine = None
        self.loop_lag_ms = 0.0
        self.loop_lag_max_ms = 0.0
        self.db: Database | None = None

    # --- lifecycle ------------------------------------------------------------
    async def run(self) -> None:
        cfg = self.cfg
        self.db = Database(cfg.general.db_path)
        self.db.log_event("INFO", "start", None, {"mode": self.mode})
        handler = _ExecLogHandler(self)
        logging.getLogger("polybot").addHandler(handler)
        try:
            async with aiohttp.ClientSession(
                trust_env=True, headers={"User-Agent": cfg.general.user_agent}
            ) as session:
                self._build(session)
                await self._bootstrap_history()
                await self._supervise()
        finally:
            logging.getLogger("polybot").removeHandler(handler)
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
        for feed in (self.coinbase, self.chainlink, self.channel):
            if feed is not None:
                feed.on_status = self._on_feed_status
        self.resolver = ResolutionWatcher(cfg.resolution, self.gamma, self._on_resolved)
        assert self.db is not None
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
        if self.mode == "run":
            from .engine import PaperEngine

            self.engine = PaperEngine(self)
            self.engine.reconcile_on_start()
        self.dashboard = None
        if self.dashboard_enabled:
            from .dashboard.server import DashboardServer
            from .dashboard.state import LiveSource

            self.dashboard = DashboardServer(cfg.dashboard, LiveSource(self))
            self.subscribe(self.dashboard.hub.push)

    async def _supervise(self) -> None:
        coros = {
            "coinbase": self.coinbase.run(self.stop),
            "polymarket": self.channel.run(self.stop),
            "resolver": self.resolver.run(self.stop),
            "vol_sampler": self._vol_sampler(),
            "maintenance": self._maintenance(),
            "output": self._output_loop(),
            "recorder": self._recorder(),
            "loop_lag": self._loop_lag(),
        }
        if self.chainlink is not None:
            coros["chainlink"] = self.chainlink.run(self.stop)
        if self.cfg.markets.gamma_heartbeat_s > 0:
            coros["gamma_heartbeat"] = self._gamma_heartbeat()
        if self.dashboard is not None:
            coros["dashboard"] = self.dashboard.serve(self.stop)
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
                    assert self.db is not None
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
            if self.engine is not None:
                await self.engine.shutdown()

    async def _bootstrap_history(self) -> None:
        """Seed vol and the candle chart from Coinbase 1-minute candles."""
        try:
            candles = await self.cb_rest.candles(self.cfg.spot.product_id, 60)
        except HttpError as e:
            log.warning("Coinbase candle bootstrap failed (%s); waiting for live data", e)
            return
        now = time.time()
        closed = [c for c in candles if c[0] + 60 <= now]  # drop the in-progress bar
        self.candles.seed(closed)
        assert self.db is not None
        for t, o, h, l, c in closed[-self.candles.closed.maxlen:]:  # type: ignore[index]
            self.db.upsert_candle(t, o, h, l, c, 0, "rest")
        if self.cfg.model.vol_bootstrap_candles:
            closes = [c[4] for c in closed][-(int(self.cfg.model.vol_lookback_min) + 1):]
            n = self.vol.set_bootstrap_from_closes(closes, 60.0)
            if n and self.vol.bootstrap_var is not None:
                log.info("vol bootstrap: %d one-minute returns, sigma %.1f%%/yr", n, 100 * annualize(self.vol.bootstrap_var ** 0.5))

    # --- event bus / execution log -------------------------------------------------------
    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self._subscribers.append(fn)

    def publish(self, msg: dict) -> None:
        for fn in self._subscribers:
            try:
                fn(msg)
            except Exception:  # noqa: BLE001
                log.exception("subscriber failed")

    def exec_log(self, tag: str, msg: str, ref: str | None = None, echo: bool = True) -> None:
        """One line of the execution log: memory ring, SQLite, dashboard, file log."""
        ts = time.time()
        self.log_ring.append((ts, tag, msg, ref))
        if self.db is not None:
            self.db.insert_exec_log(ts, tag, msg, ref)
        self.publish({"type": "log", "line": [ts, tag, msg, ref]})
        if echo:
            log.info("[%s] %s", tag, msg)

    # --- feed callbacks -------------------------------------------------------
    def _on_spot(self, tick: SpotTick) -> None:
        closed = self.candles.add(tick.exch_ts, tick.last)
        if closed is not None and self.db is not None:
            self.db.upsert_candle(closed[0], closed[1], closed[2], closed[3], closed[4], int(closed[5]), "ticks")
        if self.engine is not None:
            self.engine.evaluate(time.time())

    def _on_oracle(self, tick: OracleTick) -> None:
        last = self.coinbase.history.last()
        if last is None or last[0] < tick.obs_ts - 2.0:
            return  # Coinbase not current enough to align with this observation
        cb = self.coinbase.price_at(tick.obs_ts)
        if cb is not None:
            self.basis.update(tick.obs_ts, cb - tick.value)

    def _on_feed_status(self, name: str, event: str, detail: str) -> None:
        if event == "connected":
            feed = {"coinbase": self.coinbase, "chainlink": self.chainlink, "polymarket": self.channel}.get(name)
            again = feed is not None and feed.connects > 1
            self.exec_log("RECONNECT", f"{name}: {'reconnected' if again else 'connected'} ({detail})", echo=False)

    def _on_discovered(self, w: MarketWindow) -> None:
        self._cond_to_slug[w.condition_id] = w.slug
        fee = w.fee.describe() if w.fee else "fee ?"
        rules = "rules verified" if w.rules_ok else "RULES NOT VERIFIED (won't trade): " + "; ".join(w.rules_notes)
        self.exec_log("MKT", f"{w.slug} · {fmt_hms(w.start_ts)}–{fmt_hms(w.end_ts)}Z · {fee} · {rules}")

    def _on_window_ended(self, w: MarketWindow) -> None:
        self.resolver.add(w.slug, w.market_id, w.end_ts)
        log.info(
            "%s ended: chainlink start %s end %s -> predicted %s",
            w.slug, w.s0_chainlink, w.end_chainlink, w.chainlink_predicted_outcome() or "?",
        )

    def _on_ws_resolved(self, msg: dict) -> None:
        slug = self._cond_to_slug.get(str(msg.get("market", "")))
        outcome = str(msg.get("winning_outcome", ""))
        if slug and outcome and self.db is not None:
            self.db.set_ws_resolution(slug, outcome)

    def _on_resolved(self, slug: str, outcome: str, detail: dict) -> None:
        assert self.db is not None
        self.db.set_resolution(slug, outcome, detail)
        row = self.db.conn.execute("SELECT chainlink_predicted FROM markets WHERE slug=?", (slug,)).fetchone()
        predicted = row[0] if row else None
        if predicted and predicted != outcome:
            log.warning("RESOLVED %s: %s, but our Chainlink boundary prices predicted %s", slug, outcome, predicted)
            self.db.log_event("WARN", "oracle_mismatch", slug, {"resolved": outcome, "predicted": predicted})
        self.exec_log("SETTLE", f"{slug} resolved {outcome.upper()} (Gamma) · Chainlink predicted {(predicted or 'n/a').upper()}")
        if self.engine is not None:
            self.engine.settle(slug, outcome)

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
        last_status = time.time()
        while not self.stop.is_set():
            await self.channel.maintain()
            now = time.time()
            if self.engine is not None:
                self.engine.maintain(now)
                if now - last_status >= self.cfg.sim.status_interval_s:
                    last_status = now
                    line = self.engine.status_line(now)
                    print(f"{fmt_hms(now)}Z  {line}", flush=True)
                    log.info("status: %s", line)
            await sleep_or_stop(self.stop, 1.0)

    async def _gamma_heartbeat(self) -> None:
        """Poll the live market's Gamma record so the Gamma status reflects reality."""
        while not self.stop.is_set():
            w = self.primary_window(time.time())
            if w is not None and w.market_id:
                try:
                    await self.gamma.market(w.market_id)
                except HttpError as e:
                    log.debug("gamma heartbeat failed: %s", e)
            await sleep_or_stop(self.stop, self.cfg.markets.gamma_heartbeat_s)

    async def _loop_lag(self) -> None:
        interval = 0.25
        window: deque[float] = deque(maxlen=40)
        while not self.stop.is_set():
            t0 = time.perf_counter()
            await asyncio.sleep(interval)
            lag = max(0.0, (time.perf_counter() - t0 - interval) * 1000.0)
            window.append(lag)
            self.loop_lag_ms = lag
            self.loop_lag_max_ms = max(window)

    async def _recorder(self) -> None:
        """1-second snapshots of spot and top of book (drives REPLAY)."""
        step = self.cfg.recorder.interval_s
        depth = self.cfg.recorder.depth
        next_t = time.time()
        while not self.stop.is_set():
            now = time.time()
            try:
                self._record(now, depth)
            except Exception:  # noqa: BLE001
                log.exception("recorder failed")
            next_t += step
            if next_t < now:
                next_t = now + step
            await sleep_or_stop(self.stop, next_t - time.time())

    def _record(self, now: float, depth: int) -> None:
        assert self.db is not None
        tick = self.coinbase.last
        cl = self.chainlink.last if self.chainlink else None
        est = self.vol.estimate()
        ages = self.feed_ages(now)
        for tr in self.trackers:
            w = tr.current(now)
            fv = self.fair_value(w, now)[0] if w else None
            ub = ua = db_ = da = None
            ladder: dict[str, Any] = {"ages": ages}
            if w is not None:
                for key, tok in (("u", w.up_token), ("d", w.down_token)):
                    b = self.channel.books.get(tok)
                    if b is not None and b.ready:
                        v = b.view()
                        ladder[key + "b"] = [list(x) for x in v.bids[:depth]]
                        ladder[key + "a"] = [list(x) for x in v.asks[:depth]]
                ub = ladder.get("ub", [[None]])[0][0] if ladder.get("ub") else None
                ua = ladder.get("ua", [[None]])[0][0] if ladder.get("ua") else None
                db_ = ladder.get("db", [[None]])[0][0] if ladder.get("db") else None
                da = ladder.get("da", [[None]])[0][0] if ladder.get("da") else None
            blob = zlib.compress(json.dumps(ladder, separators=(",", ":")).encode(), 6)
            self.db.insert_snapshot_1s((
                now, tr.s.name, w.slug if w else None, (w.end_ts - now) if w else None,
                tick.price if tick else None, cl.value if cl else None, self.basis.mean,
                annualize(est.sigma) if est else None, fv.strike if fv else (w.s0_chainlink if w else None),
                fv.p_up if fv else None, ub, ua, db_, da, blob,
            ))

    async def _output_loop(self) -> None:
        last_summary = time.time()
        while not self.stop.is_set():
            now = time.time()
            if self.mode == "watch":
                print(self.render_watch(now), flush=True)
            if now - last_summary >= 60.0:
                self._log_minute_summary()
                last_summary = now
            await sleep_or_stop(self.stop, self.cfg.watch.print_interval_s)

    def _log_minute_summary(self) -> None:
        assert self.db is not None
        agree, total = self.db.oracle_check()
        log.info(
            "feeds: coinbase %s (%d msgs, %d reconnects) | chainlink %s | polymarket %s (%d msgs, %d reconnects, %d resyncs) | oracle check %d/%d | pending resolutions %d",
            "up" if self.coinbase.connected else "DOWN", self.coinbase.messages, max(0, self.coinbase.connects - 1),
            ("up" if self.chainlink.connected else "DOWN") if self.chainlink else "off",
            "up" if self.channel.connected else "DOWN", self.channel.messages, max(0, self.channel.connects - 1),
            self.channel.resyncs, agree, total, len(self.resolver.pending),
        )

    # --- helpers for output / dashboard ------------------------------------------------
    def primary_window(self, now: float) -> MarketWindow | None:
        name = self.cfg.dashboard.primary_series
        for tr in self.trackers:
            if tr.s.name == name:
                return tr.current(now)
        return self.trackers[0].current(now) if self.trackers else None

    def feed_ages(self, now: float) -> dict[str, float | None]:
        def age(t: float) -> float | None:
            return round((now - t) * 1000.0) if t else None

        return {
            "coinbase": age(self.coinbase.last_data_recv),
            "chainlink": age(self.chainlink.last_data_recv) if self.chainlink else None,
            "clob": age(self.channel.last_data_recv),
            "gamma": age(self.gamma.last_ok),
        }

    # --- stage 1 terminal display ----------------------------------------------------------
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


def _fmt_book(book: OrderBook | None) -> str:
    if book is None or not book.ready:
        return "(no book)"
    b, a = book.best_bid(), book.best_ask()
    bs = f"{b[0]:.2f}x{b[1]:.0f}" if b else "--"
    as_ = f"{a[0]:.2f}x{a[1]:.0f}" if a else "--"
    sync = "" if book.mismatch_since is None else "≠"
    return f"{bs}/{as_}{sync}"
