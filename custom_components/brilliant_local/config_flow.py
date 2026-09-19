"""Config and options flows for Brilliantly Local."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig

from .client import BrilliantClient, CannotConnect, InvalidAuth
from .const import CONF_AGENT_PANEL, CONF_AGENTS, CONF_HOME_ID, CONF_TOKEN, DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

AGENT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_TOKEN): str,
    }
)


async def _validate(user_input: dict[str, Any], errors: dict[str, str]) -> dict[str, Any] | None:
    """Connect to an agent; return its hello message, or fill `errors`."""
    client = BrilliantClient(user_input[CONF_HOST], user_input[CONF_PORT], user_input[CONF_TOKEN])
    try:
        return await client.validate()
    except InvalidAuth:
        errors["base"] = "invalid_auth"
    except CannotConnect:
        errors["base"] = "cannot_connect"
    except Exception:
        _LOGGER.exception("Unexpected error validating Brilliant agent")
        errors["base"] = "unknown"
    return None


class BrilliantLocalConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> BrilliantLocalOptionsFlow:
        return BrilliantLocalOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and (hello := await _validate(user_input, errors)):
            home_id = hello.get("home_id")
            for entry in self._async_current_entries():
                if home_id and entry.data.get(CONF_HOME_ID) == home_id:
                    # One entry per home; more agents are added from the entry's options.
                    return self.async_abort(reason="home_already_configured")
            await self.async_set_unique_id(hello["agent_panel"])
            self._abort_if_unique_id_configured(updates={CONF_HOST: user_input[CONF_HOST]})
            return self.async_create_entry(
                title=f"Brilliantly Local ({user_input[CONF_HOST]})",
                data={**user_input, CONF_HOME_ID: home_id},
            )
        return self.async_show_form(
            step_id="user", data_schema=self.add_suggested_values_to_schema(AGENT_SCHEMA, user_input), errors=errors
        )


class BrilliantLocalOptionsFlow(OptionsFlowWithReload):
    """Add or remove extra agents (agents on other panels in the same home)."""

    @property
    def _agents(self) -> list[dict[str, Any]]:
        return list(self.config_entry.options.get(CONF_AGENTS, []))

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        menu = ["add_agent"]
        if self._agents:
            menu.append("remove_agent")
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_add_agent(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and (hello := await _validate(user_input, errors)):
            home_id = self.config_entry.data.get(CONF_HOME_ID)
            known_panels = {self.config_entry.unique_id} | {a.get(CONF_AGENT_PANEL) for a in self._agents}
            if home_id and hello.get("home_id") != home_id:
                errors["base"] = "wrong_home"
            elif hello["agent_panel"] in known_panels:
                errors["base"] = "agent_already_added"
            else:
                agent = {**user_input, CONF_AGENT_PANEL: hello["agent_panel"]}
                return self.async_create_entry(data={**self.config_entry.options, CONF_AGENTS: [*self._agents, agent]})
        return self.async_show_form(
            step_id="add_agent",
            data_schema=self.add_suggested_values_to_schema(AGENT_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_remove_agent(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            keep = [a for a in self._agents if a[CONF_HOST] not in user_input[CONF_AGENTS]]
            return self.async_create_entry(data={**self.config_entry.options, CONF_AGENTS: keep})
        hosts = [a[CONF_HOST] for a in self._agents]
        return self.async_show_form(
            step_id="remove_agent",
            data_schema=vol.Schema(
                {vol.Required(CONF_AGENTS): SelectSelector(SelectSelectorConfig(options=hosts, multiple=True))}
            ),
        )
