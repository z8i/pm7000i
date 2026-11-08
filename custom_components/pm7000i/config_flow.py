"""Config flow for PM7000i."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_USERNAME, CONF_PASSWORD

from .client import LoginError, PM7000iClient


class ConfigFlow(config_entries.ConfigFlow, domain="pm7000i"):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            client = PM7000iClient(
                user_input[CONF_HOST], user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            try:
                await self.hass.async_add_executor_job(client.validate_connection)
            except LoginError:
                errors["base"] = "invalid_auth"
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_input[CONF_HOST].strip().lower())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"PM7000i ({user_input[CONF_HOST]})", data=user_input
                )

        schema = vol.Schema({
            vol.Required(CONF_HOST): str,
            vol.Required(CONF_USERNAME, default="admin"): str,
            vol.Required(CONF_PASSWORD): str,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
