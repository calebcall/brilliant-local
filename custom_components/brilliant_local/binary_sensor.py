"""Motion and connectivity sensors for Brilliantly Local."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BrilliantConfigEntry
from .entity import BrilliantEntity, panel_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: BrilliantConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    client = entry.runtime_data
    known: set[str] = set()

    def add_new() -> None:
        new: list[BinarySensorEntity] = []
        for panel in list(client.panels.values()):
            if panel["id"] in known:
                continue
            known.add(panel["id"])
            new.append(PanelConnectivity(client, panel))
            new.extend(PanelMotion(client, panel, m["id"]) for m in panel["motion"])
        if new:
            async_add_entities(new)

    add_new()
    entry.async_on_unload(client.add_listener(add_new))


class PanelConnectivity(BrilliantEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Online"

    def __init__(self, client, panel: dict[str, Any]) -> None:
        super().__init__(client, panel["id"])
        self._attr_unique_id = f"{panel['id']}_online"
        self._attr_device_info = panel_device_info(panel)

    @property
    def available(self) -> bool:
        return self.client.connected and self.panel is not None

    @property
    def is_on(self) -> bool | None:
        panel = self.panel
        return None if panel is None else panel["online"]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        panel = self.panel
        if panel is None:
            return None
        return {
            "last_seen": panel["last_seen"],
            "via_agent": panel.get("via"),
            "reachable_via": panel.get("reachable_via"),
        }


class PanelMotion(BrilliantEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.MOTION
    _attr_name = "Motion"

    def __init__(self, client, panel: dict[str, Any], sensor_id: str) -> None:
        super().__init__(client, panel["id"])
        self.sensor_id = sensor_id
        self._attr_unique_id = f"{panel['id']}_{sensor_id}"
        self._attr_device_info = panel_device_info(panel)

    @property
    def _sensor(self) -> dict[str, Any] | None:
        panel = self.panel
        if panel is None:
            return None
        return next((m for m in panel["motion"] if m["id"] == self.sensor_id), None)

    @property
    def available(self) -> bool:
        sensor = self._sensor
        return self.client.connected and sensor is not None and sensor["available"]

    @property
    def is_on(self) -> bool | None:
        sensor = self._sensor
        return None if sensor is None else sensor["detected"]
