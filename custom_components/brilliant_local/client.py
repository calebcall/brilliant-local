"""Async client for the on-panel Brilliantly Local agent (newline-delimited JSON over TCP).

Kept free of Home Assistant imports so it can be tested standalone.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from collections.abc import Callable
from typing import Any

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10
COMMAND_TIMEOUT = 15
PING_INTERVAL = 20
IDLE_TIMEOUT = 60
MAX_LINE = 1024 * 1024


class BrilliantError(Exception):
    """Base error."""


class CannotConnect(BrilliantError):
    """The agent could not be reached."""


class InvalidAuth(BrilliantError):
    """The agent rejected the token."""


class CommandFailed(BrilliantError):
    """The agent reported a failed command."""


class BrilliantClient:
    """Keeps a persistent connection to the agent and mirrors its panel state."""

    def __init__(self, host: str, port: int, token: str) -> None:
        self.host = host
        self.port = port
        self._token = token
        self.hello: dict[str, Any] = {}
        self.panels: dict[str, dict[str, Any]] = {}
        self.connected = False
        self._listeners: list[Callable[[], None]] = []
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._ids = itertools.count(1)
        self._task: asyncio.Task[None] | None = None
        self._first_snapshot = asyncio.Event()
        self._stopping = False

    # ---- connection lifecycle ---------------------------------------------

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, limit=MAX_LINE), CONNECT_TIMEOUT
            )
            hello = json.loads(await asyncio.wait_for(reader.readline(), CONNECT_TIMEOUT) or b"{}")
        except (OSError, TimeoutError, ValueError) as err:
            raise CannotConnect(str(err) or type(err).__name__) from err
        if hello.get("type") != "hello":
            writer.close()
            raise CannotConnect("unexpected greeting from agent")
        self.hello = hello
        writer.write(json.dumps({"type": "auth", "token": self._token}).encode() + b"\n")
        await writer.drain()
        return reader, writer

    async def validate(self) -> dict[str, Any]:
        """Connect once, authenticate, and return the agent's hello message."""
        reader, writer = await self._open()
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), CONNECT_TIMEOUT)
                if not line:
                    raise InvalidAuth("connection closed during auth")
                msg = json.loads(line)
                if msg.get("type") == "error":
                    raise InvalidAuth(msg.get("error", "auth_failed"))
                if msg.get("type") == "snapshot":
                    return self.hello
        except TimeoutError as err:
            raise CannotConnect("no snapshot from agent (is its bus session up?)") from err
        finally:
            writer.close()

    @property
    def agent_panel(self) -> str | None:
        """Id of the panel this agent runs on (known once connected)."""
        return self.hello.get("agent_panel")

    def start_background(self) -> None:
        """Start the connection loop without waiting; it keeps retrying until stopped."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"brilliant_local {self.host}")

    async def wait_ready(self) -> None:
        """Wait until the first snapshot has arrived."""
        await self._first_snapshot.wait()

    async def start(self) -> None:
        """Start the background connection loop and wait for the first snapshot."""
        self.start_background()
        try:
            await asyncio.wait_for(self._first_snapshot.wait(), CONNECT_TIMEOUT + 5)
        except TimeoutError as err:
            await self.stop()
            raise CannotConnect(f"no data from agent at {self.host}:{self.port}") from err

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                reader, writer = await self._open()
                self._writer = writer
                backoff = 1.0
                await self._session(reader, writer)
            except InvalidAuth:
                _LOGGER.error("Brilliant agent at %s rejected the token", self.host)
            except (CannotConnect, OSError, TimeoutError, ValueError) as err:
                _LOGGER.debug("Brilliant agent connection error: %s", err)
            finally:
                self._writer = None
                self._fail_pending("connection lost")
                if self.connected and not self._stopping:
                    _LOGGER.warning("Lost connection to Brilliant agent at %s", self.host)
                    self.connected = False
                    self._notify()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        pinger = asyncio.create_task(self._ping_loop())
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT)
                if not line:
                    return
                self._handle(json.loads(line))
        finally:
            pinger.cancel()
            writer.close()

    async def _ping_loop(self) -> None:
        # A failed ping just ends the loop; the idle timeout in _session reconnects.
        with contextlib.suppress(BrilliantError, OSError):
            while True:
                await asyncio.sleep(PING_INTERVAL)
                await self._send({"type": "ping"})

    def _handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "error":
            raise InvalidAuth(msg.get("error", ""))
        if kind == "snapshot":
            self.panels = {p["id"]: p for p in msg["panels"]}
            if not self.connected:
                _LOGGER.info("Connected to Brilliant agent at %s", self.host)
            self.connected = True
            self._first_snapshot.set()
            self._notify()
        elif kind == "panel":
            panel = msg["panel"]
            self.panels[panel["id"]] = panel
            self._notify()
        elif kind == "result":
            fut = self._pending.pop(msg.get("id"), None)
            if fut is not None and not fut.done():
                fut.set_result(msg)

    # ---- state access / commands -------------------------------------------

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback)

    def _notify(self) -> None:
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:
                _LOGGER.exception("Error in Brilliant listener")

    def _fail_pending(self, reason: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(CommandFailed(reason))
        self._pending.clear()

    async def _send(self, msg: dict[str, Any]) -> None:
        if self._writer is None:
            raise CommandFailed("not connected to agent")
        self._writer.write(json.dumps(msg, separators=(",", ":")).encode() + b"\n")
        await self._writer.drain()

    def load(self, panel_id: str, load_id: str) -> dict[str, Any] | None:
        panel = self.panels.get(panel_id)
        if panel is None:
            return None
        return next((ld for ld in panel["loads"] if ld["id"] == load_id), None)

    async def set_load(
        self, panel_id: str, load_id: str, *, on: bool | None = None, brightness: int | None = None
    ) -> None:
        req_id = next(self._ids)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        msg: dict[str, Any] = {"type": "set", "id": req_id, "panel": panel_id, "load": load_id}
        if on is not None:
            msg["on"] = on
        if brightness is not None:
            msg["brightness"] = brightness
        try:
            await self._send(msg)
            result = await asyncio.wait_for(fut, COMMAND_TIMEOUT)
        except TimeoutError as err:
            raise CommandFailed("agent did not answer in time") from err
        finally:
            self._pending.pop(req_id, None)
        if not result.get("ok"):
            raise CommandFailed(result.get("error", "unknown error"))
