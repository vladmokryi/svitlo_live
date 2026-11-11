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
    API_REGION_MAP,
    REVERSE_API_REGION_MAP,
    DNIPRO_REGIONS,
)
from .api_hub import SvitloApiHub, SvitloSourceCoordinator

_LOGGER = logging.getLogger(__name__)

TZ_KYIV = dt_util.get_time_zone("Europe/Kyiv")

# Запасне джерело для автоматичного fallback між New (DTEK) та Old API; у ПОЕ його немає
FALLBACK_SOURCE: dict[Source, Source] = {Source.DTEK: Source.OLD, Source.OLD: Source.DTEK}


class SvitloCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Дані конкретного region/queue з підтримкою fallback між New та Old API.

    Сам у мережу не ходить: слухає спільні координатори свого джерела (і запасного,
    якщо воно є) та перебудовує дані з їхнього JSON. Власне оновлення (точний тік на
    зміну статусу, ручне оновлення) перераховує стан локально і лише за застарілих
    даних просить джерела оновитися.
    """

    def __init__(self, hass: HomeAssistant, config: dict[str, Any], hub: SvitloApiHub, subscriber_id: str) -> None:
        self.hass = hass
        self.hub = hub
        self.region: str = config[CONF_REGION]
        self.queue: str = config[CONF_QUEUE]
        self._subscriber_id = subscriber_id

        # Визначаються один раз в async_attach
        self.source: Optional[Source] = None  # первинне джерело
        self._fallback_source: Optional[Source] = None
        self._primary_key: str = self.region
        self._sources: dict[Source, SvitloSourceCoordinator] = {}
        self._unsub_sources: list[Callable[[], None]] = []

        # Джерело, з якого побудовано поточні дані (первинне або запасне)
        self._used_source: Optional[Source] = None
        self.source_url: str = ""
        self.api_region_key = self.region

        self._history_today: list[list[str]] = []
        self._history_tomorrow: list[list[str]] = []

        self._unsub_precise: Optional[Callable[[], None]] = None

        super().__init__(
            hass=hass,
            logger=_LOGGER,
            name=f"svitlo_live_{self.region}_{self.queue}",
            update_interval=None,  # опитують координатори джерел
        )

    async def async_attach(self) -> None:
        """Визначає джерело та підписується на спільні координатори (первинний і запасний)."""
        self.source, self._primary_key = await self.hub.async_resolve_source(self.region)
        self.api_region_key = self._primary_key
        self.source_url = SOURCE_URLS[self.source]
        self._fallback_source = FALLBACK_SOURCE.get(self.source)
        for source in (self.source, self._fallback_source):
            if source is None:
                continue
            coordinator = await self.hub.async_acquire_source(source, self._subscriber_id)
            self._sources[source] = coordinator
            self._unsub_sources.append(coordinator.async_add_listener(self._handle_source_update))

    @callback
    def async_detach(self) -> None:
        if self._unsub_precise:
            self._unsub_precise()
            self._unsub_precise = None
        for unsub in self._unsub_sources:
            unsub()
        self._unsub_sources.clear()
        for source in self._sources:
            self.hub.async_release_source(source, self._subscriber_id)
        self._sources.clear()

    @callback
    def _handle_source_update(self) -> None:
        """Джерело отримало нові дані (або помилку) — перебудовуємо дані черги."""
        try:
            payload = self._build_current()
        except UpdateFailed as err:
            self.async_set_update_error(err)
            return
        self.async_set_updated_data(payload)

    async def _async_update_data(self) -> dict[str, Any]:
        """Точний тік на зміну статусу або ручне оновлення сутності.

        Стан перераховується одразу з наявних даних (статус перемкнеться вчасно навіть
        без мережі), а якщо дані джерел застарілі — джерела просимо оновитися.
        """
        payload = self._build_current()
        self._request_source_refresh_if_stale()
        return payload

    def _request_source_refresh_if_stale(self) -> None:
        now = dt_util.utcnow()
        for coordinator in self._sources.values():
            fetched_at = coordinator.fetched_at
            if fetched_at and now - fetched_at < timedelta(minutes=STALE_REFRESH_MINUTES):
                continue
            # Дебаунсер джерела зводить одночасні запити кількох черг в один
            self.hass.async_create_task(coordinator.async_request_refresh())

    def _source_data(self, source: Optional[Source]) -> Optional[dict[str, Any]]:
        coordinator = self._sources.get(source) if source else None
        return coordinator.data if coordinator else None

    def _updated_iso(self) -> str:
        """Час останньої успішної перевірки використаного джерела, а не момент перерахунку."""
        coordinator = self._sources.get(self._used_source) if self._used_source else None
        fetched_at = coordinator.fetched_at if coordinator else None
        return (fetched_at or dt_util.utcnow()).replace(microsecond=0).isoformat()

    def _get_candidate_keys(self, region: str, is_new: bool) -> list[str]:
        """Отримує список можливих ключів регіону для заданого API."""
        candidates: list[str] = []
        if is_new:
            if region in API_REGION_MAP:
                candidates.append(API_REGION_MAP[region])
            candidates.append(region)
        else:
            if region in REVERSE_API_REGION_MAP:
                candidates.append(REVERSE_API_REGION_MAP[region])
            if region in DNIPRO_REGIONS:
                candidates.append("dnipropetrovska-oblast")
            candidates.append(region)
            if region in API_REGION_MAP:
                candidates.append(API_REGION_MAP[region])
        seen: set[str] = set()
        return [x for x in candidates if not (x in seen or seen.add(x))]

    def _find_region_and_schedule(
        self, api_data: Optional[dict[str, Any]], candidate_keys: list[str]
    ) -> tuple[Optional[dict[str, Any]], Optional[str], Optional[dict[str, Any]]]:
        """Знаходить об'єкт регіону, його ключ та розклад для черги."""
        if not api_data:
            return None, None, None
        for r in api_data.get("regions", []):
            cpu = r.get("cpu")
            if cpu in candidate_keys:
                sched = (r.get("schedule") or {}).get(self.queue) or {}
                return r, cpu, sched
        return None, None, None

    def _score_schedule(self, schedule: Optional[dict[str, Any]], date_iso: str) -> int:
        """
        Оцінює якість розкладу на дату:
        2 - є слоти 1 або 2 (світло є / немає)
        1 - є ключ дати (наприклад, 0 - без обмежень)
        0 - відсутні дані на дату
        """
        if not schedule or date_iso not in schedule:
            return 0
        slots = schedule[date_iso]
        if isinstance(slots, dict) and any(v in (1, 2) for v in slots.values()):
            return 2
        return 1

    def _build_current(self) -> dict[str, Any]:
        """Будує дані черги з кешованого JSON джерел з автоматичним fallback між New та Old API."""
        if self.source is None:
            raise UpdateFailed(f"Source for {self.region} is not resolved yet")

        primary_source = self.source
        fallback_source = self._fallback_source
        primary_key = self._primary_key
        primary_json = self._source_data(primary_source)
        if not primary_json and not self._source_data(fallback_source):
            raise UpdateFailed(f"No data available from {SOURCE_URLS[primary_source]}")

        primary_is_new = primary_source is Source.DTEK
        fallback_is_new = not primary_is_new

        today = dt_util.now(TZ_KYIV).date()
        today_iso = today.isoformat()
        tomorrow_iso = (today + timedelta(days=1)).isoformat()

        # 1) Первинне джерело
        prim_candidates = self._get_candidate_keys(primary_key, is_new=primary_is_new)
        prim_reg, prim_key, prim_sched = self._find_region_and_schedule(primary_json, prim_candidates)
        prim_score = self._score_schedule(prim_sched, today_iso)
        prim_emergency = bool(prim_reg and prim_reg.get("emergency"))

        # 2) Перевіряємо альтернативне джерело (fallback), якщо в первинному немає повних даних
        # Якщо в первинному джерелі активні екстрені відключення (emergency), графіків може не бути (це норма).
        # Не шукаємо fallback якщо діє emergency у первинному і регіон знайдено.
        fall_reg, fall_key, fall_sched = None, None, None
        fall_score = 0

        if fallback_source is not None and ((prim_score < 2 and not prim_emergency) or not prim_reg):
            fall_candidates = self._get_candidate_keys(primary_key, is_new=fallback_is_new)
            fall_reg, fall_key, fall_sched = self._find_region_and_schedule(
                self._source_data(fallback_source), fall_candidates
            )
            fall_score = self._score_schedule(fall_sched, today_iso)

        # Перемикаємося на fallback тільки якщо:
        # 1) fall_score > prim_score
        # 2) І у первинному джерелі НЕМАЄ активного emergency (бо пустий розклад при emergency є штатною ситуацією,
        #    і його не можна замінювати заглушкою з нулями зі старого API).
        if fall_score > prim_score and not prim_emergency:
            _LOGGER.warning(
                "Using fallback API (%s) for %s/%s (primary score=%d vs fallback score=%d)",
                SOURCE_URLS[fallback_source],
                self.region,
                self.queue,
                prim_score,
                fall_score,
            )
            used_source, used_reg, used_key, used_sched = fallback_source, fall_reg, fall_key, fall_sched
            other_source, other_sched = primary_source, prim_sched
        else:
            used_source, used_reg, used_key, used_sched = primary_source, prim_reg, prim_key, prim_sched
            other_source, other_sched = fallback_source, fall_sched

        if not used_reg:
            raise UpdateFailed(f"Region '{self.region}' not found in API response")

        self._used_source = used_source
        self.api_region_key = used_key or primary_key
        self.source_url = SOURCE_URLS[used_source]

        # Визначаємо слоти на сьогодні та завтра
        slots_today_map = (used_sched or {}).get(today_iso) or {}
        slots_tomorrow_map = (used_sched or {}).get(tomorrow_iso) or {}

        # Якщо на завтра немає графіка в основному джерелі, перевіряємо альтернативне
        # (тільки якщо в основному джерелі не активна аварія без розкладів)
        if other_source is not None and self._score_schedule(used_sched, tomorrow_iso) < 2 and not prim_emergency:
            if other_sched is None:
                other_candidates = self._get_candidate_keys(primary_key, is_new=other_source is Source.DTEK)
                _, _, other_sched = self._find_region_and_schedule(
                    self._source_data(other_source), other_candidates
                )

            if other_sched and self._score_schedule(other_sched, tomorrow_iso) == 2:
                slots_tomorrow_map = other_sched.get(tomorrow_iso) or {}
                _LOGGER.debug(
                    "Augmenting tomorrow schedule for %s from alternative API",
                    self.region
                )

        # Статус екстрених відключень: якщо хоча б одне джерело (особливо первинне) повідомляє про аварію
        is_emergency = bool(
            (used_reg and used_reg.get("emergency"))
            or (prim_reg and prim_reg.get("emergency"))
            or (fall_reg and fall_reg.get("emergency"))
        )

        try:
            payload = self._build_payload(
                used_reg=used_reg,
                slots_today_map=slots_today_map,
                slots_tomorrow_map=slots_tomorrow_map,
                today_iso=today_iso,
                tomorrow_iso=tomorrow_iso,
                source_url=self.source_url,
                is_emergency=is_emergency,
            )
        except Exception as e:
            raise UpdateFailed(f"Parse/Build error for {self.region}: {e}") from e

        # 3) Precise tick
        self._schedule_precise_refresh(payload)
        return payload

    def _build_payload(
        self,
        used_reg: dict[str, Any],
        slots_today_map: dict[str, int],
        slots_tomorrow_map: dict[str, int],
        today_iso: str,
        tomorrow_iso: str,
        source_url: str,
        is_emergency: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Побудова внутрішнього стану координатора."""
        # Check for Day Rollover (Midnight)
        if self.data and self.data.get("date") and today_iso:
            previous_date = self.data["date"]
            if previous_date != today_iso:
                _LOGGER.debug("Day rollover detected: %s -> %s. Clearing history.", previous_date, today_iso)
                self._history_today = []
                self._history_tomorrow = []

        if is_emergency is None:
            is_emergency = bool(used_reg.get("emergency", False))
        has_any_slots = any(v in (1, 2) for v in slots_today_map.values())
        if not has_any_slots:
            base_day = datetime.fromisoformat(today_iso).date()
            return {
                "queue": self.queue,
                "date": base_day.isoformat(),
                "now_status": "nosched",
                "now_halfhour_index": None,
                "next_change_at": None,
                "today_48half": [],
                "updated": self._updated_iso(),
                "source": source_url,
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
        base_day = datetime.fromisoformat(today_iso).date()
        
        if now_local.date() != base_day:
            idx = 0
        else:
            idx = now_local.hour * 2 + (1 if now_local.minute >= 30 else 0)

        cur = today_half[idx] if today_half else "unknown"

        next_on_at = self._find_next_at(["on"], base_day, today_half, idx, tomorrow_iso, tomorrow_half)
        next_off_at = self._find_next_at(["off"], base_day, today_half, idx, tomorrow_iso, tomorrow_half)

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
            "source": source_url,
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
        if self.data and self.data.get("date") == today_iso:
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

        if tomorrow_iso and tomorrow_half:
            data["tomorrow_date"] = tomorrow_iso
            data["tomorrow_48half"] = tomorrow_half

        return data

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
    def _find_next_at(
        target_states: list[str],
        base_date: date,
        today_half: list[str],
        idx: int,
        tomorrow_date_iso: Optional[str],
        tomorrow_half: Optional[list[str]],
    ) -> Optional[str]:
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
