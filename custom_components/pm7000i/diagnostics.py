"""Diagnostics for PM7000i."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    coordinator = hass.data["pm7000i"][entry.entry_id]
    return {
        "host": coordinator.host,
        "available": coordinator.last_update_success,
        "sources": {
            source: len(readings)
            for source, readings in (coordinator.data or {}).items()
        },
        "device_identities": getattr(coordinator, "device_identities", {}),
    }
