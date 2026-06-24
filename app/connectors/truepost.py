"""
Connector для TruePost / АвтоПост.

Это ПЕРВЫЙ адаптер Project Metrics API, не основа архитектуры. Всё, что
специфично для TruePost (названия полей users_created, channels_created
и т.д.), живёт только здесь. Наружу (в analyzer.py, rules.py, llm.py)
отдаются только нормализованные ключи воронки.
"""

from datetime import datetime, timezone
from typing import Optional

import httpx

from app.config import CORE_FUNNEL_KEYS


# Mapping по умолчанию для TruePost. Используется при первом создании
# Project в БД (см. db.py). После этого источник правды -- то, что лежит
# в Project.settings_json["funnel_mapping"], а не эта константа.
DEFAULT_FUNNEL_MAPPING = {
    "signup": "users_created",
    "activation_1": "channels_created",
    "activation_2": "posts_generated",
    "payment_started": "payments_started",
    "payment_success": "payments_success",
    "revenue": "revenue_rub",
}


class TruePostConnectorError(Exception):
    pass


async def fetch_metrics(
    base_url: str,
    api_token: str,
    period_hours: int,
    funnel_mapping: dict,
    timeout_seconds: float = 15.0,
) -> dict:
    """
    Запрашивает метрики у TruePost за указанное окно и переводит их в
    нормализованные ключи воронки через funnel_mapping. Возвращает dict с
    нормализованными ключами + "_raw" с исходным ответом + "as_of".

    Бросает TruePostConnectorError при сетевой ошибке, таймауте или
    неожиданном формате ответа -- вызывающий код (scheduler.py) должен
    перехватывать это и помечать Integration.status = error, НЕ создавать
    бизнес-алерт на основе отсутствующих данных.
    """
    url = f"{base_url.rstrip('/')}/api/internal/metrics"
    headers = {"Authorization": f"Bearer {api_token}"}
    params = {"period_hours": period_hours}

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            raw = response.json()
    except httpx.HTTPError as exc:
        raise TruePostConnectorError(f"HTTP error calling TruePost: {exc}") from exc
    except ValueError as exc:
        raise TruePostConnectorError(f"Invalid JSON from TruePost: {exc}") from exc

    if "as_of" not in raw:
        raise TruePostConnectorError("TruePost response missing required field 'as_of'")

    as_of: Optional[datetime] = None
    try:
        as_of = datetime.fromisoformat(raw["as_of"].replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise TruePostConnectorError(f"Invalid 'as_of' format: {exc}") from exc

    normalized: dict = {}
    for norm_key, raw_key in funnel_mapping.items():
        if raw_key in raw:
            normalized[norm_key] = raw[raw_key]

    # traffic не приходит из TruePost -- он приходит из Директа/Метрики.
    # Здесь его не заполняем, это явная нулевая зона ответственности коннектора.
    normalized.setdefault("traffic", None)

    # pending_payments -- не входит в базовую нормализованную воронку
    # (CORE_FUNNEL_KEYS), но важен для analyzer.py (правило pending_payments).
    # Сохраняем как дополнительное поле, не нормализованное, рядом с _raw.
    pending_payments = raw.get("pending_payments")

    return {
        **{k: normalized.get(k) for k in CORE_FUNNEL_KEYS},
        "pending_payments": pending_payments,
        "as_of": as_of,
        "_raw": raw,
    }
