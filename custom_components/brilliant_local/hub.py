"""Combine several Brilliantly Local agents into one view of the home and route commands.

Every agent reports the whole home, but only controls a panel directly when it
runs on that panel or has a live peer link to it. The hub therefore prefers,
per panel: the agent running on that panel, then any agent that reports the
panel online, then any other copy (shown as offline). Commands go to the same
ordered list and fall through to the next agent on connection-type failures.

Kept free of Home Assistant imports so it can be tested standalone.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from .client import BrilliantClient, CannotConnect, CommandFailed

_LOGGER = logging.getLogger(__name__)

READY_TIMEOUT = 15

# Failures that mean "this agent can't reach the panel right now" - try the next agent.
RETRYABLE_ERRORS = frozenset(
    {
        "panel_unreachable",
        "timeout",
        "bus not ready",
        "not connected to agent",
        "connection lost",
        "agent did not answer in time",
    }
)


class BrilliantHub:
    """Presents the same interface entities use on a single client, backed by many."""

    def __init__(self, clients: list[BrilliantClient]) -> None:
        if not clients:
            raise ValueError("at least one agent is required")
        self.clients = clients
        self.panels: dict[str, dict[str, Any]] = {}
        self._listeners: list[Callable[[], None]] = []
        for client in clients:
            client.add_listener(self._on_client_update)

    # ---- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Start every agent connection; return once any agent has delivered state."""
        for client in self.clients:
            client.start_background()
        waiters = [asyncio.ensure_future(client.wait_ready()) for client in self.clients]
        try:
            done, _ = await asyncio.wait(waiters, timeout=READY_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
        if not done:
            await self.stop()
            hosts = ", ".join(f"{c.host}:{c.port}" for c in self.clients)
            raise CannotConnect(f"no data from any agent ({hosts})")
        self._on_client_update()

    async def stop(self) -> None:
        await asyncio.gather(*(client.stop() for client in self.clients))

    # ---- merged state -------------------------------------------------------

    @property
    def connected(self) -> bool:
        return any(client.connected for client in self.clients)

    def sources(self, panel_id: str) -> list[BrilliantClient]:
        """Connected agents that know this panel, best first."""
        known = [c for c in self.clients if c.connected and panel_id in c.panels]
        own = [c for c in known if c.agent_panel == panel_id]
        linked = [c for c in known if c not in own and c.panels[panel_id]["online"]]
        rest = [c for c in known if c not in own and c not in linked]
        return own + linked + rest

    def _merge(self) -> dict[str, dict[str, Any]]:
        panel_ids = {pid for c in self.clients if c.connected for pid in c.panels}
        merged: dict[str, dict[str, Any]] = {}
        for panel_id in panel_ids:
            sources = self.sources(panel_id)
            best = sources[0]
            panel = dict(best.panels[panel_id])
            reachable_via = [c.host for c in sources if c.panels[panel_id]["online"]]
            panel["online"] = bool(reachable_via)
            panel["via"] = reachable_via[0] if reachable_via else None
            panel["reachable_via"] = reachable_via
            merged[panel_id] = panel
        return merged

    def _on_client_update(self) -> None:
        self.panels = self._merge()
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:
                _LOGGER.exception("Error in Brilliant listener")

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback)

    def load(self, panel_id: str, load_id: str) -> dict[str, Any] | None:
        panel = self.panels.get(panel_id)
        if panel is None:
            return None
        return next((ld for ld in panel["loads"] if ld["id"] == load_id), None)

    # ---- commands -----------------------------------------------------------

    async def set_load(
        self, panel_id: str, load_id: str, *, on: bool | None = None, brightness: int | None = None
    ) -> None:
        candidates = [c for c in self.sources(panel_id) if c.panels[panel_id]["online"]]
        if not candidates:
            raise CommandFailed("panel_unreachable")
        last_error: CommandFailed | None = None
        for client in candidates:
            try:
                await client.set_load(panel_id, load_id, on=on, brightness=brightness)
            except CommandFailed as err:
                if str(err) not in RETRYABLE_ERRORS:
                    raise
                _LOGGER.debug("Agent %s could not reach panel %s (%s); trying next", client.host, panel_id, err)
                last_error = err
            else:
                return
        assert last_error is not None
        raise last_error
