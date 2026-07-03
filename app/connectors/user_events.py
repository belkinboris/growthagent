"""
User-events connector для TruePost internal API (Founder Live Feed).

GET /api/internal/user-events?period_minutes=120&limit=200

В отличие от user_journeys.py (снимок текущего состояния каждого пути),
этот endpoint должен отдавать ДИСКРЕТНЫЕ СОБЫТИЯ с event_id -- что
позволяет дедуплицировать по event_id напрямую, без построения
event_key из timestamp полей.

TODO(TruePost): если этот endpoint ещё не задеплоен, fetch_user_events
вернёт {"ok": False, "status": "not_found"} и вызывающий код (scheduler)
должен продолжать использовать user_journeys.py / notifications.py
(per-user snapshot diffing) как временную замену.

Контракт (см. задачу "Founder Live Feed"):
{
  "ok": true,
  "events": [
    {
      "event_id": "...",
      "event_type": "user_registered" | "channel_created" |
                     "first_post_feedback_good" | "first_post_feedback_bad" |
                     "pricing_viewed" | "payment_cta_clicked" |
                     "payment_started" | "payment_success" | "payment_failed",
      "user_key": "u_...",
      "source": "...",
      "utm_source": "...", "utm_campaign": "...", "utm_content": "...",
      "created_at": "...",
      "journey_snapshot": {
        "registered": bool, "channel_created": bool,
        "onboarding_choice": str|None,
        "first_post_feedback": "good"|"bad"|None,
        "first_post_feedback_reason": str|None,
        "pricing_viewed": bool, "payment_cta_clicked": bool,
        "payment_started": bool, "payment_success": bool,
        "payment_failed": bool, "stuck_at": str|None,
      }
    }
  ]
}

Ни один auto-created post событие НЕ входит в этот контракт -- raw
post_generations/auto-generation НЕ являются user-events по определению
задачи. Если TruePost когда-либо добавит такое событие, connector должен
явно его игнорировать (см. _IGNORED_EVENT_TYPES).
"""
from __future__ import annotations

from typing import Optional


# Типы событий, которые НИКОГДА не должны попадать в Founder Live Feed,
# даже если TruePost их когда-нибудь пришлёт -- это защита на уровне
# connector-а, а не только на уровне форматирования уведомлений.
_IGNORED_EVENT_TYPES = frozenset([
    "post_generated", "post_generation", "auto_post_created",
    "system_post_created", "scheduled_post_created",
])

_EXPECTED_EVENT_TYPES = frozenset([
    "user_registered", "channel_created",
    "onboarding_choice",
    "first_post_feedback_good", "first_post_feedback_bad",
    "pricing_viewed", "payment_cta_clicked",
    "payment_started", "payment_success", "payment_failed",
])


def _normalize_event(raw: dict) -> Optional[dict]:
    """
    Нормализует одно событие. Возвращает None, если событие нужно
    отфильтровать (нет event_id/user_key, или это игнорируемый тип).
    """
    event_id = raw.get("event_id")
    user_key = raw.get("user_key")
    event_type = raw.get("event_type")

    if not event_id or not user_key or not event_type:
        return None
    if event_type in _IGNORED_EVENT_TYPES:
        return None

    return {
        "event_id": event_id,
        "event_type": event_type,
        "user_key": user_key,
        "source": raw.get("source"),
        "utm_source": raw.get("utm_source"),
        "utm_campaign": raw.get("utm_campaign"),
        "utm_content": raw.get("utm_content"),
        "created_at": raw.get("created_at"),
        "journey_snapshot": raw.get("journey_snapshot") or {},
    }


async def fetch_user_events(
    base_url: Optional[str],
    api_token: Optional[str],
    period_minutes: int = 120,
    limit: int = 200,
    timeout_seconds: float = 10.0,
) -> dict:
    """
    Запрашивает дискретные user-events у TruePost internal API.

    Возвращает {"ok": True, "events": [...]} при успехе,
    {"ok": False, "events": [], "error": str, "status": str} при провале.
    Никогда не бросает исключение наружу -- вызывающий код должен падать
    обратно на user_journeys.py при status != "ok".
    """
    if not base_url or not api_token:
        return {"ok": False, "events": [], "error": "not_configured", "status": "not_configured"}

    import httpx

    url = f"{base_url.rstrip('/')}/api/internal/user-events"
    params = {"period_minutes": period_minutes, "limit": limit}
    headers = {"Authorization": f"Bearer {api_token}"}

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.get(url, params=params, headers=headers)
    except httpx.TimeoutException:
        return {"ok": False, "events": [], "error": f"timeout after {timeout_seconds:.0f}s", "status": "timeout"}
    except Exception as exc:
        return {"ok": False, "events": [], "error": f"{type(exc).__name__}: {exc}", "status": "request_error"}

    if resp.status_code == 404:
        return {
            "ok": False, "events": [],
            "error": "endpoint not found (404) -- TruePost may not have deployed it yet",
            "status": "not_found",
        }
    if resp.status_code >= 500:
        return {"ok": False, "events": [], "error": f"TruePost server error: HTTP {resp.status_code}", "status": "server_error"}
    if resp.status_code != 200:
        return {"ok": False, "events": [], "error": f"unexpected HTTP {resp.status_code}", "status": "http_error"}

    try:
        data = resp.json()
    except Exception as exc:
        return {"ok": False, "events": [], "error": f"invalid JSON: {exc}", "status": "bad_format"}

    if not isinstance(data, dict) or not data.get("ok"):
        return {"ok": False, "events": [], "error": "TruePost returned ok=false or unexpected shape", "status": "bad_format"}

    raw_events = data.get("events")
    if not isinstance(raw_events, list):
        return {"ok": False, "events": [], "error": "events field missing or not a list", "status": "bad_format"}

    events: list[dict] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        normalized = _normalize_event(raw)
        if normalized is not None:
            events.append(normalized)

    return {"ok": True, "events": events, "error": None, "status": "ok"}
