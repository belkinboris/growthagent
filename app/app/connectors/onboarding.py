"""
Connector для onboarding diagnostics endpoint TruePost.

TruePost сейчас (на момент написания) НЕ имеет этого endpoint. Это
ожидаемая, не аварийная ситуация -- весь модуль спроектирован так, чтобы
graceful обрабатывать его отсутствие, а не считать это ошибкой сервиса.

Контракт endpoint (см. examples/truepost_onboarding_diagnostics_patch.py
для готового кода, который можно вставить в TruePost):

GET /api/internal/onboarding-diagnostics?period_hours=24
Authorization: Bearer {PROJECT_INTERNAL_API_TOKEN}

Ожидаемый ответ -- см. NotAvailableError/OnboardingConnectorError ниже для
того, как различаются "endpoint не существует" (404 -- ожидаемо, не
ошибка) от "endpoint сломан" (500/timeout/bad JSON -- тоже не падаем, но
это технически другая ситуация, и текст пользователю должен быть другим).
"""

import logging
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger("growth_agent.connectors.onboarding")


class OnboardingConnectorError(Exception):
    """Endpoint существует, но ответил ошибкой (5xx, timeout, bad JSON)."""
    pass


class NotAvailableError(Exception):
    """
    Endpoint не реализован в TruePost (404) или продукт не настроен
    (нет base_url/token). Это ОЖИДАЕМАЯ ситуация на данный момент, не
    сбой -- TruePost ещё не имеет этого endpoint. Отличается от
    OnboardingConnectorError, чтобы вызывающий код мог сформулировать
    разные тексты: "endpoint ещё не реализован" vs "endpoint сломан".
    """
    pass


async def fetch_onboarding_diagnostics(
    base_url: Optional[str],
    api_token: Optional[str],
    period_hours: int = 24,
    timeout_seconds: float = 15.0,
) -> dict:
    """
    Возвращает dict с полями из контракта (registrations, onboarding_started,
    channels_created, dropoff_by_step, last_known_step_summary, notes, as_of).

    Бросает NotAvailableError при 404 или отсутствии base_url/token --
    это означает "endpoint не существует/не настроен", не "сломан".
    Бросает OnboardingConnectorError при любой другой ошибке (timeout,
    5xx, невалидный JSON, отсутствие обязательных полей в ответе).
    """
    if not base_url or not api_token:
        raise NotAvailableError("Product base_url or internal API token not configured")

    url = f"{base_url.rstrip('/')}/api/internal/onboarding-diagnostics"
    headers = {"Authorization": f"Bearer {api_token}"}
    params = {"period_hours": period_hours}

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(url, headers=headers, params=params)
    except httpx.TimeoutException as exc:
        raise OnboardingConnectorError(f"Timeout calling onboarding diagnostics endpoint: {exc}") from exc
    except httpx.HTTPError as exc:
        raise OnboardingConnectorError(f"HTTP error calling onboarding diagnostics endpoint: {exc}") from exc

    if response.status_code == 404:
        # Ожидаемо на данный момент -- endpoint просто не реализован в
        # TruePost. Не логируем как ошибку (не exception/warning), это
        # штатная, предвиденная ситуация.
        raise NotAvailableError("Onboarding diagnostics endpoint not implemented in TruePost (404)")

    if response.status_code != 200:
        raise OnboardingConnectorError(
            f"Onboarding diagnostics endpoint returned HTTP {response.status_code}: {response.text[:300]}"
        )

    try:
        raw = response.json()
    except ValueError as exc:
        raise OnboardingConnectorError(f"Invalid JSON from onboarding diagnostics endpoint: {exc}") from exc

    if "as_of" not in raw:
        # Та же защита, что в connectors/truepost.py для основного
        # internal metrics endpoint -- без as_of нельзя доверять
        # свежести данных, отсутствие этого поля считаем поломкой
        # контракта, не просто "недостающим необязательным полем".
        raise OnboardingConnectorError("Onboarding diagnostics response missing required field 'as_of'")

    try:
        as_of = datetime.fromisoformat(raw["as_of"].replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise OnboardingConnectorError(f"Invalid 'as_of' format: {exc}") from exc

    return {
        "registrations": raw.get("registrations", 0),
        "onboarding_started": raw.get("onboarding_started"),
        "create_channel_clicked": raw.get("create_channel_clicked"),
        "channels_created": raw.get("channels_created"),
        "channels_verified": raw.get("channels_verified"),
        "first_post_generated": raw.get("first_post_generated"),
        "payment_started": raw.get("payment_started"),
        "payment_success": raw.get("payment_success"),
        "errors_count": raw.get("errors_count"),
        "dropoff_by_step": raw.get("dropoff_by_step", []),
        "last_known_step_summary": raw.get("last_known_step_summary", {}),
        "notes": raw.get("notes", []),
        "as_of": as_of,
        "_raw": raw,
    }
