from homeassistant.const import Platform

DOMAIN = "svitlo_live"

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.CALENDAR,
]

# Інтервал опитування налаштовується для кожного запису (у хвилинах)
MIN_SCAN_INTERVAL_MINUTES = 8
MAX_SCAN_INTERVAL_MINUTES = 60
DEFAULT_SCAN_INTERVAL_MINUTES = 10
DEFAULT_SCAN_INTERVAL = DEFAULT_SCAN_INTERVAL_MINUTES * 60  # секунди

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
POE_REGION_ID = "poltavska-oblast-poe"
POE_REGION_NAME = "Полтавська область (ПОЕ)"
POE_QUEUES = [f"{q}.{s}" for q in range(1, 7) for s in (1, 2)]
