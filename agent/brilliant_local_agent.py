#!/usr/bin/env python3
"""Brilliant Local agent.

Runs on a Brilliant Control panel and exposes the home's panel loads over a
small newline-delimited JSON protocol on TCP, for the `brilliant_local` Home
Assistant integration. Standard library only (panel runtime is Python 3.10).

Protocol (one JSON object per line):
  server -> client  {"type": "hello", "version", "home_id", "agent_panel"}
  client -> server  {"type": "auth", "token"}
  server -> client  {"type": "snapshot", "panels": [panel, ...]}
  server -> client  {"type": "panel", "panel": panel}          # on any change
  client -> server  {"type": "set", "id", "panel", "load", "on"?, "brightness"?}
  server -> client  {"type": "result", "id", "ok", "error"?}
  client -> server  {"type": "ping"}  ->  {"type": "pong"}
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hmac
import json
import logging
import os
import struct
import sys
import time
from typing import Any, Callable, Dict, Optional

VERSION = "0.1.0"
LOG = logging.getLogger("brilliant_local")

# Message-bus enum values (thrift_types.message_bus.ttypes), live-verified.
DEVICE_TYPE_CONTROL = 1
STATUS_ONLINE = 1
PT_MOTION_SENSOR = 5
PT_LIGHT = 27
PT_OUTLET = 40
PT_GENERIC_ON_OFF = 45
PT_ALWAYS_ON = 46
LOAD_KINDS = {
    PT_LIGHT: "light",
    PT_GENERIC_ON_OFF: "switch",
    PT_OUTLET: "switch",
    PT_ALWAYS_ON: "always_on",
}
INFO_PERIPHERALS = ("device_config_peripheral", "hardware_peripheral", "remote_bridge")
# remote_bridge.DeviceStatus
PEER_ONLINE = 0

SOCKET_PATH = "/var/run/brilliant/server_socket"
EMBEDDED_ROOT = "/data/switch-embedded"
MAX_LINE = 64 * 1024
MAX_CLIENTS = 8


class PanelUnreachable(Exception):
    """The bus has no route to the target panel (it is off the network)."""


# --------------------------------------------------------------------------
# Home model (pure; unit-testable off-panel)
# --------------------------------------------------------------------------


def _tracked(pid: str, ptype: int) -> bool:
    return ptype in LOAD_KINDS or ptype == PT_MOTION_SENSOR or pid in INFO_PERIPHERALS


def _as_bool(value: Optional[str]) -> Optional[bool]:
    if value is None or value == "":
        return None
    return value not in ("0", "false", "False")


def decode_known_remote_devices(value: Optional[str]) -> Dict[str, int]:
    """Decode remote_bridge.known_remote_devices -> {device_id: DeviceStatus}.

    Thrift binary encoding of KnownRemoteDevices{1: list<RemoteDevice{
    1: string device_id, 3: i32 device_status, 4: bool always_connect}>}.
    Returns {} if the value is missing or not in the expected shape.
    """
    if not value:
        return {}
    try:
        raw = base64.b64decode(value)
        pos = 0

        def take(n: int) -> bytes:
            nonlocal pos
            if pos + n > len(raw):
                raise ValueError("truncated")
            out = raw[pos:pos + n]
            pos += n
            return out

        def skip(ftype: int) -> None:
            sizes = {2: 1, 3: 1, 4: 8, 6: 2, 8: 4, 10: 8}
            if ftype in sizes:
                take(sizes[ftype])
            elif ftype == 11:
                take(struct.unpack(">i", take(4))[0])
            else:
                raise ValueError("unsupported thrift type %d" % ftype)

        peers: Dict[str, int] = {}
        while True:
            ftype = take(1)[0]
            if ftype == 0:
                return peers
            fid = struct.unpack(">h", take(2))[0]
            if ftype != 15 or fid != 1:
                skip(ftype)
                continue
            etype, size = take(1)[0], struct.unpack(">i", take(4))[0]
            if etype != 12:
                raise ValueError("unexpected list element type")
            for _ in range(size):
                device_id, status = None, None
                while True:
                    t = take(1)[0]
                    if t == 0:
                        break
                    f = struct.unpack(">h", take(2))[0]
                    if t == 11 and f == 1:
                        device_id = take(struct.unpack(">i", take(4))[0]).decode()
                    elif t == 8 and f == 3:
                        status = struct.unpack(">i", take(4))[0]
                    else:
                        skip(t)
                if device_id is not None and status is not None:
                    peers[device_id] = status
    except (ValueError, UnicodeDecodeError, struct.error):
        LOG.warning("could not decode known_remote_devices")
        return {}


def _as_float(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


class Panel:
    def __init__(self, panel_id: str) -> None:
        self.id = panel_id
        # peripheral_id -> {"type": int, "status": int, "vars": {name: value}}
        self.peripherals: Dict[str, Dict[str, Any]] = {}
        self.last_seen = 0.0  # epoch seconds of the newest data from this panel (diagnostic)
        self.unreachable_until = 0.0  # set when a write finds no route to the panel

    def var(self, pid: str, name: str) -> Optional[str]:
        return self.peripherals.get(pid, {}).get("vars", {}).get(name)

    def to_json(self, online: bool) -> Dict[str, Any]:
        loads = []
        motion = []
        for pid, p in sorted(self.peripherals.items()):
            v = p["vars"]
            alive = p["status"] == STATUS_ONLINE
            if p["type"] in LOAD_KINDS:
                intensity = _as_float(v.get("intensity"))
                loads.append(
                    {
                        "id": pid,
                        "name": (v.get("display_name") or pid).strip(),
                        "kind": LOAD_KINDS[p["type"]],
                        "on": _as_bool(v.get("on")),
                        "dimmable": bool(_as_bool(v.get("dimmable"))),
                        "brightness": int(intensity) if intensity is not None else None,
                        "max_brightness": int(_as_float(v.get("max_intensity_value")) or 1000),
                        "power": _as_float(v.get("power")),
                        # Loads need a local route for commands; motion also arrives via the cloud.
                        "available": online and alive,
                    }
                )
            elif p["type"] == PT_MOTION_SENSOR:
                motion.append(
                    {
                        "id": pid,
                        "detected": _as_bool(v.get("movement_detected")),
                        "available": alive,
                    }
                )
        return {
            "id": self.id,
            "name": (self.var("device_config_peripheral", "device_name") or self.id[:12]).strip(),
            "firmware": self.var("hardware_peripheral", "current_release_tag"),
            "online": online,
            "last_seen": round(self.last_seen, 1),
            "loads": loads,
            "motion": motion,
        }


class HomeModel:
    """Tracked state of every panel in the home, as seen from the agent's own panel.

    Reachability: a remote panel is controllable only while the agent panel has a
    direct link to it (its own remote_bridge.known_remote_devices says ONLINE).
    Updates can still arrive through the cloud relay when that link is down, so
    freshness of data is not a reachability signal.
    """

    def __init__(self, own_id: str, retry_after: float = 300.0) -> None:
        self.own_id = own_id
        self.retry_after = retry_after
        self.panels: Dict[str, Panel] = {}
        self._peer_cache: tuple = (None, {})

    def peer_status(self) -> Dict[str, int]:
        own = self.panels.get(self.own_id)
        value = own.var("remote_bridge", "known_remote_devices") if own else None
        if value != self._peer_cache[0]:
            self._peer_cache = (value, decode_known_remote_devices(value))
        return self._peer_cache[1]

    def is_online(self, panel: Panel, now: Optional[float] = None) -> bool:
        if panel.id == self.own_id:
            return True
        now = time.time() if now is None else now
        if now < panel.unreachable_until:
            return False
        status = self.peer_status().get(panel.id)
        return status is None or status == PEER_ONLINE

    def _peers_changed(self) -> None:
        # Fresh link information supersedes a failed-write verdict.
        for panel in self.panels.values():
            panel.unreachable_until = 0.0

    def load_device(self, device: Any) -> Optional[Panel]:
        """Replace a panel's tracked state from a full bus `Device`."""
        if device.device_type != DEVICE_TYPE_CONTROL:
            return None
        panel = self.panels.setdefault(device.id, Panel(device.id))
        newest = 0
        tracked = {}
        for pid, p in (device.peripherals or {}).items():
            variables = p.variables or {}
            newest = max([newest] + [v.timestamp or 0 for v in variables.values()])
            if _tracked(pid, p.peripheral_type):
                tracked[pid] = {
                    "type": p.peripheral_type,
                    "status": p.status,
                    "vars": {name: v.value for name, v in variables.items()},
                }
        old_peers = panel.var("remote_bridge", "known_remote_devices")
        panel.peripherals = tracked
        if panel.id == self.own_id and panel.var("remote_bridge", "known_remote_devices") != old_peers:
            self._peers_changed()
        panel.last_seen = max(panel.last_seen, newest / 1000.0)
        return panel

    def apply_notification(self, notification: Any, now: Optional[float] = None) -> Optional[Panel]:
        """Apply a SubscriptionNotification delta. Returns the panel if tracked."""
        device = notification.updated_device
        panel = self.panels.get(device.id) if device is not None else None
        if panel is None:
            return None
        panel.last_seen = time.time() if now is None else now
        for mp in notification.modified_peripherals or []:
            p = panel.peripherals.get(mp.peripheral_id)
            if p is None:
                continue
            if mp.status is not None:
                p["status"] = mp.status
            for mv in mp.modified_variables or []:
                if mv.variable is not None:
                    p["vars"][mv.variable_name] = mv.variable.value
                    if panel.id == self.own_id and mv.variable_name == "known_remote_devices":
                        self._peers_changed()
        return panel

    def mark_unreachable(self, panel: Panel, now: Optional[float] = None) -> None:
        panel.unreachable_until = (time.time() if now is None else now) + self.retry_after

    def apply_set(self, panel_id: str, pid: str, variables: Dict[str, str]) -> None:
        p = self.panels.get(panel_id, Panel(panel_id)).peripherals.get(pid)
        if p is not None:
            p["vars"].update(variables)

    def snapshot(self) -> list:
        return [p.to_json(self.is_online(p)) for p in self.panels.values()]


def build_set_variables(panel: Panel, pid: str, cmd: Dict[str, Any]) -> Dict[str, str]:
    """Translate a client `set` command into bus variables (all string-valued)."""
    p = panel.peripherals.get(pid)
    if p is None or p["type"] not in LOAD_KINDS or p["type"] == PT_ALWAYS_ON:
        raise ValueError("unknown or non-controllable load")
    variables: Dict[str, str] = {}
    if cmd.get("brightness") is not None:
        top = int(_as_float(p["vars"].get("max_intensity_value")) or 1000)
        variables["intensity"] = str(max(1, min(top, int(cmd["brightness"]))))
    if cmd.get("on") is not None:
        variables["on"] = "1" if cmd["on"] else "0"
    if not variables:
        raise ValueError("nothing to set")
    return variables


# --------------------------------------------------------------------------
# Message-bus session (panel-only imports live here)
# --------------------------------------------------------------------------


class BusSession:
    def __init__(self, on_notification: Callable[[Any], None]) -> None:
        self._on_notification = on_notification
        self._proc: Any = None
        self._obs: Any = None
        self.own_id = ""
        self.home_id = ""

    async def start(self) -> None:
        if EMBEDDED_ROOT not in sys.path:
            sys.path.insert(0, EMBEDDED_ROOT)
        import lib.protocol.message_bus_peer_service as mbps
        from lib.message_bus_api.observer_interface import RPCObserver
        from lib.protocol.processor import SinglePeerProcessor

        on_notification = self._on_notification

        class Observer(RPCObserver):
            # The bus awaits this and passes `notification` by keyword.
            async def handle_notification(self, notification):
                on_notification(notification)

        loop = asyncio.get_running_loop()
        self._obs = Observer(loop)
        self._proc = SinglePeerProcessor(
            socket_path=SOCKET_PATH,
            my_name="brilliant_local_agent",
            handler=mbps.PeripheralServer(self._obs),
            client_class=mbps.MessageBusClient,
            loop=loop,
        )
        await self._proc.start()
        for _ in range(100):
            if self._proc.is_connected():
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("message bus did not connect")
        await self._obs.start(self._proc, None)
        self.own_id = self._obs.get_owning_device_id()
        self.home_id = self._obs.get_home_id() or ""

    def is_connected(self) -> bool:
        return bool(self._proc and self._proc.is_connected())

    async def get_all(self) -> Any:
        return await self._obs.get_all()

    async def subscribe(self, device_id: str) -> None:
        from thrift_types.message_bus.ttypes import SubscriptionRequest

        await self._obs.subscribe(
            SubscriptionRequest(device_id=device_id), callback_func=None, forward_to_message_bus=True
        )

    async def set_variables(self, device_id: str, pid: str, variables: Dict[str, str]) -> Dict[str, str]:
        from lib.exceptions import NoConnectionError

        try:
            resp = await self._obs.request_set_variables_in_peripheral(pid, variables, device_id=device_id)
        except NoConnectionError as err:
            raise PanelUnreachable(str(err)) from err
        return {
            mv.variable_name: mv.variable.value
            for mv in (getattr(resp, "modified_variables", None) or [])
            if mv.variable is not None
        }

    async def close(self) -> None:
        for closer in (getattr(self._obs, "shutdown", None), getattr(self._proc, "shutdown", None)):
            if closer is None:
                continue
            try:
                await asyncio.wait_for(closer(), 5)
            except Exception:  # best effort teardown
                LOG.debug("teardown error", exc_info=True)


# --------------------------------------------------------------------------
# Agent: owns the bus session, the model, and the TCP clients
# --------------------------------------------------------------------------


class Agent:
    def __init__(self, token: str, retry_after: float, resync_every: float, stale_after: float,
                 session_factory: Callable[..., Any] = BusSession) -> None:
        self.token = token
        self.retry_after = retry_after
        self.resync_every = resync_every
        self.stale_after = stale_after
        self.session_factory = session_factory
        self.session: Any = None
        self.model: Optional[HomeModel] = None
        self.clients: Dict[int, asyncio.Queue] = {}
        self._dirty: set = set()
        self._flush_handle: Optional[asyncio.TimerHandle] = None
        self._last_push = 0.0
        self._online: Dict[str, bool] = {}
        self.ready = asyncio.Event()
        self._tasks: set = set()

    # ---- bus side -------------------------------------------------------

    async def run_bus(self) -> None:
        backoff = 2.0
        while True:
            self.session = self.session_factory(self._handle_notification)
            try:
                await self._run_session()
            except asyncio.CancelledError:
                await self.session.close()
                raise
            except Exception:
                LOG.exception("bus session failed; rebuilding in %.0fs", backoff)
            self.ready.clear()
            await self.session.close()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _run_session(self) -> None:
        s = self.session
        await s.start()
        model = HomeModel(s.own_id, self.retry_after)
        if self.model is not None:  # keep reachability history across rebuilds
            for pid, old in self.model.panels.items():
                model.panels[pid] = old
        self.model = model
        await self._resync()
        for panel_id in list(model.panels):
            await s.subscribe(panel_id)
        self._last_push = time.monotonic()
        self.ready.set()
        LOG.info("bus session up: own=%s panels=%d", s.own_id, len(model.panels))
        self._dirty.clear()  # the snapshot below already carries every panel
        self._broadcast_snapshot()
        last_resync = time.monotonic()
        while True:
            await asyncio.sleep(5)
            if not s.is_connected():
                raise RuntimeError("bus disconnected")
            if time.monotonic() - self._last_push > self.stale_after:
                raise RuntimeError("no bus notifications for %.0fs; stream presumed dead" % self.stale_after)
            if time.monotonic() - last_resync > self.resync_every:
                await self._resync()
                last_resync = time.monotonic()
            self._check_online()

    async def _resync(self) -> None:
        assert self.model is not None
        devices = await self.session.get_all()
        for device in devices.devices:
            panel = self.model.load_device(device)
            if panel is not None:
                self._mark_dirty(panel.id)

    def _handle_notification(self, notification: Any) -> None:
        if self.model is None:
            return
        panel = self.model.apply_notification(notification)
        if panel is not None:
            if panel.id == self.model.own_id:
                self._last_push = time.monotonic()
            self._mark_dirty(panel.id)
            self._check_online()

    def _check_online(self) -> None:
        assert self.model is not None
        for panel in self.model.panels.values():
            online = self.model.is_online(panel)
            if self._online.get(panel.id) != online:
                if panel.id in self._online:
                    LOG.info("panel %s is now %s", panel.id[:12], "online" if online else "offline")
                self._online[panel.id] = online
                self._mark_dirty(panel.id)

    async def set_load(self, cmd: Dict[str, Any]) -> None:
        if self.model is None or not self.ready.is_set():
            raise RuntimeError("bus not ready")
        panel = self.model.panels.get(str(cmd.get("panel")))
        if panel is None:
            raise ValueError("unknown panel")
        pid = str(cmd.get("load"))
        variables = build_set_variables(panel, pid, cmd)
        try:
            applied = await asyncio.wait_for(self.session.set_variables(panel.id, pid, variables), 10)
        except PanelUnreachable:
            self.model.mark_unreachable(panel)
            self._check_online()
            raise
        self.model.apply_set(panel.id, pid, applied or variables)
        self._mark_dirty(panel.id)

    # ---- client side ----------------------------------------------------

    def _mark_dirty(self, panel_id: str) -> None:
        self._dirty.add(panel_id)
        if self._flush_handle is None:
            self._flush_handle = asyncio.get_running_loop().call_later(0.05, self._flush)

    def _flush(self) -> None:
        self._flush_handle = None
        if self.model is None:
            return
        for panel_id in self._dirty:
            panel = self.model.panels.get(panel_id)
            if panel is not None:
                self._broadcast({"type": "panel", "panel": panel.to_json(self.model.is_online(panel))})
        self._dirty.clear()

    def _broadcast_snapshot(self) -> None:
        if self.model is not None:
            self._broadcast({"type": "snapshot", "panels": self.model.snapshot()})

    def _broadcast(self, msg: Dict[str, Any]) -> None:
        for q in self.clients.values():
            if q.qsize() < 1000:
                q.put_nowait(msg)

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if len(self.clients) >= MAX_CLIENTS:
            writer.close()
            return
        queue: asyncio.Queue = asyncio.Queue()
        sender: Optional[asyncio.Task] = None
        try:
            await self._send(writer, {
                "type": "hello",
                "version": VERSION,
                "home_id": getattr(self.session, "home_id", ""),
                "agent_panel": getattr(self.session, "own_id", ""),
            })
            line = await asyncio.wait_for(reader.readline(), 10)
            auth = json.loads(line or b"{}")
            if auth.get("type") != "auth" or not hmac.compare_digest(str(auth.get("token", "")), self.token):
                await self._send(writer, {"type": "error", "error": "auth_failed"})
                LOG.warning("auth failed from %s", peer)
                return
            LOG.info("client connected: %s", peer)
            self.clients[id(queue)] = queue
            if self.model is not None and self.ready.is_set():
                queue.put_nowait({"type": "snapshot", "panels": self.model.snapshot()})
            sender = asyncio.create_task(self._pump(queue, writer))
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("type") == "ping":
                    queue.put_nowait({"type": "pong"})
                elif msg.get("type") == "set":
                    task = asyncio.create_task(self._run_set(msg, queue))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                elif msg.get("type") == "snapshot" and self.model is not None:
                    queue.put_nowait({"type": "snapshot", "panels": self.model.snapshot()})
        except (asyncio.TimeoutError, ConnectionError, ValueError, asyncio.LimitOverrunError):
            pass
        finally:
            self.clients.pop(id(queue), None)
            if sender is not None:
                sender.cancel()
            writer.close()
            LOG.info("client disconnected: %s", peer)

    async def _run_set(self, msg: Dict[str, Any], queue: asyncio.Queue) -> None:
        result: Dict[str, Any] = {"type": "result", "id": msg.get("id"), "ok": True}
        try:
            await self.set_load(msg)
        except PanelUnreachable:
            result.update(ok=False, error="panel_unreachable")
        except asyncio.TimeoutError:
            result.update(ok=False, error="timeout")
        except Exception as err:  # report every failure to the caller
            result.update(ok=False, error=str(err) or type(err).__name__)
        queue.put_nowait(result)

    async def _pump(self, queue: asyncio.Queue, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                await self._send(writer, await queue.get())
        except (ConnectionError, asyncio.CancelledError):
            writer.close()

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, msg: Dict[str, Any]) -> None:
        writer.write(json.dumps(msg, separators=(",", ":")).encode() + b"\n")
        await writer.drain()


async def main_async(args: argparse.Namespace) -> None:
    with open(args.token_file) as f:
        token = f.read().strip()
    if len(token) < 16:
        raise SystemExit("token in %s is too short" % args.token_file)
    agent = Agent(token, args.retry_after, args.resync_every, args.stale_after)
    server = await asyncio.start_server(agent.handle_client, args.bind, args.port, limit=MAX_LINE)
    LOG.info("brilliant_local agent %s listening on %s:%d", VERSION, args.bind, args.port)
    async with server:
        await asyncio.gather(server.serve_forever(), agent.run_bus())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bind", default=os.environ.get("BL_BIND", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BL_PORT", "61172")))
    parser.add_argument("--token-file", default=os.environ.get("BL_TOKEN_FILE", "/var/brilliant-local/token"))
    parser.add_argument("--retry-after", type=float, default=float(os.environ.get("BL_RETRY_AFTER", "300")))
    parser.add_argument("--resync-every", type=float, default=float(os.environ.get("BL_RESYNC_EVERY", "120")))
    parser.add_argument("--stale-after", type=float, default=float(os.environ.get("BL_STALE_AFTER", "900")))
    parser.add_argument("--log-level", default=os.environ.get("BL_LOG_LEVEL", "INFO"))
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
