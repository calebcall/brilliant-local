"""Off-panel tests for the agent: model logic plus the TCP protocol over a fake bus."""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).parent))
import brilliant_local_agent as agent_mod  # noqa: E402
from brilliant_local_agent import (  # noqa: E402
    Agent,
    HomeModel,
    PanelUnreachable,
    build_set_variables,
    decode_known_remote_devices,
)

OWN = "own0000000000000000000000000000a"
REMOTE = "remote00000000000000000000000000b"
TOKEN = "t" * 32


def var(value, ts=1_000_000):
    return NS(value=value, timestamp=ts)


def light(name, on="0", intensity="500", dimmable="1", ts=1_000_000):
    return NS(
        peripheral_type=27,
        status=1,
        variables={
            "on": var(on, ts),
            "intensity": var(intensity, ts),
            "max_intensity_value": var("1000", ts),
            "dimmable": var(dimmable, ts),
            "display_name": var(name, ts),
            "power": var("0", ts),
        },
    )


def known_remote_devices(peers):
    """Thrift-binary KnownRemoteDevices, as the panel encodes it."""
    out = b"\x0f\x00\x01\x0c" + struct.pack(">i", len(peers))
    for dev_id, status in peers.items():
        raw = dev_id.encode()
        out += b"\x0b\x00\x01" + struct.pack(">i", len(raw)) + raw
        out += b"\x08\x00\x03" + struct.pack(">i", status)
        out += b"\x02\x00\x04\x00\x00"
    return base64.b64encode(out + b"\x00").decode()


def control(dev_id, name, ts=1_000_000, peers=None, **loads):
    peripherals = {
        "device_config_peripheral": NS(peripheral_type=0, status=1, variables={"device_name": var(name + " ", ts)}),
        "hardware_peripheral": NS(peripheral_type=22, status=1, variables={"current_release_tag": var("v1", ts)}),
        "faceplate_peripheral": NS(peripheral_type=5, status=1, variables={"movement_detected": var("0", ts)}),
        "ui": NS(peripheral_type=12, status=1, variables={"x": var("1", ts)}),
    }
    if peers is not None:
        peripherals["remote_bridge"] = NS(
            peripheral_type=0, status=1, variables={"known_remote_devices": var(known_remote_devices(peers), ts)}
        )
    peripherals.update(loads)
    return NS(id=dev_id, device_type=1, peripherals=peripherals)


def notification(dev_id, pid, **values):
    return NS(
        updated_device=NS(id=dev_id),
        modified_peripherals=[
            NS(
                peripheral_id=pid,
                status=None,
                modified_variables=[NS(variable_name=k, variable=NS(value=v)) for k, v in values.items()],
            )
        ],
    )


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.model = HomeModel(OWN, retry_after=300)
        self.model.load_device(control(OWN, "Kitchen", peers={REMOTE: 0, "cloud": 0},
                                       gangbox_peripheral_0=light("Walkway", dimmable="0")))
        self.model.load_device(control(REMOTE, "Theater", ts=1_000_000, gangbox_peripheral_0=light("Rear")))
        self.model.load_device(NS(id="cloud", device_type=4, peripherals={}))

    def test_only_control_devices_and_relevant_peripherals(self):
        self.assertEqual(set(self.model.panels), {OWN, REMOTE})
        self.assertNotIn("ui", self.model.panels[OWN].peripherals)

    def test_panel_json(self):
        j = self.model.panels[OWN].to_json(True)
        self.assertEqual(j["name"], "Kitchen")
        self.assertEqual(j["firmware"], "v1")
        self.assertEqual(
            j["loads"][0],
            {"id": "gangbox_peripheral_0", "name": "Walkway", "kind": "light", "on": False, "dimmable": False,
             "brightness": 500, "max_brightness": 1000, "power": 0.0, "available": True},
        )
        self.assertEqual(j["motion"][0]["detected"], False)

    def test_decode_known_remote_devices(self):
        blob = known_remote_devices({"panel_aa": 1, "panel_bb": 0, "cloud": 0})
        self.assertEqual(decode_known_remote_devices(blob), {"panel_aa": 1, "panel_bb": 0, "cloud": 0})
        self.assertEqual(decode_known_remote_devices("not base64!!"), {})
        self.assertEqual(decode_known_remote_devices(blob[:12]), {})
        self.assertEqual(decode_known_remote_devices(None), {})

    def test_reachability_follows_own_peer_links(self):
        remote = self.model.panels[REMOTE]
        self.assertTrue(self.model.is_online(remote))
        self.model.apply_notification(notification(OWN, "remote_bridge",
                                                   known_remote_devices=known_remote_devices({REMOTE: 1})))
        self.assertFalse(self.model.is_online(remote))
        self.assertFalse(remote.to_json(False)["loads"][0]["available"])
        # Motion still arrives via the cloud relay, so it stays available.
        self.assertTrue(remote.to_json(False)["motion"][0]["available"])
        self.assertTrue(self.model.is_online(self.model.panels[OWN]))

    def test_failed_write_marks_unreachable_until_links_change_or_retry(self):
        remote = self.model.panels[REMOTE]
        self.model.mark_unreachable(remote, now=1_000)
        self.assertFalse(self.model.is_online(remote, now=1_100))
        self.assertTrue(self.model.is_online(remote, now=1_301))
        self.model.mark_unreachable(remote)
        self.assertFalse(self.model.is_online(remote))
        self.model.apply_notification(notification(OWN, "remote_bridge",
                                                   known_remote_devices=known_remote_devices({REMOTE: 0})))
        self.assertTrue(self.model.is_online(remote))

    def test_data_from_remote_does_not_imply_reachable(self):
        remote = self.model.panels[REMOTE]
        self.model.mark_unreachable(remote)
        self.model.apply_notification(notification(REMOTE, "gangbox_peripheral_0", power="5"))
        self.assertFalse(self.model.is_online(remote))

    def test_notification_updates_state(self):
        panel = self.model.panels[REMOTE]
        self.model.apply_notification(notification(REMOTE, "gangbox_peripheral_0", on="1", power="223"), now=5_000)
        self.assertEqual(panel.var("gangbox_peripheral_0", "on"), "1")
        self.assertEqual(panel.var("gangbox_peripheral_0", "power"), "223")
        self.assertEqual(panel.last_seen, 5_000)

    def test_resync_does_not_regress_last_seen(self):
        self.model.apply_notification(notification(REMOTE, "gangbox_peripheral_0", on="1"), now=9_000)
        self.model.load_device(control(REMOTE, "Theater", gangbox_peripheral_0=light("Rear")))
        self.assertEqual(self.model.panels[REMOTE].last_seen, 9_000)

    def test_build_set_variables(self):
        panel = self.model.panels[REMOTE]
        self.assertEqual(build_set_variables(panel, "gangbox_peripheral_0", {"on": True}), {"on": "1"})
        self.assertEqual(
            build_set_variables(panel, "gangbox_peripheral_0", {"on": True, "brightness": 5000}),
            {"intensity": "1000", "on": "1"},
        )
        self.assertEqual(build_set_variables(panel, "gangbox_peripheral_0", {"brightness": 0}), {"intensity": "1"})
        with self.assertRaises(ValueError):
            build_set_variables(panel, "faceplate_peripheral", {"on": True})
        with self.assertRaises(ValueError):
            build_set_variables(panel, "gangbox_peripheral_0", {})


class FakeSession:
    instances: list = []

    def __init__(self, on_notification):
        self.on_notification = on_notification
        self.own_id = OWN
        self.home_id = "home1"
        self.writes = []
        self.unreachable = set()
        FakeSession.instances.append(self)

    async def start(self):
        pass

    def is_connected(self):
        return True

    async def get_all(self):
        return NS(devices=[
            control(OWN, "Kitchen", ts=10**13, gangbox_peripheral_0=light("Walkway")),
            control(REMOTE, "Theater", ts=10**13, gangbox_peripheral_0=light("Rear")),
        ])

    async def subscribe(self, device_id):
        pass

    async def set_variables(self, device_id, pid, variables):
        if device_id in self.unreachable:
            raise PanelUnreachable("No path to device")
        self.writes.append((device_id, pid, variables))
        return variables

    async def close(self):
        pass


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeSession.instances.clear()
        self.agent = Agent(TOKEN, retry_after=300, resync_every=120, stale_after=900, session_factory=FakeSession)
        self.bus = asyncio.create_task(self.agent.run_bus())
        await asyncio.wait_for(self.agent.ready.wait(), 2)
        self.server = await asyncio.start_server(self.agent.handle_client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.bus.cancel()
        self.server.close()
        await self.server.wait_closed()

    async def connect(self, token=TOKEN):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        hello = json.loads(await reader.readline())
        writer.write(json.dumps({"type": "auth", "token": token}).encode() + b"\n")
        return hello, reader, writer

    async def recv(self, reader, type_, panel_id=None):
        while True:
            msg = json.loads(await asyncio.wait_for(reader.readline(), 2))
            if msg["type"] == type_ and (panel_id is None or msg["panel"]["id"] == panel_id):
                return msg

    async def test_bad_token_rejected(self):
        _, reader, writer = await self.connect("wrong")
        self.assertEqual(json.loads(await reader.readline())["error"], "auth_failed")
        writer.close()

    async def test_snapshot_set_and_push(self):
        hello, reader, writer = await self.connect()
        self.assertEqual(hello["agent_panel"], OWN)
        snap = await self.recv(reader, "snapshot")
        self.assertEqual({p["name"] for p in snap["panels"]}, {"Kitchen", "Theater"})

        writer.write(json.dumps({"type": "set", "id": 1, "panel": REMOTE, "load": "gangbox_peripheral_0",
                                 "on": True, "brightness": 300}).encode() + b"\n")
        self.assertEqual(await self.recv(reader, "result"), {"type": "result", "id": 1, "ok": True})
        self.assertEqual(FakeSession.instances[-1].writes,
                         [(REMOTE, "gangbox_peripheral_0", {"intensity": "300", "on": "1"})])
        panel = (await self.recv(reader, "panel", REMOTE))["panel"]
        self.assertEqual(panel["loads"][0]["on"], True)

        FakeSession.instances[-1].on_notification(notification(OWN, "gangbox_peripheral_0", power="810"))
        panel = (await self.recv(reader, "panel", OWN))["panel"]
        self.assertEqual((panel["id"], panel["loads"][0]["power"]), (OWN, 810.0))
        writer.close()

    async def test_unreachable_panel_goes_offline(self):
        _, reader, writer = await self.connect()
        await self.recv(reader, "snapshot")
        FakeSession.instances[-1].unreachable.add(REMOTE)
        writer.write(json.dumps({"type": "set", "id": 7, "panel": REMOTE, "load": "gangbox_peripheral_0",
                                 "on": False}).encode() + b"\n")
        result = await self.recv(reader, "result")
        self.assertEqual((result["ok"], result["error"]), (False, "panel_unreachable"))
        panel = (await self.recv(reader, "panel", REMOTE))["panel"]
        self.assertEqual((panel["id"], panel["online"], panel["loads"][0]["available"]), (REMOTE, False, False))
        writer.close()

    async def test_ping(self):
        _, reader, writer = await self.connect()
        writer.write(b'{"type":"ping"}\n')
        await self.recv(reader, "pong")
        writer.close()


if __name__ == "__main__":
    agent_mod.logging.basicConfig(level="WARNING")
    unittest.main()
