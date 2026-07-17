"""
Per-user journeys connector для TruePost internal API.

GET /api/internal/user-journeys?period_hours=24&limit=100

Возвращает анонимные пользовательские пути (user_key, не PII) с временными
метками каждого шага воронки. В отличие от payment_path connector (только
агрегаты), этот endpoint даёт per-user детализацию -- можно слать
уведомления про конкретного (анонимного) пользователя, а не только дельты
суммарных счётчиков.

Контракт TruePost (см. задачу): GET возвращает
{"ok": bool, "period_hours": int, "as_of": str, "journeys": [...]}.

user_key -- анонимный идентификатор (например "u_febdae54"), НЕ telegram_id,
НЕ username, НЕ email. Connector ничего не делает с PII -- если TruePost
когда-либо начнёт передавать настоящие идентификаторы, это ответственность
TruePost; здесь принимается как есть.
"""
from __future__ import annotations

from typing import Optional


class UserJourneysConnectorError(Exception):
    """Базовая ошибка connector-а (HTTP, timeout, неожиданный формат)."""


class UserJourneysNotConfigured(UserJourneysConnectorError):
    """Endpoint не настроен (нет base_url/token) -- не ошибка, а отсутствие конфигурации."""


_EXPECTED_JOURNEY_FIELDS = [
    "user_key", "source", "utm_source", "utm_campaign", "utm_content",
    "registered_at", "channel_created_at",
    "onboarding_choice", "first_post_feedback", "first_post_feedback_reason",
    "first_post_feedback_at",
    "pricing_viewed_at", "payment_cta_clicked_at",
    "payment_started_at", "payment_success_at", "payment_failed_at",
    "last_step", "stuck_at", "minutes_since_last_step",
]


def _normalize_journey(raw: dict) -> dict:
    """
    Нормализует одну запись journey -- защита от отсутствующих полей.
    Не падает на неожиданном формате одной записи (пропускает её выше).
    """
    return {field: raw.get(field) for field in _EXPECTED_JOURNEY_FIELDS}


async def fetch_user_journeys(
    base_url: Optional[str],
    api_token: Optional[str],
    period_hours: int = 24,
    limit: int = 100,
    timeout_seconds: float = 10.0,
) -> dict:
    """
    Запрашивает per-user journeys у TruePost internal API.

    Возвращает {"ok": True, "journeys": [...], "as_of": str, "period_hours": int}
    при успехе.

    Возвращает {"ok": False, "journeys": [], "error": str, "status": "..."}
    при любой проблеме (not_configured / timeout / http_error / bad_format) --
    НИКОГДА не бросает исключение наружу. Вызывающий код (scheduler) должен
    проверять result["ok"] и при False -- падать обратно на delta notifications
    (fallback), не на исключения.
    """
    if not base_url or not api_token:
        return {
            "ok": False, "journeys": [], "as_of": None, "period_hours": period_hours,
            "error": "not_configured", "status": "not_configured",
        }

    import httpx

    url = f"{base_url.rstrip('/')}/api/internal/user-journeys"
    params = {"period_hours": period_hours, "limit": limit}
    headers = {"Authorization": f"Bearer {api_token}"}

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.get(url, params=params, headers=headers)
    except httpx.TimeoutException:
        return {
            "ok": False, "journeys": [], "as_of": None, "period_hours": period_hours,
            "error": f"timeout after {timeout_seconds:.0f}s", "status": "timeout",
        }
    except Exception as exc:
        return {
            "ok": False, "journeys": [], "as_of": None, "period_hours": period_hours,
            "error": f"{type(exc).__name__}: {exc}", "status": "request_error",
        }

    if resp.status_code == 404:
        return {
            "ok": False, "journeys": [], "as_of": None, "period_hours": period_hours,
            "error": "endpoint not found (404) -- TruePost may not have deployed it yet",
            "status": "not_found",
        }
    if resp.status_code >= 500:
        return {
            "ok": False, "journeys": [], "as_of": None, "period_hours": period_hours,
            "error": f"TruePost server error: HTTP {resp.status_code}", "status": "server_error",
        }
    if resp.status_code != 200:
        return {
            "ok": False, "journeys": [], "as_of": None, "period_hours": period_hours,
            "error": f"unexpected HTTP {resp.status_code}", "status": "http_error",
        }

    try:
        data = resp.json()
    except Exception as exc:
        return {
            "ok": False, "journeys": [], "as_of": None, "period_hours": period_hours,
            "error": f"invalid JSON: {exc}", "status": "bad_format",
        }

    if not isinstance(data, dict) or not data.get("ok"):
        return {
            "ok": False, "journeys": [], "as_of": None, "period_hours": period_hours,
            "error": "TruePost returned ok=false or unexpected shape", "status": "bad_format",
        }

    raw_journeys = data.get("journeys")
    if not isinstance(raw_journeys, list):
        return {
            "ok": False, "journeys": [], "as_of": None, "period_hours": period_hours,
            "error": "journeys field missing or not a list", "status": "bad_format",
        }

    journeys: list[dict] = []
    for raw in raw_journeys:
        if not isinstance(raw, dict) or not raw.get("user_key"):
            continue  # пропускаем некорректные записи, не падаем целиком
        journeys.append(_normalize_journey(raw))

    return {
        "ok": True,
        "journeys": journeys,
        "as_of": data.get("as_of"),
        "period_hours": data.get("period_hours", period_hours),
        "error": None,
        "status": "ok",
    }
