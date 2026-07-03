"""
Telegram Ads source tracking для Growth Agent.

Telegram Ads (через eLama) не имеет прямого API для получения
конверсий — в отличие от Яндекс.Директа. Данные о кликах берутся
из рекламного кабинета eLama/Telegram Ads вручную.

В продукте (AutoPost/TruePost) можно отслеживать только события
ПОСЛЕ клика: регистрации, каналы, генерации, тарифы, оплаты —
при условии что UTM/start-параметры корректно сохраняются.

Этот модуль:
1. Читает из payment_path diagnostics поля source_breakdown (если AutoPost их отдаёт).
2. Если breakdown недоступен — формирует понятное объяснение для владельца.
3. Предоставляет рекомендации по UTM для eLama.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# UTM рекомендации для eLama / Telegram Ads
# ---------------------------------------------------------------------------

TELEGRAM_ADS_UTM_GUIDE = """Для запуска Telegram Ads через eLama нужно настроить два типа ссылок:

1. Ссылки на сайт (лендинг):
   utm_source=telegram_ads
   utm_medium=cpc
   utm_campaign=<название кампании>
   utm_content=<название объявления или ID>

   Пример:
   https://autopost26.up.railway.app/?utm_source=telegram_ads&utm_medium=cpc&utm_campaign=autopost_june&utm_content=post_generator_v1

2. Ссылки прямо в Telegram-бота (start-параметр):
   https://t.me/Trpst_bot?start=tgads_<campaign>_<creative>

   Пример:
   https://t.me/Trpst_bot?start=tgads_autopost_june_post_generator_v1

   Требования к AutoPost: при старте бота с параметром tgads_* нужно
   сохранить источник в пользователе (referral_source = "telegram_ads",
   campaign = <campaign>, creative = <creative>).

Что AutoPost должен сохранять для source breakdown:
- При регистрации: utm_source или referral_source
- При создании канала: можно унаследовать от регистрации
- При генерации поста: аналогично
- При просмотре тарифов: аналогично
- При оплате: аналогично

Без этого Growth Agent видит только суммарные числа, без разбивки по источникам.
"""

KNOWN_SOURCES = {
    "yandex_direct": "Яндекс.Директ",
    "yandex": "Яндекс.Директ",
    "direct": "Яндекс.Директ",
    "ya_direct": "Яндекс.Директ",
    "telegram_ads": "Telegram Ads",
    "tgads": "Telegram Ads",
    "telegram": "Telegram",
    "organic": "Органика",
    "referral": "Реферал",
    "unknown": "Неизвестный источник",
}

# Технические/служебные ключи, которые не должны попадать в owner-facing
# вывод как отдельный источник — это не реальный канал трафика, а
# "мусорная корзина" для несопоставленных событий. Показываем их только
# если там реально есть данные (regs/channels/gens > 0).
_TECHNICAL_BUCKET_KEYS = frozenset(["other", "none", "null", "", "n/a", "na"])

# Порядок вывода: сначала платные каналы (Telegram Ads, Яндекс.Директ),
# потом органика/реферал, потом unknown в самом конце.
_SOURCE_DISPLAY_ORDER = [
    "Telegram Ads",
    "Яндекс.Директ",
    "Telegram",
    "Органика",
    "Реферал",
]


def _normalize_source_label(source_key: str) -> str:
    """Нормализует ключ источника в человекочитаемую метку через KNOWN_SOURCES."""
    key = (source_key or "").strip().lower()
    return KNOWN_SOURCES.get(key, source_key)


def _is_empty_bucket(data: dict) -> bool:
    """True если у источника все ключевые метрики нулевые/None.
    post_generations НЕ учитывается — это техническая метрика
    (может включать автогенерацию системой), не пользовательская активность.
    """
    metrics = [
        data.get("registrations"), data.get("channels_created"),
        data.get("payment_started"), data.get("payment_success"),
    ]
    return all((m is None or m == 0) for m in metrics)


def parse_source_breakdown(payment_path: dict | None) -> dict[str, dict] | None:
    """
    Читает source_breakdown из payment_path diagnostics.

    Ожидаемый формат от AutoPost:
    {
      "source_breakdown": {
        "yandex_direct": {
          "registrations": 25, "channels_created": 20,
          "post_generations": 60, "pricing_viewed": 1,
          "payment_started": 0, "payment_success": 0
        },
        "telegram_ads": {
          "registrations": 5, "channels_created": 4,
          "post_generations": 12, "pricing_viewed": 0,
          "payment_started": 0, "payment_success": 0
        }
      }
    }

    Возвращает None если breakdown недоступен.
    """
    if not payment_path:
        return None
    breakdown = payment_path.get("source_breakdown")
    if not breakdown or not isinstance(breakdown, dict):
        return None
    return breakdown


def _aggregate_by_label(breakdown: dict[str, dict]) -> dict[str, dict]:
    """
    Группирует исходные ключи source_breakdown по нормализованной метке,
    суммируя метрики. Решает проблему дублей (yandex_direct + direct → один блок).

    Технические "мусорные" ключи (other/none/...) с нулевыми метриками
    исключаются. Если у такого ключа реально есть данные — он остаётся,
    но под своей нормализованной/исходной меткой.
    """
    SUM_FIELDS = [
        "registrations", "channels_created", "post_generations",
        "pricing_viewed", "payment_started", "payment_success",
    ]
    by_label: dict[str, dict] = {}
    pricing_seen: dict[str, bool] = {}  # отслеживаем был ли pricing_viewed хоть раз не-None

    for source_key, data in breakdown.items():
        if not isinstance(data, dict):
            continue

        normalized_key = (source_key or "").strip().lower()

        # Технический мусорный bucket с пустыми данными — пропускаем целиком
        if normalized_key in _TECHNICAL_BUCKET_KEYS and _is_empty_bucket(data):
            continue

        label = _normalize_source_label(source_key)

        if label not in by_label:
            by_label[label] = {f: 0 for f in SUM_FIELDS}
            pricing_seen[label] = False

        for field in SUM_FIELDS:
            value = data.get(field)
            if field == "pricing_viewed":
                if value is not None:
                    pricing_seen[label] = True
                    by_label[label][field] = (by_label[label][field] or 0) + (value or 0)
            else:
                by_label[label][field] = (by_label[label][field] or 0) + (value or 0)

        by_label[label]["_is_telegram_ads"] = by_label[label].get("_is_telegram_ads") or (
            normalized_key in ("telegram_ads", "tgads")
        )

    # Если pricing_viewed никогда не встречался как не-None — оставляем None
    # (отличие "0 просмотров" от "событие не отслеживается")
    for label in by_label:
        if not pricing_seen.get(label):
            by_label[label]["pricing_viewed"] = None

    return by_label


def format_source_breakdown(breakdown: dict[str, dict] | None, total_pp: dict | None) -> str:
    """
    Форматирует source breakdown для /funnel или /today.
    Если breakdown недоступен — объясняет что нужно сделать.

    Правила:
    — алиасы одного источника (yandex_direct/direct/ya_direct) объединяются
      в один блок "Яндекс.Директ" с суммированными метриками;
    — пустые технические bucket'ы (other/none с нулями) скрываются;
    — "Неизвестный источник" (unknown) всегда показывается, даже с нулями —
      это легитимный источник (старые пользователи до attribution tracking);
    — порядок: Telegram Ads, Яндекс.Директ, остальные платные/органика, unknown в конце.
    """
    if not breakdown:
        return _format_no_breakdown(total_pp)

    aggregated = _aggregate_by_label(breakdown)
    if not aggregated:
        return _format_no_breakdown(total_pp)

    # Сортируем по заданному порядку, unknown и прочее — в конец
    def _sort_key(label: str) -> tuple:
        if label in _SOURCE_DISPLAY_ORDER:
            return (0, _SOURCE_DISPLAY_ORDER.index(label))
        if label == "Неизвестный источник":
            return (2, 0)
        return (1, label)

    ordered_labels = sorted(aggregated.keys(), key=_sort_key)

    lines = ["\nИсточники трафика:"]
    for label in ordered_labels:
        data = aggregated[label]
        regs = data.get("registrations", 0) or 0
        channels = data.get("channels_created", 0) or 0
        pricing = data.get("pricing_viewed")
        started = data.get("payment_started", 0) or 0
        success = data.get("payment_success", 0) or 0

        lines.append(f"\n{label}:")
        lines.append(f"— регистраций: {regs}")
        lines.append(f"— создали канал: {channels}")
        if pricing is not None:
            lines.append(f"— открытий тарифов: {pricing}")
        lines.append(f"— попыток оплаты: {started}")
        lines.append(f"— успешных оплат: {success}")

        if data.get("_is_telegram_ads"):
            lines.append(
                "  (клики Telegram Ads берутся из рекламного кабинета eLama, "
                "в продукте видим только события после клика)"
            )

    return "\n".join(lines)


def _format_no_breakdown(total_pp: dict | None) -> str:
    """
    Сообщение когда source breakdown ещё не настроен.
    """
    lines = ["\nИсточники трафика:"]
    lines.append("Разбивка по источникам пока недоступна.")
    lines.append(
        "AutoPost не сохраняет utm_source при регистрации — "
        "все события суммируются без привязки к каналу."
    )
    lines.append(
        "\nЧтобы видеть Яндекс.Директ и Telegram Ads отдельно, "
        "AutoPost должен сохранять utm_source (или start-параметр бота) "
        "при регистрации пользователя."
    )
    return "\n".join(lines)
