"""
Connector для landing funnel diagnostics endpoint TruePost.

В отличие от connectors/onboarding.py, этот endpoint УЖЕ существует и
протестирован в TruePost (не гипотетический контракт) -- поэтому здесь
нет NotAvailableError для "endpoint не реализован". Но graceful-обработка
сетевых ошибок/timeout/невалидного JSON всё равно нужна -- по тем же
причинам, что и для остальных коннекторов: эндпоинт может быть временно
недоступен, это не повод считать это бизнес-сигналом.

Контракт (см. задачу): GET /api/internal/landing-funnel-diagnostics
?period_hours=24, Authorization: Bearer {PROJECT_INTERNAL_API_TOKEN}.

Важно из контракта: для бизнес-диагностики используются значения БЕЗ
суффикса _raw (это уже посчитанные unique-метрики). Поля с _raw -- только
для предупреждения о дублях/проблемах трекинга (см. правило F в
diagnostics.py: "if raw сильно выше unique -- instrumentation warning").
"""

import logging
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger("growth_agent.connectors.landing")


class LandingConnectorError(Exception):
    """Сетевая ошибка, timeout, невалидный ответ -- endpoint существует, но не ответил штатно."""
    pass


class NotConfiguredError(Exception):
    """Product connector не настроен (нет base_url/token) -- не ошибка, источник просто отсутствует."""
    pass


# Поля, которые ожидаются в ответе -- пары (unique_key, raw_key). Используется
# и для парсинга ответа, и для расчёта raw/unique discrepancy (правило F).
_FUNNEL_FIELDS = [
    "landing_views",
    "cta_hero_bot_clicks",
    "cta_hero_app_clicks",
    "bot_starts_from_landing",
    "web_register_opened",
    "register_success",
    "activation_1",
]


async def fetch_landing_funnel_diagnostics(
    base_url: Optional[str],
    api_token: Optional[str],
    period_hours: int = 24,
    timeout_seconds: float = 15.0,
) -> dict:
    """
    Возвращает dict с unique-метриками воронки (без _raw в основных ключах),
    плюс "_raw_values" -- отдельный dict с raw-значениями для расчёта
    instrumentation warning, плюс "dropoff_summary"/"diagnostic_notes" из
    самого TruePost (передаются как есть, TruePost уже умеет их формулировать),
    плюс "as_of", "tracking_started_at" (или None, если TruePost это поле
    пока не отдаёт -- см. ниже).

    tracking_started_at -- момент, с которого landing tracking реально
    собирает данные (поле "tracking_started_at" или "first_event_at" в
    ответе, в порядке предпочтения). Это поле ЖЕЛАТЕЛЬНОЕ, не обязательное:
    TruePost может пока не отдавать его вовсе -- тогда tracking_started_at
    будет None, и вызывающий код (diagnostics.py) обязан считать это
    "зрелость трекинга неизвестна", не "трекинг зрелый". См. правило A в
    diagnostics.analyze_landing_funnel -- без этого поля сравнение Direct
    clicks (за весь period_hours) с landing_views (которые могли начать
    собираться позже) даёт ложный сигнал "переход с рекламы сломан".

    Бросает NotConfiguredError, если token/base_url не заданы.
    Бросает LandingConnectorError при сетевой ошибке/таймауте/невалидном
    JSON/отсутствии обязательных полей.
    """
    if not base_url or not api_token:
        raise NotConfiguredError("Product base_url or internal API token not configured")

    url = f"{base_url.rstrip('/')}/api/internal/landing-funnel-diagnostics"
    headers = {"Authorization": f"Bearer {api_token}"}
    params = {"period_hours": period_hours}

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(url, headers=headers, params=params)
    except httpx.TimeoutException as exc:
        raise LandingConnectorError(f"Timeout calling landing funnel diagnostics endpoint: {exc}") from exc
    except httpx.HTTPError as exc:
        raise LandingConnectorError(f"HTTP error calling landing funnel diagnostics endpoint: {exc}") from exc

    if response.status_code != 200:
        raise LandingConnectorError(
            f"Landing funnel diagnostics endpoint returned HTTP {response.status_code}: {response.text[:300]}"
        )

    try:
        raw = response.json()
    except ValueError as exc:
        raise LandingConnectorError(f"Invalid JSON from landing funnel diagnostics endpoint: {exc}") from exc

    if "as_of" not in raw:
        # Та же защита, что во всех остальных internal-коннекторах
        # TruePost -- без as_of нельзя доверять свежести данных.
        raise LandingConnectorError("Landing funnel diagnostics response missing required field 'as_of'")

    try:
        as_of = datetime.fromisoformat(raw["as_of"].replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise LandingConnectorError(f"Invalid 'as_of' format: {exc}") from exc

    # tracking_started_at -- ЖЕЛАТЕЛЬНОЕ поле, не обязательное (см. п.3
    # задачи: "желательно должен возвращать"). Принимаем оба варианта
    # названия поля, в порядке предпочтения, и graceful игнорируем, если
    # ни одного нет или формат невалиден -- это не повод ронять весь запрос,
    # просто зрелость трекинга останется неизвестной для diagnostics.py.
    tracking_started_at = None
    for field_name in ("tracking_started_at", "first_event_at", "earliest_landing_event_at"):
        if raw.get(field_name):
            try:
                tracking_started_at = datetime.fromisoformat(raw[field_name].replace("Z", "+00:00"))
                break
            except (ValueError, AttributeError):
                logger.warning("Invalid format for %s in landing funnel response: %r", field_name, raw.get(field_name))
                continue

    unique_values = {}
    raw_values = {}
    missing_fields = []

    for field in _FUNNEL_FIELDS:
        raw_key = f"{field}_raw"
        if field not in raw:
            missing_fields.append(field)
            unique_values[field] = None
        else:
            unique_values[field] = raw[field]
        raw_values[field] = raw.get(raw_key)

    if missing_fields:
        # Не падаем -- endpoint протестирован и работает, но если в будущем
        # поле выпадет из ответа (например, при доработке TruePost), лучше
        # явно залогировать и продолжить с None для этого поля, чем упасть
        # целиком и потерять весь отчёт по остальным полям.
        logger.warning("Landing funnel diagnostics response missing fields: %s", missing_fields)

    return {
        **unique_values,
        "_raw_values": raw_values,
        "dropoff_summary": raw.get("dropoff_summary"),
        "diagnostic_notes": raw.get("diagnostic_notes", []),
        "as_of": as_of,
        "tracking_started_at": tracking_started_at,
        "_raw": raw,
    }
