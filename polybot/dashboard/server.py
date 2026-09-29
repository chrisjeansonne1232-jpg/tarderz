"""FastAPI/uvicorn server that runs inside the bot's own asyncio loop.

Read-only by design: one page, static assets, a JSON snapshot endpoint and
one WebSocket. The WebSocket sends a full snapshot on connect, then price/book
ticks at `tick_hz` and trade/signal/log events as they happen. Nothing a
client sends is acted on.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import webbrowser
from pathlib import Path
from typing import Any, Protocol

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import DashboardConfig
from ..util import sleep_or_stop
from .state import clean

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class Source(Protocol):
    def snapshot(self) -> dict[str, Any]: ...
    def tick(self) -> dict[str, Any]: ...


def dumps(msg: Any) -> str:
    try:
        return json.dumps(msg, separators=(",", ":"), allow_nan=False)
    except ValueError:
        return json.dumps(clean(msg), separators=(",", ":"))


class _Client:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=5000)
        self.dead = False

    def offer(self, text: str) -> None:
        if self.dead:
            return
        try:
            self.queue.put_nowait(text)
        except asyncio.QueueFull:
            self.dead = True  # a client this far behind gets dropped; it reconnects and resnapshots
            log.warning("dashboard: dropping a client that fell behind")

    async def run(self) -> None:
        while not self.dead:
            text = await self.queue.get()
            await self.ws.send_text(text)
        await self.ws.close(code=1013)


class Hub:
    def __init__(self, source: Source, tick_hz: float) -> None:
        self.source = source
        self.period = 1.0 / tick_hz
        self.clients: set[_Client] = set()

    def push(self, msg: dict) -> None:
        """Send an event to every client immediately (trades, signals, log lines)."""
        if not self.clients:
            return
        text = dumps(msg)
        for c in list(self.clients):
            c.offer(text)

    async def ticker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if self.clients:
                try:
                    self.push(self.source.tick())
                except Exception:  # noqa: BLE001
                    log.exception("dashboard: building tick failed")
            await sleep_or_stop(stop, self.period)


class _InLoopServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):  # type: ignore[override]
        # The bot owns SIGINT/SIGTERM; uvicorn must not replace its handlers.
        yield


def lan_ip() -> str:
    """This computer's address on the local network (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # TEST-NET address; only picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return "<this computer's IP>"


def _open_when_up(server: uvicorn.Server, url: str) -> None:
    import time

    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - no browser available is fine
        pass


class DashboardServer:
    def __init__(self, cfg: DashboardConfig, source: Source, open_browser: bool = False) -> None:
        self.cfg = cfg
        self.source = source
        self.open_browser = open_browser
        self.hub = Hub(source, cfg.tick_hz)
        self.app = self._make_app()
        self.url = f"http://{cfg.dashboard_host}:{cfg.dashboard_port}"

    def _make_app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        no_cache = {"Cache-Control": "no-cache"}

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC / "index.html", headers=no_cache)

        @app.get("/api/snapshot")
        async def snapshot() -> JSONResponse:
            return JSONResponse(clean(self.source.snapshot()))

        app.mount("/static", StaticFiles(directory=STATIC), name="static")

        @app.websocket("/ws")
        async def ws_endpoint(ws: WebSocket) -> None:
            await ws.accept()
            client = _Client(ws)
            client.offer(dumps(self.source.snapshot()))  # snapshot first, then live events
            self.hub.clients.add(client)
            sender = asyncio.create_task(client.run())
            try:
                while True:
                    msg = await ws.receive()
                    if msg.get("type") == "websocket.disconnect":
                        break
                    # Anything a client sends is ignored: the dashboard is read-only.
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                self.hub.clients.discard(client)
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await sender

        return app

    def _bind(self) -> socket.socket:
        host, port = self.cfg.dashboard_host, self.cfg.dashboard_port
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        if os.name == "nt":
            # On Windows SO_REUSEADDR would let a second bot share the port silently.
            sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR), 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as e:
            sock.close()
            raise RuntimeError(f"dashboard: cannot listen on {host}:{port}: {e}") from e
        sock.listen(64)
        sock.setblocking(False)
        if port == 0:
            self.url = f"http://{host}:{sock.getsockname()[1]}"
        return sock

    async def serve(self, stop: asyncio.Event) -> None:
        sock = self._bind()
        config = uvicorn.Config(
            self.app, log_level="warning", access_log=False, lifespan="off", ws="websockets-sansio",
            timeout_graceful_shutdown=2,
        )
        server = _InLoopServer(config)
        server_task = asyncio.create_task(server.serve(sockets=[sock]))
        ticker = asyncio.create_task(self.hub.ticker(stop))
        port = sock.getsockname()[1]
        local_url = f"http://127.0.0.1:{port}"
        print(f"dashboard: {local_url}", flush=True)
        if self.cfg.dashboard_host in ("0.0.0.0", "::"):
            print(f"dashboard on other devices (same wifi): http://{lan_ip()}:{port}", flush=True)
        log.info("dashboard listening on %s", self.url)
        if self.open_browser:
            asyncio.get_running_loop().run_in_executor(None, _open_when_up, server, local_url)
        stop_wait = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait([server_task, stop_wait], return_when=asyncio.FIRST_COMPLETED)
            if server_task in done:
                exc = server_task.exception()
                raise RuntimeError(f"dashboard server stopped: {exc!r}")
        finally:
            server.should_exit = True
            for c in list(self.hub.clients):
                c.dead = True
                with contextlib.suppress(Exception):
                    await c.ws.close(code=1001)
            stop_wait.cancel()
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(server_task, timeout=5)
            with contextlib.suppress(asyncio.CancelledError):
                await ticker
            sock.close()
