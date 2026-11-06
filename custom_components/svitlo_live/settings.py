"""Спільні налаштування інтеграції, що не належать жодному запису (черзі)."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    CONF_REGION,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
    MAX_SCAN_INTERVAL_MINUTES,
    POE_REGION_ID,
    STORAGE_KEY,
    STORAGE_VERSION,
    STORE_POE_INTERVAL,
)


def clamp_poe_interval(minutes: int) -> int:
    return max(MIN_SCAN_INTERVAL_MINUTES, min(MAX_SCAN_INTERVAL_MINUTES, int(minutes)))


class SvitloSettings:
    """Інтервал опитування ПОЕ — один на всі записи ПОЕ, зберігається в .storage."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._poe_interval_minutes = DEFAULT_SCAN_INTERVAL_MINUTES

    @property
    def poe_interval_minutes(self) -> int:
        return self._poe_interval_minutes

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        minutes = stored.get(STORE_POE_INTERVAL)
        if minutes is None:
            minutes = self._shortest_legacy_poe_interval()
            await self._store.async_save({STORE_POE_INTERVAL: minutes})
        self._poe_interval_minutes = clamp_poe_interval(minutes)

    async def async_set_poe_interval(self, minutes: int) -> bool:
        """Зберігає новий інтервал; повертає True, якщо він змінився."""
        minutes = clamp_poe_interval(minutes)
        if minutes == self._poe_interval_minutes:
            return False
        self._poe_interval_minutes = minutes
        await self._store.async_save({STORE_POE_INTERVAL: minutes})
        return True

    def _shortest_legacy_poe_interval(self) -> int:
        """Міграція: раніше інтервал задавався в кожному записі — беремо найкоротший серед ПОЕ."""
        legacy = [
            int(entry.options[CONF_SCAN_INTERVAL])
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.data.get(CONF_REGION) == POE_REGION_ID and CONF_SCAN_INTERVAL in entry.options
        ]
        return min(legacy) if legacy else DEFAULT_SCAN_INTERVAL_MINUTES
