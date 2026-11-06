"""Diagnostics support for Svitlo Live."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, CONF_REGION, CONF_QUEUE

REDACT_CONFIG = {CONF_REGION, CONF_QUEUE}
REDACT_DATA = {"cpu", "name_ua", "name_en"}

async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    hub = coordinator.hub

    diagnostics_data = {
        "entry": async_redact_data(entry.as_dict(), REDACT_CONFIG),
        "coordinator_data": coordinator.data,
        "hub_stats": hub.stats(),
        "api_urls": {
            "source": coordinator.source.value if coordinator.source else None,
            "source_url": coordinator.source_url,
            "api_region_key": coordinator.api_region_key,
        }
    }

    return diagnostics_data
