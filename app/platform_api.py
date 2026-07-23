"""
API веб-платформы Аналитика Воронки.

Всё, что раньше было доступно только через Telegram-бота, здесь доступно
через HTTP под общим префиксом (монтируется в main.py как /growth):

- вход владельца (см. platform_auth.py) -- без него ничего не видно;
- обзор: проект, интеграции, алерты, воронка по окнам 3h/24h/7d;
- ручной запуск цикла анализа;
- управление проектами: создание/редактирование/выбор активного,
  проверка подключения с автообнаружением доступных internal-endpoints;
- чат с аналитиком (тот же ask.py, что отвечал в Telegram).

Роутер самодостаточен: его можно include_router'ом подключить к любому
FastAPI-приложению (например, к Compass) -- см. COMPASS_INTEGRATION.md.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import select

from app.config import (
    ANALYSIS_WINDOWS_HOURS,
    BUILD_MARKER,
    CORE_FUNNEL_KEYS,
    RUN_CYCLE_TIMEOUT_SECONDS,
    get_settings,
)
from app.db import get_session, _ensure_integrations
from app.models import Alert, Integration, MetricSnapshot, Project
from app.platform_auth import (
    SESSION_COOKIE,
    issue_session_token,
    require_admin,
    verify_password,
)

logger = logging.getLogger("growth_agent.platform")

router = APIRouter()

PLATFORM_INDEX = Path(__file__).parent / "static" / "platform" / "index.html"

# Endpoints внутреннего API проекта, которые платформа умеет автообнаруживать
# при подключении. Обязателен только metrics -- остальные опциональны
# (контракт: CONTRACT.md). У каждого -- безопасные probe-параметры.
INTERNAL_ENDPOINT_PROBES: list[tuple[str, dict]] = [
    ("metrics", {"period_hours": 24}),
    ("payment-path-diagnostics", {"period_hours": 24}),
    ("landing-funnel-diagnostics", {"period_hours": 24}),
    ("onboarding-diagnostics", {"period_hours": 24}),
    ("user-journeys", {"period_hours": 24, "limit": 1}),
    ("user-events", {"period_minutes": 60, "limit": 1}),
]


def project_internal_api_token(project: Project) -> Optional[str]:
    """Токен внутреннего API проекта: сначала из настроек самого проекта
    (создан через платформу), иначе -- из env (legacy-путь v1)."""
    token = (project.settings_json or {}).get("internal_api_token")
    if token:
        return token
    return get_settings().project_internal_api_token


# ---------------------------------------------------------------------------
# Схемы запросов
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    password: str


class ConnectionTestRequest(BaseModel):
    base_url: str
    internal_api_token: str


class ProjectCreateRequest(BaseModel):
    # Обязательные поля -- минимум, который пользователь заполняет руками.
    name: str
    base_url: str
    internal_api_token: str
    # Опциональные -- со здравыми дефолтами, автозаполняются платформой.
    type: str = "telegram_saas"
    funnel_mapping: Optional[dict] = None
    # Внешние источники (можно добавить позже через PATCH)
    metrika_counter_id: Optional[str] = None
    direct_client_login: Optional[str] = None


class ProjectUpdateRequest(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    internal_api_token: Optional[str] = None
    type: Optional[str] = None
    funnel_mapping: Optional[dict] = None
    metrika_counter_id: Optional[str] = None
    direct_client_login: Optional[str] = None


class AskRequest(BaseModel):
    question: str


# ---------------------------------------------------------------------------
# Страница платформы + вход
# ---------------------------------------------------------------------------


@router.get("/", include_in_schema=False)
async def platform_index():
    return FileResponse(PLATFORM_INDEX)


@router.post("/api/login")
async def login(body: LoginRequest, response: Response):
    settings = get_settings()
    if not settings.platform_admin_password:
        raise HTTPException(status_code=503, detail="Платформа не настроена: задайте PLATFORM_ADMIN_PASSWORD")
    if not verify_password(body.password):
        raise HTTPException(status_code=401, detail="Неверный пароль")
    token = issue_session_token()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.platform_session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.platform_cookie_secure,
        path="/",
    )
    return {"ok": True, "token": token}


@router.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/api/session")
async def session_state(request_ok: None = Depends(lambda: None)):
    """Публичный (без auth) статус: настроена ли платформа. Ничего
    чувствительного не отдаёт -- нужен UI, чтобы показать логин/заглушку."""
    settings = get_settings()
    return {"configured": bool(settings.platform_admin_password)}


# ---------------------------------------------------------------------------
# Обзор / данные (только владелец)
# ---------------------------------------------------------------------------


def _active_project(session) -> Project:
    project = session.exec(select(Project).where(Project.is_active == True)).first()
    if project is None:
        raise HTTPException(status_code=404, detail="Нет активного проекта")
    return project


@router.get("/api/overview", dependencies=[Depends(require_admin)])
async def overview():
    with get_session() as session:
        project = session.exec(select(Project).where(Project.is_active == True)).first()
        if project is None:
            return {"build_marker": BUILD_MARKER, "project": None}

        integrations = session.exec(
            select(Integration).where(Integration.project_id == project.id)
        ).all()
        open_alerts = session.exec(
            select(Alert).where(
                Alert.project_id == project.id,
                Alert.status.in_(["open", "sent", "acknowledged", "escalated"]),
            )
        ).all()
        return {
            "build_marker": BUILD_MARKER,
            "project": {
                "id": project.id,
                "name": project.name,
                "type": project.type,
                "base_url": project.base_url,
                "connector": project.connector_name,
                "mode": (project.settings_json or {}).get("mode", "watch_only"),
            },
            "integrations": [
                {
                    "type": i.type.value,
                    "status": i.status.value,
                    "last_sync_at": i.last_sync_at.isoformat() if i.last_sync_at else None,
                    "last_error": i.last_error,
                }
                for i in integrations
            ],
            "open_alerts_count": len(open_alerts),
        }


@router.get("/api/funnel", dependencies=[Depends(require_admin)])
async def funnel():
    """Последний снимок нормализованной воронки по каждому окну (3h/24h/7d).
    Берём combined-снэпшот (product + Метрика/Директ), если он есть,
    иначе -- project_metrics_api."""
    with get_session() as session:
        project = _active_project(session)
        result = {}
        for period_key in ANALYSIS_WINDOWS_HOURS:
            snapshot = None
            for source in ("combined", "project_metrics_api"):
                snapshot = session.exec(
                    select(MetricSnapshot)
                    .where(
                        MetricSnapshot.project_id == project.id,
                        MetricSnapshot.period_key == period_key,
                        MetricSnapshot.source == source,
                    )
                    .order_by(MetricSnapshot.created_at.desc())
                    .limit(1)
                ).first()
                if snapshot is not None:
                    break
            if snapshot is None:
                result[period_key] = None
                continue
            metrics = snapshot.metrics_json or {}
            result[period_key] = {
                "source": snapshot.source,
                "as_of": snapshot.as_of.isoformat() if snapshot.as_of else None,
                "created_at": snapshot.created_at.isoformat(),
                "funnel": {k: metrics.get(k) for k in CORE_FUNNEL_KEYS},
            }
        return {"project_id": project.id, "windows": result}


@router.get("/api/alerts", dependencies=[Depends(require_admin)])
async def alerts(limit: int = 30):
    with get_session() as session:
        project = _active_project(session)
        rows = session.exec(
            select(Alert)
            .where(Alert.project_id == project.id)
            .order_by(Alert.last_seen_at.desc())
            .limit(limit)
        ).all()
        return [
            {
                "id": a.id,
                "title": a.title,
                "message": a.message,
                "category": a.category.value,
                "severity": a.severity.value,
                "confidence": a.confidence.value,
                "status": a.status.value,
                "occurrence_count": a.occurrence_count,
                "first_seen_at": a.first_seen_at.isoformat(),
                "last_seen_at": a.last_seen_at.isoformat(),
            }
            for a in rows
        ]


@router.post("/api/run", dependencies=[Depends(require_admin)])
async def run_cycle():
    from app.scheduler import run_cycle_once_sync_with_timeout

    try:
        result = await asyncio.to_thread(
            run_cycle_once_sync_with_timeout, None, RUN_CYCLE_TIMEOUT_SECONDS, "platform_run",
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Источники данных не ответили вовремя")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.exception("platform /api/run failed")
        raise HTTPException(status_code=500, detail=str(exc))

    primary = result.primary_candidate
    return {
        "has_notifiable_changes": result.has_notifiable_changes,
        "primary": None if primary is None else {
            "title": primary.title,
            "severity": primary.severity.value,
            "hypothesis": primary.hypothesis,
            "check_action": primary.check_action,
        },
    }


# ---------------------------------------------------------------------------
# Чат с аналитиком (замена ТГ-чата)
# ---------------------------------------------------------------------------


@router.post("/api/ask", dependencies=[Depends(require_admin)])
async def ask(body: AskRequest):
    from app import ask as ask_module

    settings = get_settings()
    if not ask_module.is_configured(settings):
        raise HTTPException(
            status_code=503,
            detail="LLM не настроен: задайте LLM_PROVIDER=yandex и YANDEX_API_KEY/YANDEX_FOLDER_ID",
        )
    with get_session() as session:
        project = _active_project(session)
        context_text = ask_module.build_context(session, project)

    answer = await ask_module.answer_question(body.question, context_text, settings)
    if answer is None:
        raise HTTPException(status_code=502, detail="LLM не ответил, попробуйте ещё раз")
    return {"answer": answer}


# ---------------------------------------------------------------------------
# Проекты: подключение любого продукта
# ---------------------------------------------------------------------------


async def _probe_internal_api(base_url: str, token: str) -> dict:
    """Проверка подключения: обязательный metrics + автообнаружение
    опциональных endpoints. Возвращает результат probe без исключений."""
    base = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    available: list[str] = []
    errors: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        for name, params in INTERNAL_ENDPOINT_PROBES:
            url = f"{base}/api/internal/{name}"
            try:
                resp = await client.get(url, headers=headers, params=params)
            except httpx.HTTPError as exc:
                errors[name] = f"network: {exc.__class__.__name__}"
                continue
            if resp.status_code == 200:
                available.append(name)
            else:
                errors[name] = f"HTTP {resp.status_code}"
    ok = "metrics" in available
    return {
        "ok": ok,
        "available_endpoints": available,
        "errors": errors,
        "hint": None if ok else (
            "Обязательный endpoint /api/internal/metrics недоступен. Проверьте "
            "адрес проекта и токен (на стороне проекта это TRUEPOST_INTERNAL_API_TOKEN "
            "или аналогичная переменная)."
        ),
    }


@router.post("/api/connection-test", dependencies=[Depends(require_admin)])
async def connection_test(body: ConnectionTestRequest):
    return await _probe_internal_api(body.base_url, body.internal_api_token)


def _project_to_dict(p: Project) -> dict:
    sj = p.settings_json or {}
    return {
        "id": p.id,
        "name": p.name,
        "type": p.type,
        "base_url": p.base_url,
        "connector": p.connector_name,
        "is_active": p.is_active,
        "has_token": bool(sj.get("internal_api_token") or get_settings().project_internal_api_token),
        "available_endpoints": sj.get("available_endpoints") or [],
        "funnel_mapping": sj.get("funnel_mapping") or {},
        "metrika_counter_id": sj.get("metrika_counter_id"),
        "direct_client_login": sj.get("direct_client_login"),
        "created_at": p.created_at.isoformat(),
    }


@router.get("/api/projects", dependencies=[Depends(require_admin)])
async def list_projects():
    with get_session() as session:
        projects = session.exec(select(Project).order_by(Project.id)).all()
        return [_project_to_dict(p) for p in projects]


@router.post("/api/projects", dependencies=[Depends(require_admin)])
async def create_project(body: ProjectCreateRequest):
    from app.connectors.truepost import DEFAULT_FUNNEL_MAPPING

    probe = await _probe_internal_api(body.base_url, body.internal_api_token)
    if not probe["ok"]:
        raise HTTPException(status_code=422, detail={"message": "Подключение не прошло проверку", "probe": probe})

    with get_session() as session:
        existing = session.exec(select(Project).where(Project.name == body.name)).first()
        if existing is not None:
            raise HTTPException(status_code=409, detail="Проект с таким названием уже есть")

        project = Project(
            name=body.name,
            type=body.type,
            base_url=body.base_url.rstrip("/"),
            connector_name="truepost",  # универсальный контракт /api/internal/* (CONTRACT.md)
            is_active=False,
            settings_json={
                "internal_api_token": body.internal_api_token,
                "funnel_mapping": body.funnel_mapping or DEFAULT_FUNNEL_MAPPING,
                "available_endpoints": probe["available_endpoints"],
                "metrika_counter_id": body.metrika_counter_id,
                "direct_client_login": body.direct_client_login,
            },
        )
        session.add(project)
        session.commit()
        session.refresh(project)
        _ensure_integrations(session, project)
        return {"ok": True, "project": _project_to_dict(project), "probe": probe}


@router.patch("/api/projects/{project_id}", dependencies=[Depends(require_admin)])
async def update_project(project_id: int, body: ProjectUpdateRequest):
    with get_session() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Проект не найден")

        if body.name is not None:
            project.name = body.name
        if body.type is not None:
            project.type = body.type
        if body.base_url is not None:
            project.base_url = body.base_url.rstrip("/")

        sj = dict(project.settings_json or {})
        if body.internal_api_token is not None:
            sj["internal_api_token"] = body.internal_api_token
        if body.funnel_mapping is not None:
            sj["funnel_mapping"] = body.funnel_mapping
        if body.metrika_counter_id is not None:
            sj["metrika_counter_id"] = body.metrika_counter_id
        if body.direct_client_login is not None:
            sj["direct_client_login"] = body.direct_client_login
        project.settings_json = sj

        session.add(project)
        session.commit()
        session.refresh(project)
        return {"ok": True, "project": _project_to_dict(project)}


@router.post("/api/projects/{project_id}/activate", dependencies=[Depends(require_admin)])
async def activate_project(project_id: int):
    """Делает проект активным (его анализирует планировщик). В v1 активен
    ровно один проект -- остальные деактивируются."""
    with get_session() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Проект не найден")
        for p in session.exec(select(Project)).all():
            p.is_active = (p.id == project_id)
            session.add(p)
        session.commit()
        return {"ok": True}


@router.post("/api/projects/{project_id}/retest", dependencies=[Depends(require_admin)])
async def retest_project(project_id: int):
    with get_session() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Проект не найден")
        token = project_internal_api_token(project)
        if not project.base_url or not token:
            raise HTTPException(status_code=422, detail="У проекта нет base_url или токена")
        base_url = project.base_url

    probe = await _probe_internal_api(base_url, token)

    with get_session() as session:
        project = session.get(Project, project_id)
        sj = dict(project.settings_json or {})
        sj["available_endpoints"] = probe["available_endpoints"]
        project.settings_json = sj
        session.add(project)
        session.commit()
    return probe
