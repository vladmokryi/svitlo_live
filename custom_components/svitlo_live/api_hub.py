from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Optional, Dict, List

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    API_REGION_MAP,
    NEW_API_REGIONS,
    DEFAULT_SCAN_INTERVAL,
    FAILURE_BACKOFF_SECONDS,
    REUSE_WINDOW_SECONDS,
    MIDNIGHT_REFRESH_DELAY_SECONDS,
    CATALOG_MAX_AGE_HOURS,
    Source,
    SOURCE_URLS,
    POE_URL,
    POE_UNLOADING_URL,
    POE_REGION_ID,
    POE_REGION_NAME,
    POE_QUEUES,
)
from .poe_parser import parse_poe_html, parse_poe_emergency_types
from .settings import SvitloSettings

_LOGGER = logging.getLogger(__name__)

TZ_KYIV = dt_util.get_time_zone("Europe/Kyiv")


async def async_get_hub(hass: HomeAssistant) -> "SvitloApiHub":
    """Єдиний хаб на інтеграцію (створюється за потреби, налаштування завантажуються один раз)."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    hub = domain_data.get("hub")
    if hub is None:
        hub = SvitloApiHub(hass)
        domain_data["hub"] = hub
    await hub.async_ensure_loaded()
    return hub


class SvitloSourceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Опитує одне джерело; записи (черги) лише слухають його і будують свої дані без мережі."""

    def __init__(self, hass: HomeAssistant, hub: "SvitloApiHub", source: Source, interval: timedelta) -> None:
        # Спільний координатор не належить жодному запису. Створений під час налаштування
        # запису, він би до нього прив'язався, і HA зупинив би його разом із цим записом
        token = config_entries.current_entry.set(None)
        try:
            super().__init__(
                hass,
                _LOGGER,
                name=f"{DOMAIN}_source_{source.value}",
                update_interval=interval,
            )
        finally:
            config_entries.current_entry.reset(token)
        self.hub = hub
        self.source = source
        self._unsub_midnight: Optional[Callable[[], None]] = None
        self._schedule_midnight_refresh()

    @property
    def fetched_at(self) -> Optional[datetime]:
        """Час останньої успішної перевірки джерела (200 або 304)."""
        return self.hub.fetched_at(self.source)

    async def _async_update_data(self) -> dict[str, Any]:
        data = await self.hub.async_fetch(self.source)
        if data:
            return data
        if self.data:
            # Як і раніше: при збої лишаємо попередні дані, а не робимо сутності недоступними
            _LOGGER.warning("Source %s unavailable, keeping previous data", self.source.value)
            return self.data
        raise UpdateFailed(f"No data available from {SOURCE_URLS[self.source]}")

    def _schedule_midnight_refresh(self) -> None:
        """Після півночі за Києвом "сьогодні/завтра" в даних змінюються — оновлюємось одразу."""
        tomorrow = dt_util.now(TZ_KYIV).date() + timedelta(days=1)
        refresh_at = datetime.combine(tomorrow, time(0, 0), tzinfo=TZ_KYIV) + timedelta(
            seconds=MIDNIGHT_REFRESH_DELAY_SECONDS
        )

        @callback
        def _midnight(_now: datetime) -> None:
            self._unsub_midnight = None
            self._schedule_midnight_refresh()
            self.hass.async_create_task(self.async_request_refresh())

        self._unsub_midnight = async_track_point_in_utc_time(
            self.hass, _midnight, dt_util.as_utc(refresh_at)
        )

    @callback
    def async_stop(self) -> None:
        if self._unsub_midnight:
            self._unsub_midnight()
            self._unsub_midnight = None
        # Опитування вже зупинилось із відпискою останнього слухача; shutdown додатково
        # скасовує відкладений дебаунсером запит (метод є не в усіх підтримуваних версіях HA)
        shutdown = getattr(self, "async_shutdown", None)
        if shutdown:
            self.hass.async_create_task(shutdown())


@dataclass
class _SourceState:
    """Усе, що хаб знає про одне джерело."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    data: Optional[dict[str, Any]] = None  # розпарсені дані
    fetched_at: Optional[datetime] = None  # остання успішна перевірка (200 або 304)
    failed_at: Optional[datetime] = None  # завершення останньої невдалої спроби
    coordinator: Optional[SvitloSourceCoordinator] = None
    subscribers: set[str] = field(default_factory=set)


def _sort_queues(queues: list[str]) -> list[str]:
    """Sort queues naturally (e.g., 1.1, 1.2, ..., 10.1)."""
    def _key(q: str):
        return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", q)]
    return sorted(queues, key=_key)


def _normalize_name(name: str, slug: str) -> str:
    """Ensure oblast-level regions have 'область' suffix for consistent display."""
    if slug.endswith("-oblast") and "область" not in name.lower():
        return f"{name} область"
    return name


class SvitloApiHub:
    """Єдина точка мережевих запитів: кеш даних, координатори джерел, каталог регіонів."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._session = async_get_clientsession(hass)
        self.settings = SvitloSettings(hass)
        self._load_lock = asyncio.Lock()
        self._loaded = False
        self._states: dict[Source, _SourceState] = {source: _SourceState() for source in Source}

        # HTTP Caching tags
        self._etags: dict[str, str] = {}
        self._last_modified: dict[str, str] = {}

    async def async_ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._load_lock:
            if not self._loaded:
                await self.settings.async_load()
                self._loaded = True

    # ---------- Інтервали ----------

    def _interval_for(self, source: Source) -> timedelta:
        if source is Source.POE:
            return timedelta(minutes=self.settings.poe_interval_minutes)
        return timedelta(seconds=DEFAULT_SCAN_INTERVAL)

    async def async_set_poe_interval(self, minutes: int) -> None:
        """Змінює інтервал ПОЕ для всіх записів без їх перезавантаження і без запиту."""
        if not await self.settings.async_set_poe_interval(minutes):
            return
        coordinator = self._states[Source.POE].coordinator
        if coordinator is None:
            return
        coordinator.update_interval = self._interval_for(Source.POE)
        if coordinator.data is not None:
            # Публічний спосіб перепланувати таймер від "зараз" уже з новим інтервалом
            coordinator.async_set_updated_data(coordinator.data)

    # ---------- Координатори джерел ----------

    async def async_acquire_source(self, source: Source, subscriber_id: str) -> SvitloSourceCoordinator:
        """Повертає спільний координатор джерела, створюючи його та завантажуючи дані за потреби."""
        state = self._states[source]
        state.subscribers.add(subscriber_id)
        coordinator = state.coordinator
        if coordinator is None:
            coordinator = SvitloSourceCoordinator(self.hass, self, source, self._interval_for(source))
            # Після перезавантаження запису стартуємо з уже отриманих даних: якщо оновлення
            # не вдасться, сутності покажуть їх, а не стануть недоступними
            coordinator.data = state.data
            state.coordinator = coordinator
            await coordinator.async_refresh()
        elif coordinator.data is None:
            await coordinator.async_refresh()
        return coordinator

    @callback
    def async_release_source(self, source: Source, subscriber_id: str) -> None:
        state = self._states[source]
        state.subscribers.discard(subscriber_id)
        if state.subscribers or state.coordinator is None:
            return
        state.coordinator.async_stop()
        state.coordinator = None

    async def async_resolve_source(self, region: str) -> tuple[Source, str]:
        """Визначає джерело та ключ регіону в його даних (один раз при налаштуванні запису)."""
        if region == POE_REGION_ID:
            return Source.POE, region

        catalog = await self.get_regions_catalog()
        dtek_known = self._states[Source.DTEK].data is not None
        mapped_id = API_REGION_MAP.get(region)

        for candidate in (region, mapped_id):
            if not candidate:
                continue
            info = next((r for r in catalog if r["id"] == candidate), None)
            if info is None:
                continue
            if info["is_new_api"]:
                return Source.DTEK, candidate
            if dtek_known:
                return Source.OLD, candidate
            # Без даних DTEK не можна знати, чи регіон не переїхав до нового API
            _LOGGER.debug("DTEK catalog unavailable, resolving %s by static list", region)
            break
        else:
            _LOGGER.warning(
                "Region %s not found in dynamic catalog. Available regions: %s",
                region,
                [r["id"] for r in catalog],
            )

        if region in NEW_API_REGIONS or mapped_id in NEW_API_REGIONS:
            return Source.DTEK, (mapped_id or region)
        return Source.OLD, region

    # ---------- Каталог ----------

    async def get_regions_catalog(self) -> List[Dict[str, Any]]:
        """Об'єднаний список регіонів з обох API (кешується на кілька годин)."""
        max_age = timedelta(hours=CATALOG_MAX_AGE_HOURS)
        old_data, new_data = await asyncio.gather(
            self.async_fetch(Source.OLD, max_age=max_age),
            self.async_fetch(Source.DTEK, max_age=max_age),
        )
        old_data = old_data or {}
        new_data = new_data or {}

        if new_data:
            regions_new = new_data.get("regions", [])
            _LOGGER.debug(
                "New API fetched %d regions. IDs: %s",
                len(regions_new),
                [r.get("cpu") for r in regions_new]
            )
        else:
            _LOGGER.warning("New API data is empty or None")

        merged_regions = {}

        # Parse New API (only for regions explicitly configured for New API)
        for r in new_data.get("regions", []):
            cpu = r.get("cpu")
            if not cpu: continue
            if cpu not in NEW_API_REGIONS and API_REGION_MAP.get(cpu, cpu) not in NEW_API_REGIONS:
                continue
            raw_name = r.get("name_ua") or r.get("name_en") or cpu
            merged_regions[cpu] = {
                "id": cpu,
                "name": _normalize_name(raw_name, cpu),
                "is_new_api": True,
                "queues": _sort_queues(list(r.get("schedule", {}).keys()))
            }

        # Parse Old API (fallback or unique regions)
        for r in old_data.get("regions", []):
            cpu = r.get("cpu")
            if not cpu: continue

            # Normalize slug using map if present (e.g. harkivska -> kharkivska)
            target_id = API_REGION_MAP.get(cpu, cpu)

            # Filter out null schedules (like Crimea in old API)
            schedule = r.get("schedule")
            if schedule is None: continue

            old_queues = list(schedule.keys())

            if target_id not in merged_regions:
                raw_name = r.get("name_ua") or r.get("name_en") or target_id
                merged_regions[target_id] = {
                    "id": target_id,
                    "name": _normalize_name(raw_name, target_id),
                    "is_new_api": False,
                    "queues": _sort_queues(old_queues)
                }
            else:
                # If already in merged_regions, ensure any missing queues from Old API are merged
                existing_queues = merged_regions[target_id].get("queues", [])
                if not existing_queues:
                    merged_regions[target_id]["queues"] = _sort_queues(old_queues)
                else:
                    combined = list(existing_queues)
                    for q in old_queues:
                        if q not in combined:
                            combined.append(q)
                    merged_regions[target_id]["queues"] = _sort_queues(combined)

        # ПОЕ: статичний запис, щоб конфігурація працювала навіть коли poe.pl.ua недоступний
        merged_regions[POE_REGION_ID] = {
            "id": POE_REGION_ID,
            "name": POE_REGION_NAME,
            "is_new_api": False,
            "source": Source.POE.value,
            "queues": list(POE_QUEUES),
        }

        return sorted(merged_regions.values(), key=lambda x: x["name"])

    # ---------- Завантаження ----------

    def fetched_at(self, source: Source) -> Optional[datetime]:
        return self._states[source].fetched_at

    def stats(self) -> dict[str, Any]:
        """Стан джерел для діагностики."""
        result: dict[str, Any] = {"poe_interval_minutes": self.settings.poe_interval_minutes}
        for source, state in self._states.items():
            result[source.value] = {
                "fetched_at": state.fetched_at.isoformat() if state.fetched_at else None,
                "failed_at": state.failed_at.isoformat() if state.failed_at else None,
                "polling": state.coordinator is not None,
                "interval_seconds": self._interval_for(source).total_seconds() if state.coordinator else None,
                "subscribers": len(state.subscribers),
            }
        result["etags_count"] = len(self._etags)
        result["last_modified_count"] = len(self._last_modified)
        return result

    async def async_fetch(self, source: Source, max_age: Optional[timedelta] = None) -> Optional[dict[str, Any]]:
        """Свіжі дані джерела або None, якщо отримати не вдалося (попередні дані лишаються).

        Дані, молодші за max_age (типово — хвилина), віддаються з кешу: так паралельні
        виклики та перезавантаження записів не дублюють запити.
        """
        if max_age is None:
            max_age = timedelta(seconds=REUSE_WINDOW_SECONDS)
        state = self._states[source]

        async with state.lock:
            now = dt_util.utcnow()
            if state.data and state.fetched_at and (now - state.fetched_at) < max_age:
                return state.data

            if state.failed_at and (now - state.failed_at) < timedelta(seconds=FAILURE_BACKOFF_SECONDS):
                _LOGGER.debug("Skipping %s fetch: recent failure at %s", source.value, state.failed_at)
                return None

            if source is Source.POE:
                data = await self._fetch_poe(state.data)
            else:
                data = await self._fetch_proxy(SOURCE_URLS[source], state.data)

            # Час фіксуємо після запиту: цикл повторів може тривати понад хвилину
            finished = dt_util.utcnow()
            if not data:
                state.failed_at = finished
                return None

            state.failed_at = None
            state.data = data
            state.fetched_at = finished
            return data

    async def _fetch_proxy(self, url: str, cached: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        result = await self._fetch_text(url)
        if result is None:
            return None

        status, text = result
        if status == 304:
            if cached is None:
                # Тег є, а даних немає (минулого разу не розпарсилось) — наступний запит без умов
                self._etags.pop(url, None)
                self._last_modified.pop(url, None)
            return cached

        try:
            raw = json.loads(text)
        except json.JSONDecodeError as err:
            _LOGGER.error("Failed to parse JSON from %s: %s", url, err)
            return None

        # New API Worker format: JSON загорнутий у рядок "body"
        body_str = raw.get("body") if isinstance(raw, dict) else None
        if not body_str:
            return raw
        try:
            return json.loads(body_str)
        except json.JSONDecodeError as err:
            _LOGGER.error("Failed to parse body from %s: %s", url, err)
            return None

    async def _fetch_poe(self, cached: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        """Розпарсені дані з HTML-сторінки ПОЕ (poe.pl.ua)."""
        today = dt_util.now(TZ_KYIV).date()

        # Без умовних заголовків: 304 повернув би дані, розпарсені для вчорашньої дати
        result = await self._fetch_text(POE_URL, conditional=False)
        if result is None:
            return None

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
            return None

        emergency_types = await self._fetch_poe_emergency_types(today, cached)
        region = parsed["regions"][0]
        region["emergency"] = bool(emergency_types)
        region["emergency_types"] = emergency_types
        return parsed

    async def _fetch_poe_emergency_types(self, today: date, cached: Optional[dict[str, Any]]) -> list[str]:
        """Аварійні заходи (ГАВ/СГАВ), що діють зараз за даними ПОЕ.

        Якщо запит не вдався, лишаємо попередній список за сьогодні: інакше
        разовий збій мережі "скасовував" би аварійні відключення в сенсорі.
        """
        previous: list[str] = []
        if cached and cached.get("date_today") == today.isoformat():
            regions = cached.get("regions") or [{}]
            previous = list(regions[0].get("emergency_types") or [])

        result = await self._fetch_text(POE_UNLOADING_URL, conditional=False)
        if result is None or result[1] is None:
            _LOGGER.warning("POE: unloading info unavailable, keeping emergency types %s", previous)
            return previous

        return parse_poe_emergency_types(result[1], dt_util.now(TZ_KYIV))

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
