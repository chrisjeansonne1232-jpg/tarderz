"""Poll Gamma after each window closes until Polymarket posts the resolution."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable

from .config import ResolutionConfig
from .markets import _json_list
from .rest import GammaClient, HttpError
from .util import sleep_or_stop

log = logging.getLogger(__name__)


def parse_resolution(market: dict) -> tuple[str | None, dict]:
    """Return ("Up"|"Down", detail) once the market is closed with a decisive
    outcome, else (None, detail). Live (unresolved) markets also carry
    outcomePrices, so `closed` is required before trusting them."""
    detail = {
        "closed": market.get("closed"),
        "outcomePrices": market.get("outcomePrices"),
        "umaResolutionStatus": market.get("umaResolutionStatus"),
    }
    if market.get("closed") is not True:
        return None, detail
    try:
        outcomes = [str(o) for o in _json_list(market.get("outcomes"))]
        prices = [float(p) for p in _json_list(market.get("outcomePrices"))]
    except (ValueError, TypeError):
        return None, detail
    if len(outcomes) != len(prices) or not prices:
        return None, detail
    winners = [i for i, p in enumerate(prices) if p >= 0.999]
    losers = [i for i, p in enumerate(prices) if p <= 0.001]
    if len(winners) != 1 or len(losers) != len(prices) - 1:
        return None, detail
    name = outcomes[winners[0]].strip().lower()
    if name not in ("up", "down"):
        return None, detail
    return ("Up" if name == "up" else "Down"), detail


@dataclass
class Pending:
    slug: str
    market_id: str
    end_ts: float
    next_poll: float
    polls: int = 0


class ResolutionWatcher:
    def __init__(
        self,
        cfg: ResolutionConfig,
        gamma: GammaClient,
        on_resolved: Callable[[str, str, dict], None],
    ) -> None:
        self.cfg = cfg
        self.gamma = gamma
        self.on_resolved = on_resolved
        self.pending: dict[str, Pending] = {}

    def add(self, slug: str, market_id: str, end_ts: float) -> None:
        if slug not in self.pending:
            self.pending[slug] = Pending(slug, market_id, end_ts, end_ts + self.cfg.first_poll_after_end_s)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            now = time.time()
            for p in [p for p in self.pending.values() if p.next_poll <= now]:
                if stop.is_set():
                    break
                await self._poll(p)
            await sleep_or_stop(stop, 1.0)

    async def _poll(self, p: Pending) -> None:
        p.polls += 1
        now = time.time()
        interval = self.cfg.poll_s if now - p.end_ts < self.cfg.fast_window_s else self.cfg.slow_poll_s
        p.next_poll = now + interval
        try:
            m = await self.gamma.market(p.market_id) if p.market_id else None
            if m is None:
                m = await self.gamma.market_by_slug(p.slug)
        except HttpError as e:
            log.warning("resolution poll for %s failed: %s", p.slug, e)
            return
        if not m:
            return
        outcome, detail = parse_resolution(m)
        if outcome is None:
            if p.polls % 20 == 0:
                log.info("%s still unresolved after %d polls (%s)", p.slug, p.polls, detail)
            return
        del self.pending[p.slug]
        self.on_resolved(p.slug, outcome, detail)
