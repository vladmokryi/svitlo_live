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
_NUM_RE = re.compile(r"\d+")

# Класи комірок таблиці ПОЕ -> коди спільного формату API (1 = світло є, 2 = відключення).
# light_1 = зелений (світло є), light_2 = червоний (відключення),
# light_3 = жовтий ("можливе відключення"). Враховуємо лише гарантовані (червоні)
# відключення, тому light_3 трактуємо як "світло є" (так само робить DTEK-проксі).
SLOT_CLASS_CODES = {
    "light_1": 1,
    "light_2": 2,
    "light_3": 1,
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


def _slot_code(cell: Any) -> int:
    for cls in cell.get("class", []):
        if cls in SLOT_CLASS_CODES:
            return SLOT_CLASS_CODES[cls]
    return 0


def _parse_table(table: Any) -> dict[str, dict[str, int]]:
    """Повертає {"1.1": {"00:00": 1, "00:30": 2, ...}, ...} для однієї таблиці."""
    result: dict[str, dict[str, int]] = {}
    body = table.find("tbody") or table
    queue_num: Optional[str] = None

    for row in body.find_all("tr"):
        # Комірка черги має rowspan="2", тому є лише в першому рядку кожної черги.
        queue_cell = row.find("td", class_="turnoff-scheduleui-table-queue")
        if queue_cell:
            match = _NUM_RE.search(queue_cell.get_text())
            queue_num = match.group(0) if match else None

        subqueue_cell = row.find("td", class_="turnoff-scheduleui-table-subqueue")
        if not queue_num or not subqueue_cell:
            continue
        match = _NUM_RE.search(subqueue_cell.get_text())
        if not match:
            continue
        queue_id = f"{queue_num}.{match.group(0)}"

        slot_cells = [
            td for td in row.find_all("td")
            if any(cls.startswith("light_") for cls in td.get("class", []))
        ]
        if len(slot_cells) != SLOTS_PER_DAY:
            _LOGGER.warning(
                "POE: queue %s has %d slots instead of %d, skipping",
                queue_id, len(slot_cells), SLOTS_PER_DAY,
            )
            continue

        result[queue_id] = {
            f"{i // 2:02d}:{30 * (i % 2):02d}": _slot_code(cell)
            for i, cell in enumerate(slot_cells)
        }

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
        table = section.find("table")
        if not table:
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
