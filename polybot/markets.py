"""Market windows: discovery via Gamma, rules verification, fee parameters,
boundary (start/end) prices, and rollover to the next window."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from .config import Config, SeriesConfig
from .fees import FeeModel
from .rest import ClobClient, GammaClient, HttpError
from .util import fmt_utc, parse_iso, sleep_or_stop

if TYPE_CHECKING:
    from .db import Database
    from .feeds import ChainlinkFeed, CoinbaseFeed
    from .market_ws import MarketChannel

log = logging.getLogger(__name__)


class ParseError(ValueError):
    pass


def _json_list(v: object) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        out = json.loads(v)
        if isinstance(out, list):
            return out
    raise ParseError(f"expected a JSON list, got {v!r}")


@dataclass
class MarketWindow:
    series: str
    slug: str
    event_id: str
    market_id: str
    condition_id: str
    question: str
    description: str
    resolution_source: str
    start_ts: float
    end_ts: float
    up_token: str
    down_token: str
    tick_size: float
    min_order_size: float
    fees_enabled: bool | None
    gamma_fee: tuple[float, float] | None  # (rate, exponent) from Gamma feeSchedule
    rules_ok: bool
    rules_notes: list[str] = field(default_factory=list)
    fee: FeeModel | None = None
    # Boundary prices. Chainlink is what the market resolves on.
    s0_chainlink: float | None = None
    s0_chainlink_ts: float | None = None
    s0_coinbase: float | None = None
    s0_status: str = "pending"  # pending | ok | missed (for the configured strike source)
    end_chainlink: float | None = None
    end_chainlink_ts: float | None = None
    end_coinbase: float | None = None
    end_status: str = "pending"
    # Streaming / resolution state.
    subscribed: bool = False
    streaming_done: bool = False
    resolved_outcome: str | None = None
    ws_resolved_outcome: str | None = None

    @property
    def tokens(self) -> list[str]:
        return [self.up_token, self.down_token]

    def outcome_of(self, token: str) -> str:
        return "Up" if token == self.up_token else "Down"

    def chainlink_predicted_outcome(self) -> str | None:
        if self.s0_chainlink is None or self.end_chainlink is None:
            return None
        return "Up" if self.end_chainlink >= self.s0_chainlink else "Down"  # ties -> Up


def parse_event(event: dict, series: SeriesConfig, required_terms: list[str]) -> MarketWindow:
    """Build a MarketWindow from a Gamma event (one binary Up/Down market)."""
    markets = event.get("markets") or []
    if not markets:
        raise ParseError(f"event {event.get('slug')} has no markets")
    slug = event.get("slug") or ""
    m = next((x for x in markets if x.get("slug") == slug), markets[0])

    outcomes = [str(o) for o in _json_list(m.get("outcomes"))]
    tokens = [str(t) for t in _json_list(m.get("clobTokenIds"))]
    if len(outcomes) != 2 or len(tokens) != 2:
        raise ParseError(f"{slug}: expected 2 outcomes/tokens, got {outcomes} / {len(tokens)} tokens")
    idx = {o.strip().lower(): i for i, o in enumerate(outcomes)}
    if set(idx) != {"up", "down"}:
        raise ParseError(f"{slug}: outcomes are {outcomes}, expected Up/Down")

    notes: list[str] = []
    rules_ok = True

    end_raw = m.get("endDate") or event.get("endDate")
    if not end_raw:
        raise ParseError(f"{slug}: no endDate")
    end_ts = parse_iso(end_raw)
    start_raw = m.get("eventStartTime") or event.get("eventStartTime") or event.get("startTime")
    slug_ts: int | None = None
    try:
        slug_ts = int(slug.rsplit("-", 1)[-1])
    except ValueError:
        pass
    if start_raw:
        start_ts = parse_iso(start_raw)
    elif slug_ts is not None:
        start_ts = float(slug_ts)
        notes.append("no eventStartTime; start taken from slug")
    else:
        start_ts = end_ts - series.interval_s
        notes.append("no eventStartTime; start = endDate - interval")
    if slug_ts is not None and abs(slug_ts - start_ts) > 1:
        notes.append(f"slug timestamp {slug_ts} != eventStartTime {start_ts:.0f}")
        rules_ok = False
    if abs((end_ts - start_ts) - series.interval_s) > 1:
        notes.append(f"window length {end_ts - start_ts:.0f}s != configured {series.interval_s}s")
        rules_ok = False

    description = m.get("description") or event.get("description") or ""
    missing = [t for t in required_terms if t.lower() not in description.lower()]
    if missing:
        rules_ok = False
        notes.append(f"description is missing required term(s): {missing}")

    fs = m.get("feeSchedule")
    gamma_fee = None
    if isinstance(fs, dict) and "rate" in fs:
        gamma_fee = (float(fs.get("rate") or 0.0), float(fs.get("exponent") or 0.0))

    return MarketWindow(
        series=series.name,
        slug=slug,
        event_id=str(event.get("id", "")),
        market_id=str(m.get("id", "")),
        condition_id=str(m.get("conditionId", "")),
        question=str(m.get("question") or event.get("title") or ""),
        description=description,
        resolution_source=str(m.get("resolutionSource") or event.get("resolutionSource") or ""),
        start_ts=start_ts,
        end_ts=end_ts,
        up_token=tokens[idx["up"]],
        down_token=tokens[idx["down"]],
        tick_size=float(m.get("orderPriceMinTickSize") or 0.01),
        min_order_size=float(m.get("orderMinSize") or 0.0),
        fees_enabled=m.get("feesEnabled"),
        gamma_fee=gamma_fee,
        rules_ok=rules_ok,
        rules_notes=notes,
    )


async def resolve_fee_model(cfg: Config, clob: ClobClient, w: MarketWindow) -> tuple[FeeModel, list[str]]:
    """Pick the fee parameters for a window per `fees.source`, cross-checking
    CLOB (/clob-markets fd) against Gamma (feeSchedule)."""
    notes: list[str] = []
    clob_fee: tuple[float, float] | None = None
    try:
        cm = await clob.clob_market(w.condition_id)
        if cm:
            fd = cm.get("fd")
            if isinstance(fd, dict):
                clob_fee = (float(fd.get("r") or 0.0), float(fd.get("e") or 0.0))
            if cm.get("mts"):
                w.tick_size = float(cm["mts"])
        else:
            notes.append("/clob-markets returned nothing")
    except HttpError as e:
        notes.append(f"/clob-markets failed: {e}")

    if clob_fee and w.gamma_fee and (
        abs(clob_fee[0] - w.gamma_fee[0]) > 1e-9 or abs(clob_fee[1] - w.gamma_fee[1]) > 1e-9
    ):
        notes.append(f"fee mismatch: CLOB fd={clob_fee} vs Gamma feeSchedule={w.gamma_fee}")

    fixed = (cfg.fees.fixed_rate, cfg.fees.fixed_exponent)
    order = {
        "clob": [("clob", clob_fee), ("gamma", w.gamma_fee), ("fixed-fallback", fixed)],
        "gamma": [("gamma", w.gamma_fee), ("clob", clob_fee), ("fixed-fallback", fixed)],
        "fixed": [("fixed", fixed)],
    }[cfg.fees.source]
    src, params = next((s, p) for s, p in order if p is not None)
    if src == "fixed-fallback":
        notes.append("no fee parameters from CLOB or Gamma; using fees.fixed_* fallback")
    if w.fees_enabled is False and params[0] > 0:
        notes.append("Gamma says feesEnabled=false but fee rate > 0; using the rate anyway")
    return FeeModel(rate=params[0], exponent=params[1], source=src, decimals=cfg.fees.round_decimals), notes


class SeriesTracker:
    """Keeps the current (and next) window of one recurring series discovered,
    subscribed, and annotated with boundary prices; hands ended windows off."""

    def __init__(
        self,
        series: SeriesConfig,
        cfg: Config,
        gamma: GammaClient,
        clob: ClobClient,
        channel: "MarketChannel",
        coinbase: "CoinbaseFeed",
        chainlink: "ChainlinkFeed | None",
        db: "Database",
        on_ended: Callable[[MarketWindow], None],
        on_discovered: Callable[[MarketWindow], None] | None = None,
    ) -> None:
        self.s = series
        self.cfg = cfg
        self.gamma = gamma
        self.clob = clob
        self.channel = channel
        self.coinbase = coinbase
        self.chainlink = chainlink
        self.db = db
        self.on_ended = on_ended
        self.on_discovered = on_discovered
        self.windows: dict[int, MarketWindow] = {}
        self._next_try: dict[int, float] = {}
        self.discovery_failures = 0

    def current(self, now: float) -> MarketWindow | None:
        start = int(now // self.s.interval_s) * self.s.interval_s
        return self.windows.get(start)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self._tick(time.time())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep tracking no matter what
                log.exception("%s: tracker error", self.s.name)
            await sleep_or_stop(stop, 0.25)

    async def _tick(self, now: float) -> None:
        iv = self.s.interval_s
        cur = int(now // iv) * iv
        wanted = [cur]
        if now >= cur + iv - self.cfg.markets.discover_ahead_s:
            wanted.append(cur + iv)
        for start in wanted:
            if start not in self.windows and now >= self._next_try.get(start, 0.0):
                await self._discover(start)

        for start, w in list(self.windows.items()):
            if not w.subscribed and not w.streaming_done and now < w.end_ts:
                await self.channel.subscribe(w.tokens)
                w.subscribed = True
            self._capture_boundaries(w, now)
            if w.subscribed and now >= w.end_ts + self.cfg.markets.unsubscribe_after_end_s:
                await self.channel.unsubscribe(w.tokens)
                w.subscribed = False
                w.streaming_done = True
            if w.streaming_done and w.end_status != "pending":
                self.on_ended(w)
                del self.windows[start]

    async def _discover(self, start: int) -> None:
        slug = f"{self.s.slug_prefix}-{start}"
        try:
            event = await self.gamma.event_by_slug(slug)
            if event is None and self.s.series_slug:
                events = await self.gamma.events_by_series(self.s.series_slug)
                event = next((e for e in events if e.get("slug") == slug), None)
            if event is None:
                raise ParseError(f"no Gamma event with slug {slug}")
            w = parse_event(event, self.s, self.cfg.markets.required_description_terms)
            w.fee, fee_notes = await resolve_fee_model(self.cfg, self.clob, w)
        except (HttpError, ParseError, KeyError, ValueError) as e:
            self.discovery_failures += 1
            self._next_try[start] = time.time() + self.cfg.markets.discover_retry_s
            log.warning("%s: discovery of %s failed: %s", self.s.name, slug, e)
            self.db.log_event("WARN", "discovery_failed", slug, str(e))
            return
        self._next_try.pop(start, None)
        for n in fee_notes:
            log.warning("%s: %s", slug, n)
            self.db.log_event("WARN", "fee_note", slug, n)
        for n in w.rules_notes:
            log.warning("%s: %s", slug, n)
            self.db.log_event("WARN", "rules_note", slug, n)
        self.windows[start] = w
        self.db.upsert_market(w)
        if self.on_discovered is not None:
            self.on_discovered(w)
        log.info(
            "%s: discovered %s  %s -> %s  rules_ok=%s  fee %s  resolution=%s",
            self.s.name, slug, fmt_utc(w.start_ts), fmt_utc(w.end_ts), w.rules_ok,
            w.fee.describe() if w.fee else "?", w.resolution_source or "?",
        )

    def _coinbase_asof(self, ts: float) -> float | None:
        h = self.coinbase.history
        first, last = h.first(), h.last()
        if first is None or last is None or first[0] > ts or last[0] < ts:
            return None  # can't bracket the boundary: joined late or feed not there yet
        s = h.asof(ts)
        return s[1] if s else None

    def _capture_boundaries(self, w: MarketWindow, now: float) -> None:
        late = self.cfg.chainlink.boundary_max_delay_s + 2.0
        use_cl = self.cfg.model.strike_source == "chainlink"
        changed = False
        for which, ts in (("s0", w.start_ts), ("end", w.end_ts)):
            status_attr = f"{which}_status"
            if now < ts:
                continue
            cl_attr = "s0_chainlink" if which == "s0" else "end_chainlink"
            cb_attr = "s0_coinbase" if which == "s0" else "end_coinbase"
            # Keep filling in the non-strike feed's boundary price for a minute
            # (informational); after that only a pending status needs deciding.
            trying = now <= ts + late + 60.0
            need_cl = trying and self.chainlink is not None and getattr(w, cl_attr) is None
            need_cb = trying and getattr(w, cb_attr) is None
            if not need_cl and not need_cb and getattr(w, status_attr) != "pending":
                continue
            if need_cl:
                bp = self.chainlink.boundary_price(ts)
                if bp is not None:
                    setattr(w, f"{cl_attr}_ts", bp[0])
                    setattr(w, cl_attr, bp[1])
                    changed = True
            if need_cb:
                v = self._coinbase_asof(ts)
                if v is not None:
                    setattr(w, cb_attr, v)
                    changed = True
            if getattr(w, status_attr) != "pending":
                continue
            have = getattr(w, cl_attr) if use_cl else getattr(w, cb_attr)
            if have is not None:
                setattr(w, status_attr, "ok")
                changed = True
            elif now > ts + late:
                setattr(w, status_attr, "missed")
                changed = True
                log.warning("%s: %s boundary price missed (feed gap or joined late)", w.slug, which)
        if changed:
            self.db.update_boundaries(w)
