from enum import Enum

from homeassistant.const import Platform

DOMAIN = "svitlo_live"

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.CALENDAR,
]

# Інтервал опитування: для проксі-API фіксований, для ПОЕ — один спільний на всю інтеграцію
DEFAULT_SCAN_INTERVAL_MINUTES = 10
DEFAULT_SCAN_INTERVAL = DEFAULT_SCAN_INTERVAL_MINUTES * 60  # секунди
MIN_SCAN_INTERVAL_MINUTES = 8   # межі налаштування інтервалу ПОЕ
MAX_SCAN_INTERVAL_MINUTES = 60

# Якщо на момент зміни статусу черги дані джерела старші за це — дотягуємо свіжі
STALE_REFRESH_MINUTES = 5
# Після невдалого запиту не ходимо до джерела повторно протягом цього часу
FAILURE_BACKOFF_SECONDS = 60
# Дані, отримані щойно, віддаємо з кешу (старт кількох записів, перезавантаження)
REUSE_WINDOW_SECONDS = 60
# Оновлення після півночі за Києвом: пізніше за вікно повторного використання, щоб
# опитування о 23:59 не повернуло з кешу дані зі вчорашніми датами
MIDNIGHT_REFRESH_DELAY_SECONDS = REUSE_WINDOW_SECONDS + 30
# Каталог регіонів потрібен лише майстру налаштування та визначенню джерела
CATALOG_MAX_AGE_HOURS = 6


class Source(str, Enum):
    """Джерело даних: кожне опитується одним спільним координатором."""

    DTEK = "dtek"
    OLD = "old"
    POE = "poe"


# Спільні налаштування інтеграції (поза записами)
STORAGE_KEY = "svitlo_live.settings"
STORAGE_VERSION = 1
STORE_POE_INTERVAL = "poe_scan_interval_minutes"

CONF_REGION = "region"
CONF_QUEUE = "queue"
CONF_OPERATOR = "operator"
CONF_PRESERVE_ID = "preserve_id"
CONF_SCAN_INTERVAL = "scan_interval_minutes"

# Static mappings are deprecated in favor of dynamic fetching, but kept for migration if needed.
API_REGION_MAP = {
    "harkivska-oblast": "kharkivska-oblast",
    "hmelnitska-oblast": "khmelnytska-oblast",
    "chernigivska-oblast": "chernihivska-oblast",
    "jitomirska-oblast": "zhytomyrska-oblast",
}

# --- 3. СПИСОК РЕГІОНІВ ДЛЯ НОВОГО API ---
# Якщо регіон (або його переклад) є в цьому списку -> йдемо на DTEK_API_URL
# Інакше -> йдемо на OLD_API_URL
NEW_API_REGIONS = {
    "kyiv", "kiivska-oblast",
    "odeska-oblast",
    "dnipro-dnem", "dnipro-cek", "dnipro-city", "dnipropetrovska-oblast",
    "lvivska-oblast",
    "chernivetska-oblast",
    # Нові з парсера
    "kharkivska-oblast", 
    "poltavska-oblast",
    "cherkaska-oblast",
    "chernihivska-oblast",
    "khmelnytska-oblast",
    "ivano-frankivska-oblast",
    "rivnenska-oblast",
    "ternopilska-oblast",
    "zakarpatska-oblast",
    "zaporizka-oblast",
    "zhytomyrska-oblast",
    "sumska-oblast",
}


REGION_QUEUE_MODE = {
    "chernivetska-oblast": "GRUPA_NUM",
    "donetska-oblast": "GRUPA_NUM",
}

# --- 4. ДВА API ---
OLD_API_URL = "https://svitlo-proxy.svitlo-proxy.workers.dev"  # Для старих
DTEK_API_URL = "https://dtek-api.svitlo-proxy.workers.dev/"    # Для нових

# --- 5. ПОЕ (Полтаваобленерго), HTML-сторінка напряму ---
POE_URL = "https://www.poe.pl.ua/customs/dynamicgpv-info.php"
# JSON з поточними заходами розвантаження (ГАВ/СГАВ/ГОП/...), той самий, що показує головна сторінка ПОЕ
POE_UNLOADING_URL = "https://www.poe.pl.ua/customs/dynamic-unloading-info.php"
POE_REGION_ID = "poltavska-oblast-poe"
POE_REGION_NAME = "Полтавська область (ПОЕ)"
POE_QUEUES = [f"{q}.{s}" for q in range(1, 7) for s in (1, 2)]

SOURCE_URLS = {
    Source.DTEK: DTEK_API_URL,
    Source.OLD: OLD_API_URL,
    Source.POE: POE_URL,
}
