"""
Connector для payment-path diagnostics endpoint TruePost.

Запрашивает GET /api/internal/payment-path-diagnostics?period_hours=N
и возвращает сырые данные по воронке до оплаты. Никакой бизнес-логики
здесь нет -- только транспорт и минимальная валидация ответа.

По архитектуре симметричен connectors/landing.py:
- NotConfiguredError -- product connector не настроен (нет base_url/token)
- PaymentPathConnectorError -- endpoint существует, но не ответил штатно

Endpoint добавлен в TruePost специально для этой задачи. Если не задеплоен
и вернёт 404/500/connection error -- PaymentPathConnectorError, /run
продолжает работу без блока "Путь до оплаты".

Field names из контракта AutoPost endpoint (задача):
  registrations, channels_created, post_generations,
  pricing_viewed, payment_cta_clicked, payment_started,
  payment_success, payment_failed, payment_returned,
  quota_warning_seen, limit_reached,
  biggest_dropoff, likely_explanation, missing_data,
  conversion_steps, event_map.

Алиасы (_FIELD_ALIASES ниже) обрабатывают расхождения имён на случай
если реальный AutoPost endpoint использует немного другие имена.
"""

import logging
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger("growth_agent.connectors.payment_path")


class PaymentPathConnectorError(Exception):
    """Сетевая ошибка, timeout, невалидный ответ -- endpoint существует, но не ответил штатно."""
    pass


class NotConfiguredError(Exception):
    """Product connector не настроен (нет base_url/token)."""
    pass


# Канонические имена полей, которые ожидаем в ответе.
# Каждое поле опциональное (None если не заполнено endpoint'ом).
_EXPECTED_FIELDS = [
    # Основная воронка (существующие)
    "registrations",
    "channels_created",
    "post_generations",
    "pricing_viewed",
    "payment_cta_clicked",
    "payment_started",
    "payment_success",
    "payment_failed",
    "payment_returned",
    "quota_warning_seen",
    "limit_reached",
    # Onboarding choice (новые, 2026-06-28)
    "onboarding_choice_counts",   # dict: {"generate_post": N, "analyze_channel": N, "skip": N}
    # First post feedback (новые, 2026-06-28)
    "first_post_feedback_good",   # int
    "first_post_feedback_bad",    # int
    "first_post_feedback_reasons", # dict: {"too_generic": N, "wrong_style": N, ...}
    # Breakdown по верификации канала (новые, 2026-06-28)
    "post_generations_verified",    # int — генерации у подключённых каналов
    "post_generations_unverified",  # int — генерации у неподключённых каналов
    # Source breakdown (новые, 2026-06-29) — разбивка по traffic source
    "source_breakdown",   # dict: {"yandex_direct": {...}, "telegram_ads": {...}, "unknown": {...}}
    # Мост к тарифам «очередь на неделю» (новые, 2026-07-07)
    "queue_offer_shown",    # int — сколько раз блок показан после good feedback
    "queue_offer_clicked",  # int — сколько раз нажали «Собрать очередь»
]

_FIELD_ALIASES: dict[str, list[str]] = {
    "post_generations":    ["posts_generated", "post_generated", "generations"],
    "pricing_viewed":      ["pricing_views", "tariff_views", "tariff_viewed"],
    "payment_cta_clicked": ["payment_cta_clicks", "payment_button_clicked", "cta_clicked"],
    "payment_started":     ["payments_started", "payment_attempts"],
    "payment_success":     ["payments_success", "payments_succeeded", "successful_payments"],
    "payment_failed":      ["payments_failed", "payment_failures"],
    "payment_returned":    ["payments_returned"],
    # Новые поля — возможные альтернативные имена
    "onboarding_choice_counts":     ["onboarding_choices", "choice_counts"],
    "first_post_feedback_good":     ["feedback_good", "feedback_positive"],
    "first_post_feedback_bad":      ["feedback_bad", "feedback_negative"],
    "first_post_feedback_reasons":  ["feedback_reasons", "bad_feedback_reasons"],
    "post_generations_verified":    ["generations_verified", "verified_channel_generations"],
    "post_generations_unverified":  ["generations_unverified", "unverified_channel_generations"],
    "source_breakdown":             ["traffic_sources", "by_source", "source_stats"],
}


def _resolve_field(raw: dict, canonical: str) -> object:
    """
    Возвращает значение поля из raw по canonical имени или алиасам.
    Возвращает None если не найдено ни одного варианта.
    """
    if canonical in raw:
        return raw[canonical]
    for alias in _FIELD_ALIASES.get(canonical, []):
        if alias in raw:
            logger.debug(
                "Payment-path: field '%s' resolved via alias '%s'", canonical, alias
            )
            return raw[alias]
    return None


async def fetch_payment_path_diagnostics(
    base_url: Optional[str],
    api_token: Optional[str],
    period_hours: int = 168,
    timeout_seconds: float = 15.0,
) -> dict:
    """
    Запрашивает payment-path diagnostics у TruePost.

    Возвращает dict с полями из _EXPECTED_FIELDS (каждое может быть None,
    если endpoint не смог его заполнить или поле отсутствует), плюс:
      - "as_of": datetime (tz-aware)
      - "biggest_dropoff": str | None
      - "likely_explanation": str | None
      - "missing_data": list[str]
      - "conversion_steps": list[dict] | None
      - "event_map": dict | None
      - "_raw": исходный JSON ответа

    Бросает NotConfiguredError, если token/base_url не заданы.
    Бросает PaymentPathConnectorError при сетевой ошибке/таймауте/
    невалидном JSON / HTTP != 200 / отсутствии обязательного as_of.
    """
    if not base_url or not api_token:
        raise NotConfiguredError("Product base_url or internal API token not configured")

    url = f"{base_url.rstrip('/')}/api/internal/payment-path-diagnostics"
    headers = {"Authorization": f"Bearer {api_token}"}
    params = {"period_hours": period_hours}

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(url, headers=headers, params=params)
    except httpx.TimeoutException as exc:
        raise PaymentPathConnectorError(
            f"Timeout calling payment-path diagnostics endpoint: {exc}"
        ) from exc
    except httpx.HTTPError as exc:
        raise PaymentPathConnectorError(
            f"HTTP error calling payment-path diagnostics endpoint: {exc}"
        ) from exc

    if response.status_code != 200:
        raise PaymentPathConnectorError(
            f"Payment-path diagnostics endpoint returned HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )

    try:
        raw = response.json()
    except ValueError as exc:
        raise PaymentPathConnectorError(
            f"Invalid JSON from payment-path diagnostics endpoint: {exc}"
        ) from exc

    # as_of -- единственное обязательное поле для оценки свежести данных
    if "as_of" not in raw:
        raise PaymentPathConnectorError(
            "Payment-path diagnostics response missing required field 'as_of'"
        )

    try:
        as_of = datetime.fromisoformat(raw["as_of"].replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise PaymentPathConnectorError(f"Invalid 'as_of' format: {exc}") from exc

    # Собираем поля воронки через canonical names + aliases
    values: dict = {}
    missing_in_response: list[str] = []
    for field in _EXPECTED_FIELDS:
        resolved = _resolve_field(raw, field)
        if resolved is None and field not in raw:
            # Поле не найдено ни как canonical, ни как alias
            missing_in_response.append(field)
        values[field] = resolved

    if missing_in_response:
        # Не падаем -- лёгкий warning, пустые поля дадут None в отчёте
        logger.warning(
            "Payment-path diagnostics response missing fields (checked aliases too): %s",
            missing_in_response,
        )

    return {
        **values,
        "as_of": as_of,
        "biggest_dropoff": raw.get("biggest_dropoff"),
        "likely_explanation": raw.get("likely_explanation"),
        "missing_data": raw.get("missing_data") or [],
        "conversion_steps": raw.get("conversion_steps"),
        "event_map": raw.get("event_map"),
        "_raw": raw,
    }