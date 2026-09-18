"""Power sensors for Brilliant Local."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BrilliantConfigEntry
from .entity import BrilliantLoadEntity, panel_registry_id


async def async_setup_entry(
    hass: HomeAssistant, entry: BrilliantConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    client = entry.runtime_data
    known: set[tuple[str, str]] = set()

    def add_new() -> None:
        new = []
        for panel in list(client.panels.values()):
            for load in panel["loads"]:
                key = (panel["id"], load["id"])
                if load["power"] is not None and key not in known:
                    known.add(key)
                    new.append(LoadPower(client, panel, load["id"], panel_registry_id(hass, panel["id"])))
        if new:
            async_add_entities(new)

    add_new()
    entry.async_on_unload(client.add_listener(add_new))


class LoadPower(BrilliantLoadEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_name = "Power"

    def __init__(self, client, panel: dict[str, Any], load_id: str, via_device_id: str | None) -> None:
        super().__init__(client, panel["id"], load_id, via_device_id)
        self._attr_unique_id = f"{panel['id']}_{load_id}_power"

    @property
    def native_value(self) -> float | None:
        load = self.load
        return None if load is None else load["power"]
