"""
Конфигурация Growth Agent.

Важно: здесь нет ничего специфичного для TruePost/АвтоПоста, кроме
значений по умолчанию в .env (которые человек заполняет сам при деплое).
Сам код конфигурации универсален — он просто читает переменные окружения
для "текущего подключённого проекта", какой бы он ни был.
"""

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


# Соответствие нормализованных ключей воронки названиям целей в Яндекс.Метрике.
# Это словарь по умолчанию для проекта, у которого нет собственного mapping
# в Project.settings_json. Хранится здесь как fallback, а не как единственный
# источник правды -- per-project mapping в БД имеет приоритет.
DEFAULT_METRIKA_GOAL_MAPPING = {
    "signup": "register_success",
    "activation_1": "channel_created",
    "activation_2": "post_generated",
    "payment_started": "payment_started",
    "payment_success": "payment_success",
}

# Нормализованные ключи воронки, которые ядро (analyzer.py, rules.py,
# health_score.py) понимает. Список не закрытый -- per-project mapping
# может содержать дополнительные activation_3, activation_4 и т.д.,
# но эти пять/семь ключей считаются базовыми и всегда проверяются.
CORE_FUNNEL_KEYS = [
    "traffic",
    "signup",
    "activation_1",
    "activation_2",
    "payment_started",
    "payment_success",
    "revenue",
]

ANALYSIS_WINDOWS_HOURS = {
    "3h": 3,
    "24h": 24,
    "7d": 168,
}

# Пороги для правила "низкая конверсия в регистрацию" (low_signup_conversion).
# Не P1, потому что это не "проблема подтверждена", а "стоит присмотреться".
# Живут в config.py как дефолты; per-project override -- через
# Project.settings_json["thresholds"], если когда-нибудь понадобится разный
# порог для разных проектов (например, у Зари будет другая норма конверсии).
MIN_CLICKS_FOR_CONVERSION_CHECK = 100
MIN_SIGNUP_CONVERSION_WARN_PERCENT = 2.0

# Сколько часов считать deep diagnostics кэш свежим, чтобы не дёргать
# granular-отчёты Директа на каждый /run. Per-project override -- через
# Project.settings_json["deep_diagnostics_cache_ttl_hours"], если когда-то
# понадобится другой ритм для другого проекта.
DEEP_DIAGNOSTICS_CACHE_TTL_HOURS = 6

# Минимальный объём данных, при котором deep diagnostics даёт осмысленный
# результат, а не шум. Ниже этого порога агент пишет "данных мало" и не
# запускает granular-анализ автоматически (но кнопка "Проверить глубже"
# всё равно доступна как принудительный запуск с явной пометкой "предварительно").
MIN_CLICKS_FOR_DEEP_DIAGNOSTICS = 30

# Пороги для landing funnel diagnostics (правила A-F). Каждый порог --
# "доля, ниже которой считаем переход на этом шаге воронки проблемным".
# Например LANDING_VIEWS_VS_CLICKS_MIN_RATIO=0.5 означает: если landing_views
# меньше 50% от Direct-кликов за тот же период, это сигнал A (проблема в
# переходе/загрузке/tracking). Эти числа -- разумные дефолты, не результат
# A/B теста на реальных данных АвтоПоста (на момент написания трафика мало
# для статистической калибровки) -- если станут давать слишком много
# ложных тревог на реальных данных, имеет смысл их пересмотреть.
LANDING_VIEWS_VS_CLICKS_MIN_RATIO = 0.5
CTA_CLICKS_VS_VIEWS_MIN_RATIO = 0.05
BOT_STARTS_VS_CTA_MIN_RATIO = 0.5
# Минимальный объём cta_hero_bot_clicks (НЕ суммы CTA), при котором правило
# C (Telegram-open path) даёт уверенный вывод. Ниже этого порога -- low
# confidence, не уверенный сигнал о проблеме (требование E: "если Telegram
# CTA clicks мало, не делать уверенный вывод").
MIN_TELEGRAM_CTA_CLICKS_FOR_CONFIDENT_FINDING = 10
REGISTER_VS_BOT_STARTS_MIN_RATIO = 0.3
# Минимальный объём (landing_views), ниже которого правила B-F не
# применяются вообще -- слишком мало данных для процентных порогов,
# 1-2 события дают случайный шум, не сигнал.
MIN_LANDING_VIEWS_FOR_FUNNEL_DIAGNOSTICS = 10
# Если raw в N+ раз больше unique -- instrumentation warning (правило F).
RAW_VS_UNIQUE_WARNING_MULTIPLIER = 1.5

# Минимальный возраст landing tracking, при котором сравнение с Direct
# clicks за весь requested-период считается надёжным. Если tracking_started_at
# моложе этого порога ОТНОСИТЕЛЬНО начала запрошенного периода (period_hours),
# правило A не делает вывод "переход с рекламы сломан" -- классифицирует
# это как data_quality_warning (period mismatch), потому что Direct clicks
# включают время до того, как landing tracking начал что-либо считать.
LANDING_TRACKING_MIN_MATURITY_HOURS = 1

# Словарь кластеров поисковых запросов по умолчанию -- используется, если
# в Project.settings_json["query_clusters"] ничего не задано. Per-project
# словарь почти всегда нужен (у каждого продукта свой релевантный intent),
# но дефолт даёт системе работать "из коробки" для АвтоПоста без
# дополнительной настройки -- и не даёт коду упасть, если settings_json
# пустой или повреждён (см. diagnostics.py: graceful fallback).
DEFAULT_QUERY_CLUSTERS = {
    "good": {
        "telegram_autoposting": {
            "label": "Telegram / автопостинг",
            "include": [
                "автопостинг", "автопост", "telegram", "телеграм",
                "посты для телеграм", "контент для телеграм",
                "контент план телеграм", "бот для постинга",
            ],
            "exclude": [],
        },
        "smm_content": {
            "label": "SMM / контент",
            "include": [
                "контент план", "ведение канала", "посты для канала",
                "контент для канала",
            ],
            "exclude": [],
        },
    },
    "irrelevant": {
        "generic_text_generation": {
            "label": "Общая генерация текста",
            "include": [
                "генерация текста", "сгенерировать текст", "нейросеть текст",
                "написать текст", "переписать текст",
            ],
            "exclude": ["telegram", "телеграм", "канал"],
        },
        "student_homework": {
            "label": "Учёба / рефераты / сочинения",
            "include": ["реферат", "сочинение", "эссе", "курсовая", "домашнее задание"],
            "exclude": [],
        },
    },
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Инфраструктура ---
    database_url: str = "sqlite:///./growth_agent.db"
    public_url: Optional[str] = None

    # --- Telegram ---
    bot_token: Optional[str] = None
    bot_admin_chat_ids: str = ""  # список через запятую, парсится в telegram_bot.py

    # --- LLM (опционально) ---
    llm_provider: str = "none"  # none | openai | anthropic
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: Optional[str] = None
    anthropic_model: str = "claude-sonnet-4-6"

    # --- Текущий подключённый проект ---
    # В v1 сервис обслуживает один активный проект. Project как модель в БД
    # универсален (на будущее), но эти переменные описывают единственную
    # текущую интеграцию, которая будет создана/обновлена в БД при старте.
    project_name: str = "Проект"
    project_type: str = "telegram_saas"
    project_connector: str = "truepost"  # имя модуля в connectors/, не хардкод в core
    project_base_url: Optional[str] = None
    project_internal_api_token: Optional[str] = None

    # --- Яндекс.Метрика ---
    yandex_oauth_token: Optional[str] = None
    metrika_counter_id: Optional[str] = None
    metrika_goal_ids_json: str = "{}"  # JSON-строка: {"signup": 123456, "activation_1": 123457, ...}

    # --- Яндекс.Директ ---
    direct_client_login: Optional[str] = None
    direct_campaign_ids: str = ""  # список через запятую
    direct_oauth_token: Optional[str] = None
    direct_sandbox: bool = False

    # --- YooKassa ---
    yookassa_shop_id: Optional[str] = None
    yookassa_secret_key: Optional[str] = None

    # --- Планировщик ---
    watch_interval_seconds: int = 10800  # 3 часа
    default_mode: str = "watch_only"

    # --- Пороги для свежести данных интеграций ---
    integration_stale_minutes: int = 180  # если as_of старше -- алерт integration_down

    @property
    def admin_chat_ids_list(self) -> list[str]:
        return [c.strip() for c in self.bot_admin_chat_ids.split(",") if c.strip()]

    @property
    def direct_campaign_ids_list(self) -> list[str]:
        return [c.strip() for c in self.direct_campaign_ids.split(",") if c.strip()]

    @property
    def metrika_goal_ids(self) -> dict[str, int]:
        """
        Парсит METRIKA_GOAL_IDS_JSON в dict {normalized_key: goal_id}.
        Возвращает пустой dict при отсутствии/невалидном JSON -- это не
        ошибка конфигурации сама по себе (Метрика просто не будет давать
        данные по целям), обработка отсутствия -- на стороне connector.
        """
        import json
        try:
            parsed = json.loads(self.metrika_goal_ids_json)
            return {k: int(v) for k, v in parsed.items()}
        except (json.JSONDecodeError, ValueError, TypeError):
            return {}

    @property
    def effective_direct_oauth_token(self) -> Optional[str]:
        """
        DIRECT_OAUTH_TOKEN, если задан отдельно, иначе общий
        YANDEX_OAUTH_TOKEN -- реалистично, что один и тот же OAuth-токен
        Яндекса используется и для Метрики, и для Директа. Отдельная
        переменная даёт гибкость, если потребуется разделить токены позже.
        """
        return self.direct_oauth_token or self.yandex_oauth_token


@lru_cache
def get_settings() -> Settings:
    return Settings()



# Runtime safety timeouts. /run must always return a final Telegram message,
# even if Direct/Metrika/product endpoints hang or build reports too slowly.
# Values are deliberately conservative for a Telegram command: deep/manual
# diagnostics can be run separately when a slower granular report is needed.
CONNECTOR_CALL_TIMEOUT_SECONDS = 20.0
DIRECT_SUMMARY_TIMEOUT_SECONDS = 12.0
DIRECT_SUMMARY_MAX_RETRIES = 2
DEEP_DIAGNOSTICS_TIMEOUT_SECONDS = 35.0
RUN_CYCLE_TIMEOUT_SECONDS = 75.0
MANUAL_RUN_TIMEOUT_SECONDS = 75.0
MANUAL_RUN_STALE_AFTER_SECONDS = 120.0
DIRECT_REPORT_RETRY_SLEEP_CAP_SECONDS = 3.0
STATUS_COMMAND_DB_TIMEOUT_SECONDS = 1.5

# Метка версии для диагностики деплоя -- не семантический номер версии,
# просто текстовая метка последнего значимого изменения. Видна через
# /status и /start, чтобы быстро проверить, какая версия кода реально
# запущена на сервере, не гадая по поведению.
BUILD_MARKER = "growth-agent-statusfix-2026-06-25"
