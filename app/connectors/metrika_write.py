"""
Запись в Яндекс.Метрику через Management API (задача F6).

Не путать с `app/connectors/metrika.py` -- тот читает статистику (Reports
API), только чтение, и токен для него нужен с правом только на чтение.
Здесь -- создание/правка целей (Management API), для которого нужен
ОТДЕЛЬНЫЙ OAuth-токен с правом "Управление счётчиками". Разделены
намеренно: спутать read- и write-токен значило бы либо не суметь писать
(токен слабый), либо выдать боту куда больше прав, чем нужно для простого
чтения статистики.

Токен для записи можно задать двумя способами: `METRIKA_MANAGEMENT_TOKEN`
в окружении (как `YANDEX_OAUTH_TOKEN` сейчас) -- предпочтительно, секрет
не проходит через интерфейс/чат -- либо `Project.settings_json
["metrika_management_token"]`, если понадобится другой токен на конкретный
проект. Настройка проекта, если задана, побеждает env (тот же порядок
приоритета, что у per-project override в scheduler.py).

Пока владелец не выдал такой токен, `is_configured()` возвращает False, и
всё, что пытается писать, получает честную ошибку вместо тихого no-op --
см. принцип "честность данных" в PRODUCT_ROADMAP.md.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

import httpx

logger = logging.getLogger("growth_agent.connectors.metrika_write")

MANAGEMENT_API_URL = "https://api-metrika.yandex.net/management/v1"


class MetrikaWriteError(RuntimeError):
    pass


def _management_token(project) -> Optional[str]:
    """Токен для записи -- сначала настройка проекта, иначе
    METRIKA_MANAGEMENT_TOKEN из окружения (тот же паттерн, что у
    effective_direct_oauth_token в app/config.py). Токен нарочно можно
    задать через env, а не только через интерфейс -- секрет не должен
    ходить через чат/UI, если владелец не хочет."""
    from app.config import get_settings

    project_token = (project.settings_json or {}).get("metrika_management_token")
    return project_token or get_settings().metrika_management_token


def is_configured(project) -> bool:
    return bool(_management_token(project) and _counter_id(project))


def _headers(project) -> dict[str, str]:
    token = _management_token(project)
    return {
        "Authorization": f"OAuth {token}",
        "Content-Type": "application/json",
        # Идемпотентность на своей стороне: пишем её в тело диагностики
        # AgentAction, а не полагаемся на поддержку заголовка Метрикой --
        # Management API её не поддерживает, поэтому явно не создаём одну
        # и ту же цель дважды на уровне вызывающего кода (см. marketer_actions.py).
        "X-Request-Id": str(uuid.uuid4()),
    }


def _counter_id(project) -> str:
    from app.config import get_settings

    project_counter = (project.settings_json or {}).get("metrika_counter_id")
    return str(project_counter or get_settings().metrika_counter_id or "")


async def list_goals(project, timeout_seconds: float = 15.0) -> list[dict[str, Any]]:
    """Текущие цели счётчика -- нужно перед созданием новой, чтобы не
    плодить дубликаты, и после записи, чтобы проверить, что она реально
    произошла (вебхуку/ответу самой Метрики доверяем меньше, чем повторному
    чтению -- тот же принцип, что в billing_platform.py)."""
    if not is_configured(project):
        raise MetrikaWriteError(
            "Запись в Метрику не настроена: нужен metrika_management_token "
            "с правом «Управление счётчиками» и metrika_counter_id у проекта."
        )
    url = f"{MANAGEMENT_API_URL}/counter/{_counter_id(project)}/goals"
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        try:
            resp = await client.get(url, headers=_headers(project))
        except httpx.HTTPError as exc:
            raise MetrikaWriteError(f"Не удалось обратиться к Метрике: {exc}") from exc
    if resp.status_code >= 400:
        raise MetrikaWriteError(_extract_error(resp))
    return resp.json().get("goals", [])


async def create_goal(project, *, name: str, goal_type: str, conditions: list[dict],
                       timeout_seconds: float = 15.0) -> dict[str, Any]:
    """Создаёт новую цель. `goal_type`/`conditions` -- в формате Management
    API (например, `url`/`[{"type": "contain", "url": "/success"}]`)."""
    if not is_configured(project):
        raise MetrikaWriteError(
            "Запись в Метрику не настроена: нужен metrika_management_token "
            "с правом «Управление счётчиками» и metrika_counter_id у проекта."
        )
    url = f"{MANAGEMENT_API_URL}/counter/{_counter_id(project)}/goals"
    payload = {"goal": {"name": name[:60], "type": goal_type, "conditions": conditions}}
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        try:
            resp = await client.post(url, headers=_headers(project), json=payload)
        except httpx.HTTPError as exc:
            raise MetrikaWriteError(f"Не удалось создать цель в Метрике: {exc}") from exc
    if resp.status_code >= 400:
        logger.warning("metrika_write: create_goal error %s: %s", resp.status_code, resp.text[:300])
        raise MetrikaWriteError(_extract_error(resp))
    return resp.json().get("goal", {})


async def update_goal(project, goal_id: int, *, name: str | None = None,
                       conditions: list[dict] | None = None,
                       timeout_seconds: float = 15.0) -> dict[str, Any]:
    if not is_configured(project):
        raise MetrikaWriteError(
            "Запись в Метрику не настроена: нужен metrika_management_token "
            "с правом «Управление счётчиками» и metrika_counter_id у проекта."
        )
    url = f"{MANAGEMENT_API_URL}/counter/{_counter_id(project)}/goals/{goal_id}"
    goal: dict[str, Any] = {}
    if name is not None:
        goal["name"] = name[:60]
    if conditions is not None:
        goal["conditions"] = conditions
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        try:
            resp = await client.put(url, headers=_headers(project), json={"goal": goal})
        except httpx.HTTPError as exc:
            raise MetrikaWriteError(f"Не удалось изменить цель в Метрике: {exc}") from exc
    if resp.status_code >= 400:
        logger.warning("metrika_write: update_goal error %s: %s", resp.status_code, resp.text[:300])
        raise MetrikaWriteError(_extract_error(resp))
    return resp.json().get("goal", {})


def _extract_error(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except Exception:
        return f"Ошибка Метрики: HTTP {resp.status_code}"
    msg = data.get("message") or data.get("errors")
    return f"Ошибка Метрики: {msg}" if msg else f"Ошибка Метрики: HTTP {resp.status_code}"
