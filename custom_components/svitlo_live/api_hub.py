from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional, Dict, List

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import (
    OLD_API_URL,
    DTEK_API_URL,
    API_REGION_MAP,
    MIN_SCAN_INTERVAL_MINUTES,
    POE_URL,
    POE_REGION_ID,
    POE_REGION_NAME,
    POE_QUEUES,
)
from .poe_parser import parse_poe_html

_LOGGER = logging.getLogger(__name__)

TZ_KYIV = dt_util.get_time_zone("Europe/Kyiv")


class SvitloApiHub:
    """Centralized hub for fetching data from both Svitlo APIs and providing dynamic catalogs."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._session = async_get_clientsession(hass)
        self._lock = asyncio.Lock()
        
        # Cache for raw data
        self._data_old: Optional[dict[str, Any]] = None
        self._data_new: Optional[dict[str, Any]] = None
        self._last_fetch_old: Optional[datetime] = None
        self._last_fetch_new: Optional[datetime] = None
        self._data_poe: Optional[dict[str, Any]] = None
        self._last_fetch_poe: Optional[datetime] = None

        # HTTP Caching tags
        self._etags: dict[str, str] = {}
        self._last_modified: dict[str, str] = {}

        # Кеш спільний для всіх записів: тримаємо його трохи коротшим за мінімальний
        # інтервал опитування, щоб кожне опитування отримувало свіжі дані
        self._cache_ttl = timedelta(minutes=MIN_SCAN_INTERVAL_MINUTES - 1)

    async def get_regions_catalog(self) -> List[Dict[str, Any]]:
        """Fetch all regions from both APIs and return a unified list."""
        old_data = await self.ensure_data(is_new=False)
        new_data = await self.ensure_data(is_new=True)

        # --- DEBUG LOGGING ---
        if new_data:
            regions_new = new_data.get("regions", [])
            _LOGGER.info(
                "New API fetched %d regions. IDs: %s", 
                len(regions_new),
                [r.get("cpu") for r in regions_new]
            )
        else:
            _LOGGER.warning("New API data is empty or None")
        # ---------------------

        merged_regions = {}

        def _normalize_name(name: str, slug: str) -> str:
            """Ensure oblast-level regions have 'область' suffix for consistent display."""
            if slug.endswith("-oblast") and "область" not in name.lower():
                return f"{name} область"
            return name

        # Parse New API (usually higher priority or has different slugs)
        for r in new_data.get("regions", []):
            cpu = r.get("cpu")
            if not cpu: continue
            raw_name = r.get("name_ua") or r.get("name_en") or cpu
            merged_regions[cpu] = {
                "id": cpu,
                "name": _normalize_name(raw_name, cpu),
                "is_new_api": True,
                "queues": list(r.get("schedule", {}).keys())
            }

        # Parse Old API (fallback or unique regions)
        for r in old_data.get("regions", []):
            cpu = r.get("cpu")
            if not cpu: continue
            
            # Normalize slug using map if present (e.g. harkivska -> kharkivska)
            target_id = API_REGION_MAP.get(cpu, cpu)

            # If already there (under new or normalized ID), we might update or ignore. 
            # Usually keep new API preference if same slug.
            if target_id not in merged_regions:
                # Filter out null schedules (like Crimea in old API)
                schedule = r.get("schedule")
                if schedule is None: continue
                
                raw_name = r.get("name_ua") or r.get("name_en") or target_id
                merged_regions[target_id] = {
                    "id": target_id,
                    "name": _normalize_name(raw_name, target_id),
                    "is_new_api": False,
                    "queues": list(schedule.keys())
                }

        # ПОЕ: статичний запис, щоб конфігурація працювала навіть коли poe.pl.ua недоступний
        merged_regions[POE_REGION_ID] = {
            "id": POE_REGION_ID,
            "name": POE_REGION_NAME,
            "is_new_api": False,
            "source": "poe",
            "queues": list(POE_QUEUES),
        }

        return sorted(merged_regions.values(), key=lambda x: x["name"])

    async def ensure_data(self, is_new: bool) -> dict[str, Any]:
        """Ensure we have fresh data for the specified API."""
        now = dt_util.utcnow()
        cache_data = self._data_new if is_new else self._data_old
        cache_time = self._last_fetch_new if is_new else self._last_fetch_old

        if cache_data and cache_time and (now - cache_time) < self._cache_ttl:
            return cache_data

        async with self._lock:
            # Double check inside lock
            cache_data = self._data_new if is_new else self._data_old
            cache_time = self._last_fetch_new if is_new else self._last_fetch_old
            if cache_data and cache_time and (now - cache_time) < self._cache_ttl:
                return cache_data

            url = DTEK_API_URL if is_new else OLD_API_URL

            result = await self._fetch_text(url)
            if result is None:
                return cache_data or {}

            status, text = result
            if status == 304:
                if is_new:
                    self._last_fetch_new = now
                else:
                    self._last_fetch_old = now
                return cache_data or {}

            try:
                raw = json.loads(text)
            except json.JSONDecodeError as err:
                _LOGGER.error("Failed to parse JSON from %s: %s", url, err)
                return cache_data or {}

            if is_new:
                # New API Worker format
                body_str = raw.get("body")
                if body_str:
                    try:
                        final_data = json.loads(body_str)
                    except json.JSONDecodeError as err:
                        _LOGGER.error("Failed to parse New API body: %s", err)
                        return cache_data or {}
                else:
                    final_data = raw

                self._data_new = final_data
                self._last_fetch_new = now
            else:
                self._data_old = raw
                self._last_fetch_old = now

            return self._data_new if is_new else self._data_old

    async def ensure_poe_data(self) -> dict[str, Any]:
        """Ensure we have fresh parsed data from the POE (poe.pl.ua) HTML page."""
        now = dt_util.utcnow()
        today = dt_util.now(TZ_KYIV).date()

        def _is_fresh() -> bool:
            # Дати "сьогодні/завтра" вшиті в розпарсені дані, тому після півночі кеш застаріває
            return bool(
                self._data_poe
                and self._last_fetch_poe
                and (now - self._last_fetch_poe) < self._cache_ttl
                and self._data_poe.get("date_today") == today.isoformat()
            )

        if _is_fresh():
            return self._data_poe

        async with self._lock:
            if _is_fresh():
                return self._data_poe

            # Без умовних заголовків: 304 повернув би дані, розпарсені для вчорашньої дати
            result = await self._fetch_text(POE_URL, conditional=False)
            if result is None:
                return self._data_poe or {}

            _status, html = result
            try:
                parsed = await self.hass.async_add_executor_job(
                    parse_poe_html,
                    html,
                    POE_REGION_ID,
                    POE_REGION_NAME,
                    today,
                    today + timedelta(days=1),
                )
            except Exception as err:
                _LOGGER.error("Failed to parse POE HTML: %s", err)
                return self._data_poe or {}

            self._data_poe = parsed
            self._last_fetch_poe = now
            return parsed

    async def _fetch_text(self, url: str, conditional: bool = True) -> Optional[tuple[int, Optional[str]]]:
        """GET url with retries.

        Returns (200, body) on success, (304, None) if not modified, None on failure.
        """
        max_retries = 3
        retry_delay = 2  # початкова затримка в секундах

        # Prepare headers for conditional request
        headers = {}
        if conditional:
            if url in self._etags:
                headers["If-None-Match"] = self._etags[url]
            if url in self._last_modified:
                headers["If-Modified-Since"] = self._last_modified[url]

        for attempt in range(max_retries + 1):
            try:
                _LOGGER.debug(
                    "Fetching API (attempt %d/%d): %s", 
                    attempt + 1, max_retries + 1, url
                )
                async with self._session.get(url, headers=headers, timeout=30) as resp:
                    # Handle 304 Not Modified
                    if resp.status == 304:
                        _LOGGER.debug("HTTP 304 Not Modified for %s", url)
                        return 304, None

                    if resp.status != 200:
                        _LOGGER.warning(
                            "HTTP %s for %s (attempt %d/%d)", 
                            resp.status, url, attempt + 1, max_retries + 1
                        )
                        if attempt < max_retries:
                            await asyncio.sleep(retry_delay)
                            retry_delay *= 2
                            continue
                        return None

                    # Update tags on 200 OK
                    if conditional:
                        if etag := resp.headers.get("ETag"):
                            self._etags[url] = etag
                        if last_mod := resp.headers.get("Last-Modified"):
                            self._last_modified[url] = last_mod

                    return 200, await resp.text()

            except Exception as e:
                _LOGGER.error(
                    "Error fetching %s (attempt %d/%d): %s", 
                    url, attempt + 1, max_retries + 1, e
                )
                if attempt < max_retries:
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                return None

        return None
