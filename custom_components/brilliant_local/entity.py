"""Base entities for Brilliant Local."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .client import BrilliantClient
from .const import DOMAIN


def panel_device_info(panel: dict[str, Any]) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, panel["id"])},
        name=panel["name"],
        manufacturer="Brilliant",
        model=f"Brilliant Control ({len(panel['loads'])}-gang)",
        sw_version=panel.get("firmware"),
    )


def panel_registry_id(hass: HomeAssistant, panel_id: str) -> str | None:
    """Device-registry id of a panel (registered in async_setup_entry before any platform)."""
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, panel_id)})
    return device.id if device else None


class BrilliantEntity(Entity):
    """An entity whose state lives in the client's panel mirror."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, client: BrilliantClient, panel_id: str) -> None:
        self.client = client
        self.panel_id = panel_id

    @property
    def panel(self) -> dict[str, Any] | None:
        return self.client.panels.get(self.panel_id)

    @property
    def available(self) -> bool:
        return self.client.connected and self.panel is not None and self.panel["online"]

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.client.add_listener(self.async_write_ha_state))


class BrilliantLoadEntity(BrilliantEntity):
    """An entity attached to one load (gang); each load is its own device under its panel."""

    def __init__(self, client: BrilliantClient, panel_id: str, load_id: str, via_device_id: str | None) -> None:
        super().__init__(client, panel_id)
        self.load_id = load_id
        load = self.load or {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{panel_id}_{load_id}")},
            name=load.get("name", load_id),
            manufacturer="Brilliant",
            model="Brilliant Control load",
        )
        if via_device_id is not None:
            self._attr_device_info["via_device_id"] = via_device_id

    @property
    def load(self) -> dict[str, Any] | None:
        return self.client.load(self.panel_id, self.load_id)

    @property
    def available(self) -> bool:
        load = self.load
        return self.client.connected and load is not None and load["available"]
