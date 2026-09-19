"""Tests for BrilliantHub: merging several agents' views and routing commands.

Run with a Python that has Home Assistant installed (the package __init__ imports it):
    python -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.brilliant_local.client import CannotConnect, CommandFailed  # noqa: E402
from custom_components.brilliant_local.hub import BrilliantHub  # noqa: E402

KITCHEN, THEATER, SWELL = "kitchen", "theater", "swell"


def panel(panel_id, online, name=None):
    return {
        "id": panel_id,
        "name": name or panel_id,
        "online": online,
        "last_seen": 0,
        "loads": [{"id": "g0", "name": f"{panel_id} light", "available": online}],
        "motion": [],
    }


class FakeClient:
    """Stands in for BrilliantClient: holds an agent's view and records commands."""

    def __init__(self, host, agent_panel, views, connected=True, ready=True):
        self.host = host
        self.port = 61172
        self.agent_panel = agent_panel
        self.hello = {"agent_panel": agent_panel, "home_id": "home"}
        self.panels = {p["id"]: p for p in views}
        self.connected = connected
        self._ready = ready
        self.listeners = []
        self.calls = []
        self.fail_with = None  # error string to raise from set_load
        self.started = self.stopped = False

    def add_listener(self, cb):
        self.listeners.append(cb)
        return lambda: self.listeners.remove(cb)

    def start_background(self):
        self.started = True

    async def wait_ready(self):
        if not self._ready:
            await asyncio.Event().wait()

    async def stop(self):
        self.stopped = True

    async def set_load(self, panel_id, load_id, *, on=None, brightness=None):
        self.calls.append((panel_id, load_id, on, brightness))
        if self.fail_with:
            raise CommandFailed(self.fail_with)

    def push(self, **changes):
        """Simulate the agent reporting new state."""
        for key, value in changes.items():
            setattr(self, key, value)
        for cb in list(self.listeners):
            cb()


def home_view(kitchen=True, theater=True, swell=True):
    return [panel(KITCHEN, kitchen), panel(THEATER, theater), panel(SWELL, swell)]


class HubTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Kitchen's agent currently has no link to Theater; Theater's own agent is up.
        self.kitchen = FakeClient("10.1.126.15", KITCHEN, home_view(theater=False))
        self.theater = FakeClient("10.1.126.2", THEATER, home_view())
        self.swell = FakeClient("10.1.126.188", SWELL, home_view())
        self.hub = BrilliantHub([self.kitchen, self.theater, self.swell])
        await self.hub.start()

    async def test_panel_online_if_any_agent_reaches_it(self):
        self.assertTrue(self.hub.panels[THEATER]["online"])
        self.assertEqual(self.hub.panels[THEATER]["via"], "10.1.126.2")
        self.assertEqual(self.hub.panels[THEATER]["reachable_via"], ["10.1.126.2", "10.1.126.188"])

    async def test_prefers_panel_own_agent_for_state(self):
        self.theater.panels[THEATER] = {**panel(THEATER, True), "name": "from theater agent"}
        self.theater.push()
        self.assertEqual(self.hub.panels[THEATER]["name"], "from theater agent")

    async def test_commands_go_to_panel_own_agent(self):
        await self.hub.set_load(THEATER, "g0", on=True)
        await self.hub.set_load(KITCHEN, "g0", on=False, brightness=10)
        self.assertEqual(self.theater.calls, [(THEATER, "g0", True, None)])
        self.assertEqual(self.kitchen.calls, [(KITCHEN, "g0", False, 10)])
        self.assertEqual(self.swell.calls, [])

    async def test_falls_back_when_own_agent_disconnected(self):
        self.theater.push(connected=False)
        await self.hub.set_load(THEATER, "g0", on=True)
        # Kitchen has no link to Theater, so Swellness (which has one) takes it.
        self.assertEqual(self.kitchen.calls, [])
        self.assertEqual(self.swell.calls, [(THEATER, "g0", True, None)])
        self.assertEqual(self.hub.panels[THEATER]["via"], "10.1.126.188")

    async def test_falls_through_on_retryable_error(self):
        self.theater.fail_with = "timeout"
        await self.hub.set_load(THEATER, "g0", on=True)
        self.assertEqual(len(self.theater.calls), 1)
        self.assertEqual(self.swell.calls, [(THEATER, "g0", True, None)])

    async def test_non_retryable_error_is_raised_immediately(self):
        self.theater.fail_with = "unknown or non-controllable load"
        with self.assertRaisesRegex(CommandFailed, "non-controllable"):
            await self.hub.set_load(THEATER, "g0", on=True)
        self.assertEqual(self.swell.calls, [])

    async def test_all_agents_fail_raises_last_error(self):
        self.theater.fail_with = self.swell.fail_with = "panel_unreachable"
        with self.assertRaisesRegex(CommandFailed, "panel_unreachable"):
            await self.hub.set_load(THEATER, "g0", on=True)

    async def test_unreachable_everywhere_is_offline_and_refused(self):
        self.theater.push(connected=False)
        self.swell.panels[THEATER] = panel(THEATER, False)
        self.swell.push()
        merged = self.hub.panels[THEATER]
        self.assertEqual((merged["online"], merged["via"], merged["loads"][0]["available"]), (False, None, False))
        with self.assertRaisesRegex(CommandFailed, "panel_unreachable"):
            await self.hub.set_load(THEATER, "g0", on=True)
        self.assertEqual(self.kitchen.calls + self.swell.calls, [])

    async def test_connected_if_any_agent_connected(self):
        self.kitchen.push(connected=False)
        self.theater.push(connected=False)
        self.assertTrue(self.hub.connected)
        self.swell.push(connected=False)
        self.assertFalse(self.hub.connected)
        self.assertEqual(self.hub.panels, {})

    async def test_listeners_notified_on_any_agent_update(self):
        seen = []
        self.hub.add_listener(lambda: seen.append(1))
        self.swell.push()
        self.kitchen.push()
        self.assertEqual(len(seen), 2)


class StartTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_succeeds_when_only_one_agent_ready(self):
        down = FakeClient("a", KITCHEN, home_view(), connected=False, ready=False)
        up = FakeClient("b", THEATER, home_view())
        hub = BrilliantHub([down, up])
        await hub.start()
        self.assertTrue(down.started and up.started)
        self.assertEqual(set(hub.panels), {KITCHEN, THEATER, SWELL})

    async def test_start_fails_when_no_agent_ready(self):
        import custom_components.brilliant_local.hub as hub_mod

        hub_mod.READY_TIMEOUT, saved = 0.05, hub_mod.READY_TIMEOUT
        try:
            down = FakeClient("a", KITCHEN, [], connected=False, ready=False)
            hub = BrilliantHub([down])
            with self.assertRaises(CannotConnect):
                await hub.start()
            self.assertTrue(down.stopped)
        finally:
            hub_mod.READY_TIMEOUT = saved


if __name__ == "__main__":
    unittest.main()
