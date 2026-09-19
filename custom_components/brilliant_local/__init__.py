"""Brilliantly Local: control Brilliant panels through the on-panel agent, without HomeKit or MQTT."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .client import BrilliantClient, CannotConnect
from .const import CONF_AGENTS, CONF_HOME_ID, CONF_TOKEN, DOMAIN
from .entity import panel_device_info
from .hub import BrilliantHub

PLATFORMS = [Platform.BINARY_SENSOR, Platform.LIGHT, Platform.SENSOR]

type BrilliantConfigEntry = ConfigEntry[BrilliantHub]


async def async_setup_entry(hass: HomeAssistant, entry: BrilliantConfigEntry) -> bool:
    # The entry's own agent plus any extra agents added in the options flow.
    agents = [entry.data, *entry.options.get(CONF_AGENTS, [])]
    hub = BrilliantHub([BrilliantClient(a[CONF_HOST], a[CONF_PORT], a[CONF_TOKEN]) for a in agents])
    try:
        await hub.start()
    except CannotConnect as err:
        raise ConfigEntryNotReady(str(err)) from err
    entry.runtime_data = hub

    # Entries created before multi-agent support don't record their home; backfill it
    # so the config flow can steer additional agents for the same home into options.
    if CONF_HOME_ID not in entry.data:
        home_id = next((c.hello.get("home_id") for c in hub.clients if c.hello.get("home_id")), None)
        if home_id:
            hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HOME_ID: home_id})

    # Panel devices must exist before per-load devices reference them via `via_device_id`.
    registry = dr.async_get(hass)
    registered: set[str] = set()
    # identifier -> (name, sw_version) last written, so frequent state pushes skip the registry.
    synced: dict[str, tuple[str, str | None]] = {}

    def sync_device(identifier: str, name: str, sw_version: str | None = None) -> None:
        """Follow renames (and firmware updates) made in the Brilliant app."""
        if synced.get(identifier) == (name, sw_version):
            return
        device = registry.async_get_device(identifiers={(DOMAIN, identifier)})
        if device is None:
            return  # created later by its platform with the current name
        changes: dict[str, str] = {}
        if device.name != name:
            changes["name"] = name
        if sw_version is not None and device.sw_version != sw_version:
            changes["sw_version"] = sw_version
        if changes:
            registry.async_update_device(device.id, **changes)
        synced[identifier] = (name, sw_version)

    def on_update() -> None:
        for panel in list(hub.panels.values()):
            if panel["id"] not in registered:
                registered.add(panel["id"])
                registry.async_get_or_create(config_entry_id=entry.entry_id, **panel_device_info(panel))
            sync_device(panel["id"], panel["name"], panel.get("firmware"))
            for load in panel["loads"]:
                sync_device(f"{panel['id']}_{load['id']}", load["name"])

    on_update()
    entry.async_on_unload(hub.add_listener(on_update))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BrilliantConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.stop()
    return unloaded
