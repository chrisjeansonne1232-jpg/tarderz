"""Reconnecting websocket client base used by every live feed."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

import aiohttp

from .util import Backoff, sleep_or_stop

log = logging.getLogger(__name__)


class ReconnectingWS:
    """Connect, (re)subscribe, read, and reconnect with backoff on any failure.

    Subclasses implement `on_open` (send subscriptions) and `on_text` (parse a
    message). A text heartbeat is sent every `ping_interval_s` if `ping_text`
    is set. If no data message arrives for `stale_s` the connection is
    considered dead and is recycled, which catches silent stalls that never
    produce a close frame.
    """

    name = "ws"

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        ping_text: str | None = None,
        ping_interval_s: float = 10.0,
        stale_s: float = 60.0,
        protocol_heartbeat_s: float | None = None,
    ) -> None:
        self.session = session
        self.url = url
        self.ping_text = ping_text
        self.ping_interval_s = ping_interval_s
        self.stale_s = stale_s
        self.protocol_heartbeat_s = protocol_heartbeat_s
        self.connected = False
        self.connects = 0
        self.disconnects = 0
        self.messages = 0
        self.last_data_recv = 0.0
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        # Optional hook: on_status(feed_name, "connected" | "disconnected", detail)
        self.on_status: Callable[[str, str, str], None] | None = None

    # --- subclass hooks -------------------------------------------------
    async def on_open(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        pass

    def on_text(self, text: str, recv_ts: float) -> None:
        raise NotImplementedError

    def on_disconnect(self) -> None:
        pass

    def wants_connection(self) -> bool:
        return True

    def watchdog_active(self) -> bool:
        return True

    # --- helpers --------------------------------------------------------
    async def send_json(self, payload: object) -> bool:
        ws = self._ws
        if ws is None or ws.closed:
            return False
        try:
            await ws.send_json(payload)
            return True
        except (ConnectionError, RuntimeError, aiohttp.ClientError) as e:
            log.warning("%s: send failed: %s", self.name, e)
            return False

    async def reconnect(self) -> None:
        """Force the current connection closed; the run loop reconnects."""
        ws = self._ws
        if ws is not None and not ws.closed:
            await _close_quickly(ws)

    _last_reason = "connection lost"

    def _status(self, event: str, detail: str) -> None:
        if self.on_status is not None:
            try:
                self.on_status(self.name, event, detail)
            except Exception:  # noqa: BLE001
                log.exception("%s: status hook failed", self.name)

    # --- main loop ------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        backoff = Backoff()
        while not stop.is_set():
            if not self.wants_connection():
                await sleep_or_stop(stop, 0.5)
                continue
            try:
                async with self.session.ws_connect(
                    self.url,
                    heartbeat=self.protocol_heartbeat_s,
                    max_msg_size=0,
                    autoping=True,
                    # Don't let a server that ignores our close frame stall shutdown.
                    timeout=aiohttp.ClientWSTimeout(ws_close=1.0),
                ) as ws:
                    self._ws = ws
                    self.connected = True
                    self.connects += 1
                    self.last_data_recv = time.time()
                    log.info("%s: connected to %s", self.name, self.url)
                    self._status("connected", self.url)
                    await self.on_open(ws)
                    await self._read_loop(ws, stop, backoff)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - any failure means reconnect
                log.warning("%s: connection error: %s: %s", self.name, type(e).__name__, e)
                self._last_reason = f"{type(e).__name__}: {e}"
            finally:
                if self.connected:
                    self.disconnects += 1
                    if not stop.is_set():
                        self._status("disconnected", self._last_reason)
                self._last_reason = "connection lost"
                self._ws = None
                self.connected = False
                self.on_disconnect()
            if stop.is_set():
                break
            delay = backoff.next()
            log.info("%s: reconnecting in %.1fs", self.name, delay)
            await sleep_or_stop(stop, delay)

    async def _read_loop(
        self, ws: aiohttp.ClientWebSocketResponse, stop: asyncio.Event, backoff: Backoff
    ) -> None:
        last_ping = time.time()
        got_data = False
        while not stop.is_set():
            now = time.time()
            if self.ping_text and now - last_ping >= self.ping_interval_s:
                await ws.send_str(self.ping_text)
                last_ping = now
            if self.watchdog_active() and now - self.last_data_recv > self.stale_s:
                log.warning("%s: no data for %.0fs, recycling connection", self.name, now - self.last_data_recv)
                self._last_reason = f"no data for {now - self.last_data_recv:.0f}s"
                return
            try:
                msg = await ws.receive(timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if msg.type == aiohttp.WSMsgType.TEXT:
                text = msg.data
                if text in ("PONG", "pong", ""):
                    continue
                recv_ts = time.time()
                self.last_data_recv = recv_ts
                self.messages += 1
                if not got_data:
                    got_data = True
                    backoff.reset()
                try:
                    self.on_text(text, recv_ts)
                except Exception:  # noqa: BLE001 - never let one bad message kill the feed
                    log.exception("%s: error handling message: %.300s", self.name, text)
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                log.warning("%s: server closed connection (%s)", self.name, ws.close_code)
                self._last_reason = f"server closed ({ws.close_code})"
                return
            elif msg.type == aiohttp.WSMsgType.ERROR:
                log.warning("%s: websocket error: %s", self.name, ws.exception())
                return
        await _close_quickly(ws)


async def _close_quickly(ws: aiohttp.ClientWebSocketResponse, timeout: float = 1.0) -> None:
    """Send a close frame but don't wait long for the server's reply: aiohttp
    restarts its close timeout on every data message, so a server that keeps
    streaming without acknowledging would otherwise stall shutdown. Cancelling
    close() makes aiohttp drop the transport."""
    try:
        await asyncio.wait_for(ws.close(), timeout=timeout)
    except (asyncio.TimeoutError, ConnectionError, RuntimeError, aiohttp.ClientError):
        pass
