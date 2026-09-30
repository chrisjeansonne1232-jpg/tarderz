"""Read-only REST clients: Gamma (discovery/resolution), CLOB (market params),
Coinbase (candles for vol bootstrap). No authenticated endpoints are used."""

from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp

log = logging.getLogger(__name__)


class HttpError(RuntimeError):
    pass


class _Rest:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, timeout_s: float) -> None:
        self.session = session
        self.base = base_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)
        self.requests = 0
        self.last_ok = 0.0  # time of the last successful response
        self.last_error: tuple[float, str] | None = None

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET JSON. Returns None on 404; raises HttpError on other failures."""
        url = f"{self.base}{path}"
        self.requests += 1
        try:
            async with self.session.get(url, params=params, timeout=self.timeout) as r:
                if r.status == 404:
                    self.last_ok = time.time()
                    return None
                if r.status >= 400:
                    body = (await r.text())[:300]
                    raise HttpError(f"GET {url} -> {r.status}: {body}")
                data = await r.json(content_type=None)
                self.last_ok = time.time()
                return data
        except (aiohttp.ClientError, TimeoutError) as e:
            self.last_error = (time.time(), f"{type(e).__name__}: {e}")
            raise HttpError(f"GET {url} failed: {type(e).__name__}: {e}") from e
        except HttpError as e:
            self.last_error = (time.time(), str(e))
            raise


class GammaClient(_Rest):
    async def event_by_slug(self, slug: str) -> dict | None:
        data = await self.get("/events", {"slug": slug})
        if isinstance(data, list):
            return data[0] if data else None
        return data or None

    async def event_by_slug_path(self, slug: str) -> dict | None:
        """GET /events/slug/{slug}: the single-event endpoint (may carry fields the list endpoint omits)."""
        data = await self.get(f"/events/slug/{slug}")
        return data if isinstance(data, dict) and data else None

    async def events_by_series(self, series_slug: str, limit: int = 100) -> list[dict]:
        data = await self.get(
            "/events",
            {"series_slug": series_slug, "closed": "false", "limit": limit, "order": "endDate", "ascending": "true"},
        )
        return data if isinstance(data, list) else []

    async def market(self, market_id: str) -> dict | None:
        return await self.get(f"/markets/{market_id}")

    async def market_by_slug(self, slug: str) -> dict | None:
        data = await self.get("/markets", {"slug": slug})
        if isinstance(data, list):
            return data[0] if data else None
        return data or None


class ClobClient(_Rest):
    async def clob_market(self, condition_id: str) -> dict | None:
        """CLOB V2 market parameters: t (tokens), mts (min tick), nr (neg risk),
        fd (fee details: r = rate, e = exponent)."""
        return await self.get(f"/clob-markets/{condition_id}")

    async def legacy_fee_rate(self, token_id: str) -> Any:
        """GET /fee-rate. Returns the legacy `base_fee` (bps); NOT the V2 fee.
        Logged for reference only."""
        return await self.get("/fee-rate", {"token_id": token_id})

    async def book(self, token_id: str) -> dict | None:
        return await self.get("/book", {"token_id": token_id})


class CoinbaseRest(_Rest):
    async def candles(self, product_id: str, granularity_s: int = 60) -> list[tuple[float, float, float, float, float]]:
        """Recent (bar_start_ts, open, high, low, close), oldest first. Coinbase
        rows are [time, low, high, open, close, volume], newest first."""
        rows = await self.get(f"/products/{product_id}/candles", {"granularity": granularity_s})
        if not isinstance(rows, list):
            return []
        rows = sorted((r for r in rows if isinstance(r, list) and len(r) >= 5), key=lambda r: r[0])
        return [(float(r[0]), float(r[3]), float(r[2]), float(r[1]), float(r[4])) for r in rows]
