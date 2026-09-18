"""Lights for Brilliantly Local."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BrilliantConfigEntry
from .client import BrilliantError
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
                if load["kind"] in ("light", "switch") and key not in known:
                    known.add(key)
                    new.append(BrilliantLight(client, panel, load["id"], panel_registry_id(hass, panel["id"])))
        if new:
            async_add_entities(new)

    add_new()
    entry.async_on_unload(client.add_listener(add_new))


class BrilliantLight(BrilliantLoadEntity, LightEntity):
    _attr_name = None

    def __init__(self, client, panel: dict[str, Any], load_id: str, via_device_id: str | None) -> None:
        super().__init__(client, panel["id"], load_id, via_device_id)
        self._attr_unique_id = f"{panel['id']}_{load_id}"

    @property
    def _dimmable(self) -> bool:
        load = self.load
        return bool(load and load["dimmable"])

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        return {ColorMode.BRIGHTNESS} if self._dimmable else {ColorMode.ONOFF}

    @property
    def color_mode(self) -> ColorMode:
        return ColorMode.BRIGHTNESS if self._dimmable else ColorMode.ONOFF

    @property
    def is_on(self) -> bool | None:
        load = self.load
        return None if load is None else load["on"]

    @property
    def brightness(self) -> int | None:
        load = self.load
        if not load or not load["dimmable"] or load["brightness"] is None:
            return None
        return max(1, round(load["brightness"] / load["max_brightness"] * 255))

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness = None
        load = self.load
        if ATTR_BRIGHTNESS in kwargs and load and load["dimmable"]:
            brightness = max(1, round(kwargs[ATTR_BRIGHTNESS] / 255 * load["max_brightness"]))
        await self._set(on=True, brightness=brightness)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(on=False)

    async def _set(self, **kwargs: Any) -> None:
        try:
            await self.client.set_load(self.panel_id, self.load_id, **kwargs)
        except BrilliantError as err:
            raise HomeAssistantError(f"Brilliant command failed: {err}") from err
