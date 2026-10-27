"""Parser for the ПОЕ (Poltavaoblenergo) HTML schedule page.

Kept free of Home Assistant imports so it can be tested standalone.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any, Optional

from bs4 import BeautifulSoup

_LOGGER = logging.getLogger(__name__)

UK_MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4,
    "травня": 5, "червня": 6, "липня": 7, "серпня": 8,
    "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}

_DATE_RE = re.compile(r"(\d{1,2})\s+(" + "|".join(UK_MONTHS) + r")\s+(\d{4})", re.IGNORECASE)
_QUEUE_RE = re.compile(r"(\d+)\.(\d+)")
# Час слоту з title клітинки, напр. "3.1: 10:00–10:30 — «Вимкнено», ..."
_SLOT_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[–-]")

# Класи станів клітинок таблиці ПОЕ -> коди спільного формату API (1 = світло є, 2 = відключення).
# Легенда на сайті:
#   gpv-s1 = зелений, «Увімкнено», електроенергія наявна
#   gpv-s2 = червоний, «Вимкнено», електроенергія відсутня
#   gpv-s3 = жовтий, «Час, необхідний для перемикань. Електроенергії може не бути»
# Враховуємо лише гарантовані (червоні) відключення, тому gpv-s3 трактуємо як "світло є".
SLOT_CLASS_CODES = {
    "gpv-s1": 1,
    "gpv-s2": 2,
    "gpv-s3": 1,
}

SLOTS_PER_DAY = 48


def _parse_section_date(section: Any) -> Optional[date]:
    """Дата секції з першого <b> (напр. '6 жовтня 2026 року'), інакше з усього тексту."""
    candidates = []
    first_bold = section.find("b")
    if first_bold:
        candidates.append(first_bold.get_text(" ", strip=True))
    candidates.append(section.get_text(" ", strip=True))

    for text in candidates:
        match = _DATE_RE.search(text)
        if not match:
            continue
        day, month_name, year = match.groups()
        try:
            return date(int(year), UK_MONTHS[month_name.lower()], int(day))
        except ValueError:
            return None
    return None


def _slot_code(cell: Any) -> Optional[int]:
    for cls in cell.get("class", []):
        if cls in SLOT_CLASS_CODES:
            return SLOT_CLASS_CODES[cls]
    return None


def _slot_label(cell: Any, index: int) -> str:
    """Час слоту "HH:MM": з title клітинки, інакше за її позицією в рядку."""
    match = _SLOT_TIME_RE.search(cell.get("title", ""))
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour < 24 and minute in (0, 30):
            return f"{hour:02d}:{minute:02d}"
    return f"{index // 2:02d}:{30 * (index % 2):02d}"


def _parse_table(table: Any) -> dict[str, dict[str, int]]:
    """Повертає {"1.1": {"00:00": 1, "00:30": 2, ...}, ...} для однієї таблиці."""
    result: dict[str, dict[str, int]] = {}
    body = table.find("tbody") or table

    for row in body.find_all("tr"):
        # Перша клітинка рядка: td.gpv-q з номером черги, напр. "3.1"
        queue_cell = row.find("td", class_="gpv-q")
        if not queue_cell:
            continue
        match = _QUEUE_RE.search(queue_cell.get_text(" ", strip=True))
        if not match:
            continue
        queue_id = f"{match.group(1)}.{match.group(2)}"

        slots: dict[str, int] = {}
        for index, cell in enumerate(row.find_all("td", class_="gpv-c")):
            code = _slot_code(cell)
            slots[_slot_label(cell, index)] = code if code is not None else 0

        if len(slots) != SLOTS_PER_DAY:
            _LOGGER.warning(
                "POE: queue %s has %d slots instead of %d, skipping",
                queue_id, len(slots), SLOTS_PER_DAY,
            )
            continue

        result[queue_id] = slots

    return result


def parse_poe_html(html: str, region_id: str, region_name: str, today: date, tomorrow: date) -> dict[str, Any]:
    """Convert the POE HTML page into the common API format used by the coordinator."""
    soup = BeautifulSoup(html, "html.parser")
    wanted = {today, tomorrow}
    by_date: dict[str, dict[str, dict[str, int]]] = {}

    for section in soup.find_all("div", class_="gpvinfodetail"):
        section_date = _parse_section_date(section)
        if section_date not in wanted:
            continue
        # Горизонтальна таблиця; вертикальна gpv-vtable дублює ті самі дані для мобільних
        table = section.find("table", class_="gpv-table")
        if not table:
            _LOGGER.warning("POE: schedule table not found for %s", section_date)
            continue
        by_date[section_date.isoformat()] = _parse_table(table)

    schedule: dict[str, dict[str, dict[str, int]]] = {}
    for date_str, queues in by_date.items():
        for queue_id, slots in queues.items():
            schedule.setdefault(queue_id, {})[date_str] = slots

    return {
        "date_today": today.isoformat(),
        "date_tomorrow": tomorrow.isoformat(),
        "regions": [
            {
                "cpu": region_id,
                "name_ua": region_name,
                "emergency": False,
                "schedule": schedule,
            }
        ],
    }
