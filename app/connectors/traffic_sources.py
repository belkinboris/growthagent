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
    "telegram_ads": "Telegram Ads",
    "tgads": "Telegram Ads",
    "telegram": "Telegram",
    "organic": "Органика",
    "referral": "Реферал",
    "unknown": "Неизвестный источник",
}


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


def format_source_breakdown(breakdown: dict[str, dict] | None, total_pp: dict | None) -> str:
    """
    Форматирует source breakdown для /funnel или /today.
    Если breakdown недоступен — объясняет что нужно сделать.
    """
    if not breakdown:
        return _format_no_breakdown(total_pp)

    lines = ["\nИсточники трафика:"]
    for source_key, data in breakdown.items():
        label = KNOWN_SOURCES.get(source_key, source_key)
        regs = data.get("registrations", 0) or 0
        channels = data.get("channels_created", 0) or 0
        gens = data.get("post_generations", 0) or 0
        pricing = data.get("pricing_viewed")
        started = data.get("payment_started", 0) or 0
        success = data.get("payment_success", 0) or 0

        lines.append(f"\n{label}:")
        lines.append(f"— регистраций: {regs}")
        lines.append(f"— создали канал: {channels}")
        lines.append(f"— генераций постов: {gens}")
        if pricing is not None:
            lines.append(f"— открытий тарифов: {pricing}")
        lines.append(f"— попыток оплаты: {started}")
        lines.append(f"— успешных оплат: {success}")

        if source_key in ("telegram_ads", "tgads"):
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
