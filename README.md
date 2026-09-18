<p align="center"><img src="docs/logo.png" width="280" alt="Brilliantly Local logo"></p>

# Brilliantly Local

Local Home Assistant control of Brilliant Control panels — no HomeKit, no MQTT, no cloud.

```
Home Assistant ──TCP 61172 (JSON lines, token)──▶ agent on one panel ──▶ panel message bus ──▶ every panel's loads
```

- **`agent/`** — `brilliant_local_agent.py`, a standard-library-only Python 3.10 service that runs on one
  panel. It reads the panel's internal message bus (the same API Brilliant's HomeKit bridge uses), tracks
  every panel in the home, and serves state + commands to Home Assistant.
- **`custom_components/brilliant_local/`** — the Home Assistant integration (config flow, local push).
- **`deploy/`** — install script and systemd unit for the agent.

## What you get

| Entity | Per | Notes |
|---|---|---|
| `light` | panel load | Dimmable loads get brightness; on/off loads are plain lights |
| `sensor` (W) | panel load | Live power draw |
| `binary_sensor` motion | panel | Faceplate PIR |
| `binary_sensor` connectivity | panel | Whether the agent's panel has a direct link to it (diagnostic) |

Each load is its own HA device, nested under its panel's device.

## Install

1. **Agent** (on one panel; root SSH must be enabled in the panel's settings):

   ```sh
   SSHPASS='<panel root password>' deploy/install_agent.sh <panel-ip>
   ```

   It installs to `/var/brilliant-local` (survives firmware updates), creates a random token at
   `/var/brilliant-local/token`, and runs `brilliant-local.service` capped at 64 MB / 15% CPU.

   **Optional — Wi-Fi power save** (`--wifi-powersave-off`): installs `brilliant-local-wifi.timer`,
   which keeps that panel's Wi-Fi power save off (re-applied at boot and every 5 minutes). With power
   save on, the radio dozes: on one panel, pings to the router measured 83 ms average / 1 s worst vs
   9 ms / 32 ms with it off, and links between panels suffer. It only affects the panel you run it
   against, so to apply it elsewhere run it per panel without a second agent:

   ```sh
   SSHPASS='<password>' deploy/install_agent.sh --wifi-powersave-off <agent-panel-ip>
   SSHPASS='<password>' deploy/install_agent.sh --no-agent --wifi-powersave-off <other-panel-ip>
   ```

   To undo it: `systemctl disable --now brilliant-local-wifi.timer && iw dev wlan0 set power_save on`.

   Port 61172 is used because the panel firewall only admits ports ≥ 32768; it sits above the
   ephemeral range (32768–60999) so it cannot collide with outgoing connections.

2. **Integration**: copy `custom_components/brilliant_local` into your HA `config/custom_components/`,
   restart HA, then *Settings → Devices & services → Add integration → Brilliantly Local* and enter the
   panel IP, port `61172`, and the token.

## How it works / findings

- The bus is Apache Thrift over `/var/run/brilliant/server_socket`. `get_all()` from any panel returns the
  whole home, and `request_set_variables_in_peripheral` can control **other panels' loads** too — as long as
  the agent's panel has a direct link to them.
- **Reachability** comes from the agent panel's `remote_bridge.known_remote_devices` (a thrift-encoded list of
  `{device_id, ONLINE|OFFLINE|PENDING}`). A panel that has lost its local link still reports state through the
  Brilliant cloud relay, but commands to it fail with `No path to device`. So: loads on an unlinked panel are
  unavailable in HA; its motion sensor stays available. A failed command also marks the panel unreachable
  (for 5 minutes, or until the link list changes).
- The bus notification stream can die silently; the agent rebuilds its session if its own panel pushes nothing
  for 15 minutes, and resyncs every 2 minutes.

## Agent settings (environment variables in the unit)

| Variable | Default | |
|---|---|---|
| `BL_PORT` | `61172` | Listen port |
| `BL_TOKEN_FILE` | `/var/brilliant-local/token` | |
| `BL_RETRY_AFTER` | `300` | Seconds a failed-command panel stays unavailable |
| `BL_RESYNC_EVERY` | `120` | Full state resync interval |
| `BL_STALE_AFTER` | `900` | Rebuild bus session after this long with no pushes from own panel |

## Tests

```sh
python3 agent/test_agent.py
```

Credit: bus connection recipe from [joyfulhouse/brilliant-mqtt](https://github.com/joyfulhouse/brilliant-mqtt) (MIT).

## License

MIT — see [LICENSE.md](LICENSE.md).
