"""Brilliantly Local: control Brilliant panels through the on-panel agent, without HomeKit or MQTT."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .client import BrilliantClient, CannotConnect
from .const import CONF_TOKEN
from .entity import panel_device_info

PLATFORMS = [Platform.BINARY_SENSOR, Platform.LIGHT, Platform.SENSOR]

type BrilliantConfigEntry = ConfigEntry[BrilliantClient]


async def async_setup_entry(hass: HomeAssistant, entry: BrilliantConfigEntry) -> bool:
    client = BrilliantClient(entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data[CONF_TOKEN])
    try:
        await client.start()
    except CannotConnect as err:
        raise ConfigEntryNotReady(str(err)) from err
    entry.runtime_data = client

    # Panel devices must exist before per-load devices reference them via `via_device`.
    registry = dr.async_get(hass)
    registered: set[str] = set()

    def register_panels() -> None:
        for panel in list(client.panels.values()):
            if panel["id"] not in registered:
                registered.add(panel["id"])
                registry.async_get_or_create(config_entry_id=entry.entry_id, **panel_device_info(panel))

    register_panels()
    entry.async_on_unload(client.add_listener(register_panels))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BrilliantConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.stop()
    return unloaded
