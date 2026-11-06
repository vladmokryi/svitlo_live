from __future__ import annotations

import logging
from datetime import datetime, timedelta, date, time
from typing import Any, Optional, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_REGION,
    CONF_QUEUE,
    STALE_REFRESH_MINUTES,
    Source,
    SOURCE_URLS,
)
from .api_hub import SvitloApiHub, SvitloSourceCoordinator

_LOGGER = logging.getLogger(__name__)

TZ_KYIV = dt_util.get_time_zone("Europe/Kyiv")


class SvitloCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Дані конкретного region/queue.

    Сам у мережу не ходить: слухає спільний координатор свого джерела і перебудовує
    дані з його JSON. Власне оновлення (точний тік на зміну статусу, ручне оновлення)
    перераховує стан локально і лише за застарілих даних просить джерело оновитися.
    """

    def __init__(self, hass: HomeAssistant, config: dict[str, Any], hub: SvitloApiHub, subscriber_id: str) -> None:
        self.hass = hass
        self.hub = hub
        self.region: str = config[CONF_REGION]
        self.queue: str = config[CONF_QUEUE]
        self._subscriber_id = subscriber_id

        # Визначається один раз в async_attach
        self.source: Optional[Source] = None
        self.source_url: str = ""
        self.api_region_key = self.region
        self._source_coordinator: Optional[SvitloSourceCoordinator] = None
        self._unsub_source: Optional[Callable[[], None]] = None

        self._history_today: list[list[str]] = []
        self._history_tomorrow: list[list[str]] = []

        self._unsub_precise: Optional[Callable[[], None]] = None

        super().__init__(
            hass=hass,
            logger=_LOGGER,
            name=f"svitlo_live_{self.region}_{self.queue}",
            update_interval=None,  # опитує координатор джерела
        )

    async def async_attach(self) -> None:
        """Визначає джерело та підписується на його спільний координатор."""
        self.source, self.api_region_key = await self.hub.async_resolve_source(self.region)
        self.source_url = SOURCE_URLS[self.source]
        self._source_coordinator = await self.hub.async_acquire_source(self.source, self._subscriber_id)
        self._unsub_source = self._source_coordinator.async_add_listener(self._handle_source_update)

    @callback
    def async_detach(self) -> None:
        if self._unsub_precise:
            self._unsub_precise()
            self._unsub_precise = None
        if self._unsub_source:
            self._unsub_source()
            self._unsub_source = None
        if self._source_coordinator is not None:
            self.hub.async_release_source(self.source, self._subscriber_id)
            self._source_coordinator = None

    @callback
    def _handle_source_update(self) -> None:
        """Джерело отримало нові дані (або помилку) — перебудовуємо дані черги."""
        try:
            payload = self._build_payload()
        except UpdateFailed as err:
            self.async_set_update_error(err)
            return
        self.async_set_updated_data(payload)

    async def _async_update_data(self) -> dict[str, Any]:
        """Точний тік на зміну статусу або ручне оновлення сутності.

        Стан перераховується одразу з наявних даних (статус перемкнеться вчасно навіть
        без мережі), а якщо дані джерела застарілі — джерело просимо оновитися.
        """
        payload = self._build_payload()
        self._request_source_refresh_if_stale()
        return payload

    def _request_source_refresh_if_stale(self) -> None:
        if self._source_coordinator is None:
            return
        fetched_at = self._source_coordinator.fetched_at
        if fetched_at and dt_util.utcnow() - fetched_at < timedelta(minutes=STALE_REFRESH_MINUTES):
            return
        # Дебаунсер джерела зводить одночасні запити кількох черг в один
        self.hass.async_create_task(self._source_coordinator.async_request_refresh())

    def _build_payload(self) -> dict[str, Any]:
        last_json = self._source_coordinator.data if self._source_coordinator else None
        if not last_json:
            raise UpdateFailed(f"No data available from {self.source_url}")

        try:
            payload = self._build_from_api(last_json)
        except Exception as e:
            raise UpdateFailed(f"Parse/Build error for {self.region}: {e}") from e

        self._schedule_precise_refresh(payload)
        return payload

    def _updated_iso(self) -> str:
        """Час останньої успішної перевірки джерела, а не момент перерахунку."""
        fetched_at = self._source_coordinator.fetched_at if self._source_coordinator else None
        return (fetched_at or dt_util.utcnow()).replace(microsecond=0).isoformat()

    def _build_from_api(self, api: dict[str, Any]) -> dict[str, Any]:
        date_today = api.get("date_today")
        date_tomorrow = api.get("date_tomorrow")

        # Шукаємо регіон.
        # self.api_region_key вже налаштовано в __init__:
        # - для нових областей це правильний ключ (kharkivska...)
        # - для старих областей це старий ключ (mikolaivska...)
        regions_list = api.get("regions", [])
        region_obj = next((r for r in regions_list if r.get("cpu") == self.api_region_key), None)
        
        if not region_obj:
            raise ValueError(f"Region '{self.api_region_key}' not found in API response")

        # --- Стандартна логіка парсингу (без змін) ---
        
        # Check for Day Rollover (Midnight)
        # If API date_today is different from the date in our last successful data,
        # it means the day has switched.
        if self.data and self.data.get("date") and date_today:
            previous_date = self.data["date"]
            if previous_date != date_today:
                _LOGGER.debug("Day rollover detected: %s -> %s. Clearing history.", previous_date, date_today)
                # User Request (Fix):
                # Clear history completely on day switch.
                self._history_today = []
                self._history_tomorrow = []
                # We also need to ensure we don't push "yesterday's" schedule into today's history in the block below.
                # So we return early or handle it by not matching dates. 
                # Ideally we can use a flag, but since we are inside a method, let's just note it.
                # Actually, we can rely on verifying 'previous_date == date_today' before updating history.

        is_emergency = region_obj.get("emergency", False)
        schedule = (region_obj.get("schedule") or {}).get(self.queue) or {}
        slots_today_map = schedule.get(date_today) or {}
        slots_tomorrow_map = schedule.get(date_tomorrow) or {}

        has_any_slots = any(v in (1, 2) for v in slots_today_map.values())
        if not has_any_slots:
             base_day = datetime.fromisoformat(date_today).date() if date_today else dt_util.now(TZ_KYIV).date()
             return {
                "queue": self.queue,
                "date": base_day.isoformat(),
                "now_status": "nosched",
                "now_halfhour_index": None,
                "next_change_at": None,
                "today_48half": [],
                "updated": self._updated_iso(),
                "source": self.source_url,
                "next_on_at": None,
                "next_off_at": None,
                "is_emergency": is_emergency,
                "today_outage_hours": None,
                "tomorrow_outage_hours": None,
                "longest_outage_hours": None,
                "history_today_48half": [],
                "history_tomorrow_48half": [],
                "tomorrow_date": None,
                "tomorrow_48half": [],
             }

        def build_half_list(slots_map: dict[str, int]) -> list[str]:
            res: list[str] = []
            for h in range(24):
                for m in (0, 30):
                    label = f"{h:02d}:{m:02d}"
                    code = int(slots_map.get(label, 0))
                    if code == 1: res.append("on")
                    elif code == 2: res.append("off")
                    else: res.append("unknown")
            return res

        today_half = build_half_list(slots_today_map)
        tomorrow_half = build_half_list(slots_tomorrow_map) if slots_tomorrow_map else []
        # If all slots are "unknown", there's no real schedule → treat as empty
        if tomorrow_half and all(s == "unknown" for s in tomorrow_half):
            tomorrow_half = []

        # --- Statistics calculation ---
        today_outage_hours = today_half.count("off") * 0.5
        tomorrow_outage_hours = tomorrow_half.count("off") * 0.5 if tomorrow_half else None

        def get_longest_consecutive_off(series: list[str]) -> float:
            max_count = 0
            current_count = 0
            for state in series:
                if state == "off":
                    current_count += 1
                else:
                    max_count = max(max_count, current_count)
                    current_count = 0
            max_count = max(max_count, current_count)
            return max_count * 0.5

        # Longest outage can span across today and tomorrow if available
        combined_half = today_half + (tomorrow_half or [])
        longest_outage = get_longest_consecutive_off(combined_half)

        now_local = dt_util.now(TZ_KYIV)
        base_day = datetime.fromisoformat(date_today).date() if date_today else now_local.date()
        
        if now_local.date() != base_day:
            idx = 0
        else:
            idx = now_local.hour * 2 + (1 if now_local.minute >= 30 else 0)

        cur = today_half[idx] if today_half else "unknown"

        next_on_at = self._find_next_at(["on"], base_day, today_half, idx, date_tomorrow, tomorrow_half)
        next_off_at = self._find_next_at(["off"], base_day, today_half, idx, date_tomorrow, tomorrow_half)

        # Determine next change time based on current status and calculated timestamps
        if cur == "off":
            next_change_iso = next_on_at
        elif cur == "on":
            next_change_iso = next_off_at
        else:
            # For "unknown" status, pick the earliest transition (either on or off)
            if next_on_at and next_off_at:
                next_change_iso = min(next_on_at, next_off_at)
            else:
                next_change_iso = next_on_at or next_off_at
        next_change_hhmm = None
        if next_change_iso:
            try:
                dt_change = dt_util.parse_datetime(next_change_iso)
                if dt_change:
                    # Localize to Kyiv to get the correct HH:MM
                    dt_local = self._localize_kyiv(dt_change)
                    next_change_hhmm = dt_local.strftime("%H:%M")
            except Exception as e:
                _LOGGER.debug("Error formatting next_change_at: %s", e)

        data = {
            "queue": self.queue,
            "date": base_day.isoformat(),
            "now_status": cur,
            "now_halfhour_index": idx,
            "next_change_at": next_change_hhmm,
            "today_48half": today_half,
            "updated": self._updated_iso(),
            "source": self.source_url,
            "next_on_at": next_on_at,
            "next_off_at": next_off_at,
            "today_outage_hours": today_outage_hours,
            "tomorrow_outage_hours": tomorrow_outage_hours,
            "longest_outage_hours": longest_outage,
            "history_today_48half": self._history_today,
            "history_tomorrow_48half": self._history_tomorrow,
            "is_emergency": is_emergency,
        }

        # Update "history" (store up to 3 previous versions)
        # Only update history if the date hasn't changed (avoid pushing yesterday's data into today's history)
        if self.data and self.data.get("date") == date_today:
            old_today = self.data.get("today_48half", [])
            if today_half and old_today and today_half != old_today:
                if not self._history_today or old_today != self._history_today[0]:
                    self._history_today.insert(0, old_today)
                    self._history_today = self._history_today[:3]
                data["history_today_48half"] = self._history_today
            
            old_tomorrow = self.data.get("tomorrow_48half", [])
            if tomorrow_half and old_tomorrow and tomorrow_half != old_tomorrow:
                if not self._history_tomorrow or old_tomorrow != self._history_tomorrow[0]:
                    self._history_tomorrow.insert(0, old_tomorrow)
                    self._history_tomorrow = self._history_tomorrow[:3]
                data["history_tomorrow_48half"] = self._history_tomorrow

        if date_tomorrow and tomorrow_half:
            data["tomorrow_date"] = date_tomorrow
            data["tomorrow_48half"] = tomorrow_half

        return data

    # ... методи _localize_kyiv, _schedule_precise_refresh та статичні методи без змін ...
    def _localize_kyiv(self, d: datetime) -> datetime:
        if d.tzinfo is not None:
            return d.astimezone(TZ_KYIV)
        localize = getattr(TZ_KYIV, "localize", None)
        if callable(localize):
            return localize(d)
        return d.replace(tzinfo=TZ_KYIV)

    def _schedule_precise_refresh(self, data: dict[str, Any]) -> None:
        if data.get("now_status") == "nosched":
            if self._unsub_precise:
                self._unsub_precise()
                self._unsub_precise = None
            return

        if self._unsub_precise:
            self._unsub_precise()
            self._unsub_precise = None

        next_change_hhmm = data.get("next_change_at")
        base_date_iso = data.get("date")
        if not next_change_hhmm or not base_date_iso:
            return

        try:
            hh, mm = [int(x) for x in next_change_hhmm.split(":")]
            base_day = datetime.fromisoformat(base_date_iso).date()
            local_naive = datetime.combine(base_day, time(hour=hh, minute=mm, second=0, microsecond=0))
            candidate_kyiv = self._localize_kyiv(local_naive)
            now_kyiv = dt_util.now(TZ_KYIV)
            if candidate_kyiv <= now_kyiv:
                candidate_kyiv = candidate_kyiv + timedelta(days=1)
            candidate_utc = dt_util.as_utc(candidate_kyiv)

            @callback
            def _tick(_now) -> None:
                self._unsub_precise = None
                # Локальний перерахунок; за застарілих даних — ще й оновлення джерела
                self.hass.async_create_task(self.async_request_refresh())

            self._unsub_precise = async_track_point_in_utc_time(self.hass, _tick, candidate_utc)
        except Exception as e:
            _LOGGER.debug("Failed to schedule precise refresh: %s", e)

    @staticmethod
    def _find_next_at(target_states: list[str], base_date: date, today_half: list[str], idx: int, tomorrow_date_iso: Optional[str], tomorrow_half: Optional[list[str]]) -> Optional[str]:
        if not today_half: return None
        today_tail = today_half[idx + 1 :]
        seq = list(today_tail)
        has_tomorrow = bool(tomorrow_date_iso and tomorrow_half)
        if has_tomorrow: seq.extend(tomorrow_half or [])
        
        pos = next((i for i, s in enumerate(seq) if s in target_states), None)
        if pos is None: return None

        if pos < len(today_tail):
            base_local_midnight = datetime.combine(base_date, datetime.min.time(), tzinfo=TZ_KYIV)
            minutes_from_base = (idx + 1 + pos) * 30
            next_local = base_local_midnight + timedelta(minutes=minutes_from_base)
        else:
            if not has_tomorrow: return None
            tomorrow_date = datetime.fromisoformat(tomorrow_date_iso).date()
            base_local_midnight = datetime.combine(tomorrow_date, datetime.min.time(), tzinfo=TZ_KYIV)
            minutes_into_tomorrow = (pos - len(today_tail)) * 30
            next_local = base_local_midnight + timedelta(minutes=minutes_into_tomorrow)

        return dt_util.as_utc(next_local).isoformat()
