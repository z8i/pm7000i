"""Home Assistant setup for PM7000i."""
from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_USERNAME, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import PM7000iClient

_LOGGER = logging.getLogger(__name__)
DOMAIN = "pm7000i"
PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = PM7000iClient(
        entry.data[CONF_HOST], entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
    )
    coordinator = PM7000iCoordinator(hass, entry, client)
    try:
        await coordinator.async_config_entry_first_refresh()
    except UpdateFailed as err:
        raise ConfigEntryNotReady from err
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


class PM7000iCoordinator(DataUpdateCoordinator):
    """Poller; all blocking device I/O is delegated to PM7000iClient."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: PM7000iClient):
        self.client = client
        self.host = entry.data[CONF_HOST]
        super().__init__(
            hass,
            _LOGGER,
            name=f"PM7000i {self.host}",
            update_interval=timedelta(seconds=10),
        )

    async def _async_update_data(self):
        try:
            payload = await self.hass.async_add_executor_job(self.client.fetch_data)
        except Exception as err:
            raise UpdateFailed(f"PM7000i communication failed: {err}") from err
        self.configs_by_source = payload["configs_by_source"]
        self.su100_units = payload["su100_units"]
        self.device_identities = payload["device_identities"]
        return payload["readings"]
