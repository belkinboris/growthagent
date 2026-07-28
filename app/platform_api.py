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
from app import accounts, connect_snippets
from app.error_text import humanize_error
from app.db import get_session, _ensure_integrations
from app.models import Alert, Integration, MetricSnapshot, PlatformUser, Project
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
    # Почта необязательна: владелец платформы входит одним паролем из
    # окружения, клиент -- почтой и паролем своего аккаунта.
    email: Optional[str] = None


class RegisterRequest(BaseModel):
    email: str
    password: str
    display_name: Optional[str] = None


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
    # Кому слать уведомления по этому проекту (Telegram chat id). Пусто --
    # значит, канал не настроен: см. app/notify_targets.py.
    notify_chat_ids: Optional[list[str]] = None
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


def _set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.platform_session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.platform_cookie_secure,
        path="/",
    )


@router.post("/api/login")
async def login(body: LoginRequest, response: Response):
    """Один вход на два случая: почта+пароль (аккаунт) и просто пароль
    (владелец из окружения). Разделять формы незачем — человек вводит то,
    что у него есть, а платформа сама понимает, кто пришёл."""
    settings = get_settings()

    if body.email:
        with get_session() as session:
            user = accounts.authenticate(session, body.email, body.password)
            if user is None:
                raise HTTPException(status_code=401, detail="Неверная почта или пароль")
            token = issue_session_token(user_id=user.id)
            _set_session_cookie(response, token)
            return {"ok": True, "token": token, "email": user.email, "owner": user.is_owner}

    if not settings.platform_admin_password:
        raise HTTPException(status_code=503, detail="Платформа не настроена: задайте PLATFORM_ADMIN_PASSWORD")
    if not verify_password(body.password):
        raise HTTPException(status_code=401, detail="Неверный пароль")
    token = issue_session_token()
    _set_session_cookie(response, token)
    return {"ok": True, "token": token, "owner": True}


@router.post("/api/register")
async def register(body: RegisterRequest, response: Response):
    """Регистрация клиента. Первый зарегистрировавшийся усыновляет проекты,
    заведённые до появления аккаунтов, — иначе живой проект остался бы
    без владельца и пропал бы из интерфейса после включения изоляции."""
    with get_session() as session:
        try:
            user = accounts.create_user(
                session, body.email, body.password, display_name=body.display_name
            )
        except accounts.EmailTaken:
            raise HTTPException(status_code=409, detail="Такая почта уже зарегистрирована")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        adopted = accounts.adopt_orphan_projects(session, user.id)
        token = issue_session_token(user_id=user.id)
        _set_session_cookie(response, token)
        return {"ok": True, "token": token, "email": user.email, "adopted_projects": adopted}


@router.get("/api/me", dependencies=[Depends(require_admin)])
async def me(identity=Depends(require_admin)):
    """Кто вошёл. Нужен интерфейсу после перезагрузки страницы: сессия
    живёт в cookie, и без этого шапка не знает, чей аккаунт открыт."""
    if identity is None or identity.user_id is None:
        return {"email": None, "is_owner": True, "kind": "platform_owner"}
    with get_session() as session:
        user = session.get(PlatformUser, identity.user_id)
        if user is None:
            # Аккаунт удалили, а cookie осталась -- честнее разлогинить,
            # чем показывать интерфейс несуществующему пользователю.
            raise HTTPException(status_code=401, detail="Не авторизован")
        return {
            "email": user.email,
            "display_name": user.display_name,
            "is_owner": user.is_owner,
            "kind": "account",
        }


@router.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/api/session")
async def session_state(request_ok: None = Depends(lambda: None)):
    """Публичный (без auth) статус: настроена ли платформа. Ничего
    чувствительного не отдаёт -- нужен UI, чтобы показать логин/заглушку.

    Платформа настроена, если есть пароль владельца ИЛИ хотя бы один
    аккаунт: клиент, зарегистрировавшийся сам, должен видеть форму входа,
    а не «платформа не настроена»."""
    settings = get_settings()
    has_accounts = False
    try:
        with get_session() as session:
            has_accounts = session.exec(select(PlatformUser.id)).first() is not None
    except Exception:  # база недоступна -- отвечаем честно «не настроена»
        has_accounts = False
    # Наружу -- один флаг. Отдавать «есть ли аккаунты» отдельным полем
    # незачем: анонимному посетителю это ничего не даёт, а разведке помогает.
    return {"configured": bool(settings.platform_admin_password) or has_accounts}


# ---------------------------------------------------------------------------
# Обзор / данные (только владелец)
# ---------------------------------------------------------------------------


def _visible_project_ids(session, identity) -> Optional[set[int]]:
    """Какие проекты видит пришедший. None -- видит все.

    None -- это вход по паролю из окружения: владелец платформы. У него
    аккаунта может не быть, и ограничивать его нечем и незачем -- сервер его.
    У аккаунта видно ровно то, что записано в ProjectMember.
    """
    if identity is None or identity.user_id is None:
        return None
    return set(accounts.user_project_ids(session, identity.user_id))


def _find_project(session, identity) -> Optional[Project]:
    """Проект, о котором идёт разговор на всех экранах.

    Приоритет -- активный (тот, который собирает планировщик). Если у
    аккаунта активного нет, а свой проект есть, показываем его: иначе
    человек, только что подключивший проект, видел бы «нет проекта»
    при живом проекте в списке.
    """
    visible = _visible_project_ids(session, identity)
    if visible is not None and not visible:
        return None

    def _scoped(query):
        return query if visible is None else query.where(Project.id.in_(visible))

    # Выбор в шапке важнее «первого включённого»: человек смотрит на тот
    # проект, который выбрал, даже если сбор по нему сейчас выключен.
    chosen_id = getattr(identity, "selected_project_id", None)
    if chosen_id is not None:
        chosen = session.exec(_scoped(select(Project).where(Project.id == chosen_id))).first()
        if chosen is not None:
            return chosen
        # Выбранного проекта больше нет или он чужой -- молча падаем на
        # обычный выбор, а не показываем пустоту: cookie мог остаться
        # от удалённого проекта или от другого аккаунта на том же браузере.

    project = session.exec(
        _scoped(select(Project).where(Project.is_active == True))  # noqa: E712
    ).first()
    if project is None:
        # Проект есть, но выключен -- показываем его. Иначе экран говорит
        # «проект ещё не подключён», хотя проект подключён: это враньё,
        # и человек идёт подключать второй раз.
        project = session.exec(_scoped(select(Project)).order_by(Project.id)).first()
    return project


def _active_project(session, identity=None) -> Project:
    project = _find_project(session, identity)
    if project is None:
        raise HTTPException(status_code=404, detail="Нет активного проекта")
    return project


def _owned_project(session, project_id: int, identity) -> Project:
    """Проект по id с проверкой доступа.

    Чужой проект отдаём как 404, а не 403: 403 подтверждает, что проект
    с таким номером существует, и превращает перебор в разведку.
    """
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    visible = _visible_project_ids(session, identity)
    if visible is not None and project.id not in visible:
        raise HTTPException(status_code=404, detail="Проект не найден")
    return project


def _require_own_project_id(session, project_id: Optional[int], identity) -> None:
    """Проверка доступа к объекту, найденному не по проекту, а по своему id
    (сигнал, рекомендация, эксперимент). Без неё изоляция дырявая: список
    чужой не покажет, а действие по угаданному номеру пройдёт."""
    visible = _visible_project_ids(session, identity)
    if visible is None:
        return
    if project_id is None or project_id not in visible:
        raise HTTPException(status_code=404, detail="Не найдено")


@router.get("/api/overview", dependencies=[Depends(require_admin)])
async def overview(identity=Depends(require_admin)):
    with get_session() as session:
        project = _find_project(session, identity)
        if project is None:
            return {"build_marker": BUILD_MARKER, "project": None}

        integrations = session.exec(
            select(Integration).where(Integration.project_id == project.id)
        ).all()
        # Статусы telegram и llm никто не обновляет в цикле сбора (эти
        # интеграции не источники метрик), поэтому в БД они навсегда
        # not_configured -- даже когда бот шлёт сообщения. Считаем их
        # состояние по конфигурации, чтобы список не врал владельцу.
        live_status = _config_based_statuses(settings=get_settings())
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
                # Неактивный проект планировщик не собирает: экран обязан
                # сказать это словами, иначе пустой обзор выглядит поломкой.
                "is_active": project.is_active,
            },
            "integrations": [
                {
                    "type": i.type.value,
                    "status": live_status.get(i.type.value, i.status.value),
                    "last_sync_at": i.last_sync_at.isoformat() if i.last_sync_at else None,
                    "last_error": i.last_error,
                    # Человеческое объяснение рядом с исходной строкой:
                    # владельцу -- смысл, в подсказке -- факт для поддержки.
                    "error_human": (humanize_error(i.last_error, i.type.value)
                                    if i.status.value == "error" else None),
                }
                for i in integrations
            ],
            "open_alerts_count": len(open_alerts),
        }


def _config_based_statuses(settings) -> dict[str, str]:
    """Статусы интеграций, которые определяются конфигурацией, а не сбором
    данных: Telegram (канал уведомлений) и LLM (чат с аналитиком)."""
    from app import ask as ask_module

    return {
        "telegram": "ok" if settings.bot_token else "not_configured",
        "llm": "ok" if ask_module.is_configured(settings) else "not_configured",
    }


@router.get("/api/funnel", dependencies=[Depends(require_admin)])
async def funnel(identity=Depends(require_admin)):
    """Последний снимок нормализованной воронки по каждому окну (3h/24h/7d).
    Берём combined-снэпшот (product + Метрика/Директ), если он есть,
    иначе -- project_metrics_api."""
    with get_session() as session:
        project = _active_project(session, identity)
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
            # combined-снэпшот хранит вложенную структуру
            # {"product": {нормализованные ключи}, "direct": {...}} --
            # см. service.extract_normalized_metrics_from_snapshot.
            # Флэт-структура -- запасной путь для чисто продуктовых снэпшотов.
            product = metrics.get("product") if isinstance(metrics.get("product"), dict) else metrics
            direct = metrics.get("direct") if isinstance(metrics.get("direct"), dict) else {}
            funnel_values = {k: product.get(k) for k in CORE_FUNNEL_KEYS}
            if funnel_values.get("traffic") is None and direct.get("clicks") is not None:
                funnel_values["traffic"] = direct.get("clicks")
            result[period_key] = {
                "source": snapshot.source,
                "as_of": snapshot.as_of.isoformat() if snapshot.as_of else None,
                "created_at": snapshot.created_at.isoformat(),
                "funnel": funnel_values,
            }
        # Названия шагов идут вместе с числами: интерфейс не должен
        # знать продуктовую специфику проекта.
        return {
            "project_id": project.id,
            "windows": result,
            "stage_titles": load_stage_titles(session, project.id),
            "stage_order": STAGE_ORDER,
        }


@router.get("/api/alerts", dependencies=[Depends(require_admin)])
async def alerts(limit: int = 30, identity=Depends(require_admin)):
    with get_session() as session:
        project = _active_project(session, identity)
        rows = session.exec(
            select(Alert)
            .where(Alert.project_id == project.id)
            .order_by(Alert.last_seen_at.desc())
            .limit(limit * 3)  # запас: дубли по окнам схлопнутся ниже
        ).all()

        # Одно и то же правило срабатывает отдельно в каждом окне (3h/24h/7d),
        # fingerprint = project/rule/period/step -- это осознанный дизайн ядра
        # (см. rules.py). Но владельцу в списке нужен один сигнал с пометкой,
        # в каких окнах он виден, а не три одинаковые строки.
        grouped: dict[str, dict] = {}
        for a in rows:
            parts = a.fingerprint.split("/")
            period = parts[2] if len(parts) == 4 else None
            group_key = f"{parts[1]}/{parts[3]}" if len(parts) == 4 else f"{a.category.value}/{a.title}"
            g = grouped.get(group_key)
            if g is None:
                grouped[group_key] = {
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
                    "periods": [period] if period else [],
                }
            else:
                if period and period not in g["periods"]:
                    g["periods"].append(period)
                # показываем худшую severity из окон ("P0" < "P1" лексикографически)
                if a.severity.value < g["severity"]:
                    g["severity"] = a.severity.value
                g["occurrence_count"] = max(g["occurrence_count"], a.occurrence_count)
        window_order = {"3h": 0, "24h": 1, "7d": 2}
        out = list(grouped.values())[:limit]
        for g in out:
            g["periods"].sort(key=lambda p: window_order.get(p, 9))
        return out


# ---------------------------------------------------------------------------
# Отчёты аналитика (то, что раньше жило в командах Telegram-бота)
# ---------------------------------------------------------------------------


def _report_context(session, project):
    """
    Общий контекст для всех отчётов: последний combined-снэпшот за 7 дней →
    NormalizedMetrics, кэш диагностики пути к оплате, кэш путей пользователей.
    Ровно те же данные, на которых строит отчёты Telegram-бот (без внешних
    вызовов -- всё из кэша, страница должна открываться быстро).
    """
    from app.rules import NormalizedMetrics
    from app.service import (
        PAYMENT_PATH_CACHE_PERIOD_KEY,
        USER_JOURNEYS_CACHE_PERIOD_KEY,
        extract_normalized_metrics_from_snapshot,
        get_cached_diagnostics,
    )

    snapshot = session.exec(
        select(MetricSnapshot)
        .where(
            MetricSnapshot.project_id == project.id,
            MetricSnapshot.period_key == "7d",
            MetricSnapshot.source == "combined",
        )
        .order_by(MetricSnapshot.created_at.desc())
        .limit(1)
    ).first()

    metrics_obj = None
    snapshot_dt = None
    if snapshot is not None:
        raw = extract_normalized_metrics_from_snapshot(snapshot)
        sources_ok = {
            name for name in ("product", "metrika", "direct", "yookassa")
            if (snapshot.metrics_json or {}).get(name) is not None
        }
        metrics_obj = NormalizedMetrics(
            period_key="7d",
            signup=raw.get("signup"),
            activation_1=raw.get("activation_1"),
            activation_2=raw.get("activation_2"),
            payment_started=raw.get("payment_started"),
            payment_success=raw.get("payment_success"),
            spend=raw.get("spend"),
            clicks=raw.get("clicks"),
            sources_ok=sources_ok,
        )
        snapshot_dt = snapshot.created_at

    pp_cached = get_cached_diagnostics(session, project.id, PAYMENT_PATH_CACHE_PERIOD_KEY)
    payment_path = dict(pp_cached.result_json or {}) if (pp_cached and pp_cached.ok) else None

    j_cached = get_cached_diagnostics(session, project.id, USER_JOURNEYS_CACHE_PERIOD_KEY)
    journeys = (j_cached.result_json or {}).get("journeys") if (j_cached and j_cached.ok) else None

    return {
        "project_name": project.name,
        "metrics": metrics_obj,
        "payment_path": payment_path,
        "journeys": journeys,
        "snapshot_dt": snapshot_dt,
    }


# Какие отчёты доступны на дашборде. Порядок = порядок вкладок в UI.
REPORT_KINDS = ["board", "funnel", "pay", "ads", "checks", "journeys", "experiments"]


@router.get("/api/reports/{kind}", dependencies=[Depends(require_admin)])
async def report(kind: str, identity=Depends(require_admin)):
    """
    Текстовый отчёт аналитика. Формулировки (гипотезы, «что не менять»,
    честные слова про малые выборки) -- главная ценность, поэтому берём
    ровно те же builder'ы, что и Telegram-бот, а не пересобираем заново.
    """
    if kind not in REPORT_KINDS:
        raise HTTPException(status_code=404, detail="Неизвестный отчёт")

    from app import commercial_report as cr

    with get_session() as session:
        project = _active_project(session, identity)
        ctx = _report_context(session, project)

    name = ctx["project_name"]
    pp = ctx["payment_path"]
    metrics = ctx["metrics"]
    registrations = (pp or {}).get("registrations")

    if kind == "board":
        text = cr.build_board_report(
            name, metrics, payment_path=pp,
            new_registrations_since_deploy=registrations,
            skip_decision=True,  # решение показываем отдельным блоком Growth Loop
        )
    elif kind == "funnel":
        text = cr.build_funnel_report(name, metrics, payment_path=pp, snapshot_dt=ctx["snapshot_dt"])
    elif kind == "pay":
        text = cr.build_pay_report(name, payment_path=pp, metrics=metrics, snapshot_dt=ctx["snapshot_dt"])
    elif kind == "ads":
        text = cr.build_ads_report(name, metrics=metrics, snapshot_dt=ctx["snapshot_dt"])
    elif kind == "checks":
        text = cr.build_checks_report(name, payment_path=pp)
    elif kind == "journeys":
        text = cr.build_journeys_report(name, ctx["journeys"])
    else:  # experiments
        text = cr.build_experiments_report(
            name, payment_path=pp,
            new_registrations_since_deploy=registrations,
            recent_journeys=ctx["journeys"],
        )
    return {"kind": kind, "text": text}


@router.get("/api/dynamics", dependencies=[Depends(require_admin)])
async def dynamics(days: int = 14, identity=Depends(require_admin)):
    """Динамика по дням: и сырые точки для графика, и текстовый блок
    аналитика (он умеет честно говорить «данных мало»)."""
    from app.commercial_report import build_dynamics_block
    from app.service import load_daily_counters_history

    with get_session() as session:
        project = _active_project(session, identity)
        history = load_daily_counters_history(session, project.id, days=days)

    text = build_dynamics_block(history) if len(history) >= 2 else None
    return {"history": history, "text": text}


@router.get("/api/live", dependencies=[Depends(require_admin)])
async def live_feed(period_minutes: int = 720, limit: int = 100, identity=Depends(require_admin)):
    """
    Живая лента: дискретные события пользователей продукта (регистрация,
    канал, отзыв о первом посте, открытие тарифов, оплата) в порядке
    от свежих к старым. Данные анонимные -- продукт отдаёт user_key
    (необратимый хэш), никаких email и id.

    В отличие от остальных панелей это НЕ кэш: лента должна быть живой,
    поэтому дёргаем продукт напрямую. Ошибку не прячем -- владельцу важно
    отличать «событий нет» от «источник не отвечает».
    """
    from app.connectors.user_events import fetch_user_events

    with get_session() as session:
        project = _active_project(session, identity)
        base_url = project.base_url
        token = project_internal_api_token(project)
        # Названия событий: свои для проекта, если владелец их задал
        # (или подтвердил предложенные ИИ) -- иначе покажем как есть.
        event_labels = (project.settings_json or {}).get("event_labels") or {}

    result = await fetch_user_events(base_url, token, period_minutes=period_minutes, limit=limit)
    events = result.get("events") or []
    events.sort(key=lambda e: e.get("created_at") or "", reverse=True)

    if not result.get("ok"):
        hint = {
            "not_configured": "У проекта не задан адрес или токен внутреннего API.",
            "not_found": "Продукт не отдаёт /api/internal/user-events — обновите его до версии с этим endpoint.",
            "timeout": "Продукт не ответил вовремя.",
        }.get(result.get("status"))
        return {
            "ok": False, "status": result.get("status"), "error": result.get("error"),
            "hint": hint, "events": [], "period_minutes": period_minutes,
            "event_labels": event_labels,
        }
    return {
        "ok": True, "events": events, "period_minutes": period_minutes,
        "event_labels": event_labels,
    }


# ---------------------------------------------------------------------------
# Growth Loop: рекомендация → эксперимент → вердикт (кнопки владельца)
# ---------------------------------------------------------------------------


def _rec_to_dict(rec) -> dict:
    return {
        "id": rec.id,
        "area": rec.area,
        "title": rec.title,
        "action": rec.action,
        "hypothesis": rec.hypothesis,
        "evidence": rec.evidence_json or [],
        "confidence": rec.confidence,
        "expected_effect": rec.expected_effect,
        "risk": rec.risk,
        "change_set": rec.change_set_json or [],
        "measure": rec.measure,
        "locked_variables": rec.locked_variables_json or [],
        "success_criterion": rec.success_criterion,
        "failure_criterion": rec.failure_criterion,
        "created_at": rec.created_at.isoformat(),
    }


@router.get("/api/growth", dependencies=[Depends(require_admin)])
async def growth_state(identity=Depends(require_admin)):
    """Состояние цикла роста: что предложено, что идёт, чем закончилось."""
    from app import growth_loop
    from app.commercial_report import build_experiment_block, build_verdict_block

    with get_session() as session:
        project = _active_project(session, identity)
        ctx = _report_context(session, project)
        pp = ctx["payment_path"]

        rec = growth_loop.get_active_recommendation(session, project.id)
        running = growth_loop.get_running_experiment(session, project.id)
        last = growth_loop.get_last_finished_experiment(session, project.id)

        out: dict = {"recommendation": None, "experiment": None, "last_verdict": None}

        if rec is not None:
            out["recommendation"] = _rec_to_dict(rec)
        if running is not None:
            progress = growth_loop.experiment_progress(running, pp)
            out["experiment"] = {
                "id": running.id,
                "title": running.title,
                "area": running.area,
                "hypothesis": running.hypothesis,
                "primary_metric": running.primary_metric,
                "sample_metric": running.sample_metric,
                "target_sample": running.target_sample,
                "locked_variables": running.locked_variables_json or [],
                "success_criterion": running.success_criterion,
                "failure_criterion": running.failure_criterion,
                "started_at": running.started_at.isoformat(),
                "progress": progress,
                "text": build_experiment_block(running, progress),
            }
        if last is not None:
            out["last_verdict"] = {
                "id": last.id,
                "title": last.title,
                "status": last.status.value,
                "verdict": last.verdict,
                "result_summary": last.result_summary,
                "ended_at": last.ended_at.isoformat() if last.ended_at else None,
                "text": build_verdict_block(last),
            }
        return out


@router.get("/api/growth/recommendation/{rec_id}/why", dependencies=[Depends(require_admin)])
async def growth_why(rec_id: int, identity=Depends(require_admin)):
    from app import growth_loop
    from app.commercial_report import build_recommendation_why
    from app.models import GrowthRecommendation

    with get_session() as session:
        rec = session.get(GrowthRecommendation, rec_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="Рекомендация не найдена")
        _require_own_project_id(session, rec.project_id, identity)
        return {"text": build_recommendation_why(rec)}


class GrowthDecision(BaseModel):
    reason: str = ""
    days: int = 7


@router.post("/api/growth/recommendation/{rec_id}/{action}", dependencies=[Depends(require_admin)])
async def growth_decide(rec_id: int, action: str, body: GrowthDecision | None = None, identity=Depends(require_admin)):
    """Решение владельца по рекомендации: принять (стартует эксперимент
    с зафиксированным baseline), отложить или отклонить."""
    from app import growth_loop
    from app.models import GrowthRecommendation

    if action not in ("accept", "defer", "reject"):
        raise HTTPException(status_code=404, detail="Неизвестное действие")

    body = body or GrowthDecision()
    with get_session() as session:
        rec = session.get(GrowthRecommendation, rec_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="Рекомендация не найдена")
        _require_own_project_id(session, rec.project_id, identity)
        project = _active_project(session, identity)
        pp = _report_context(session, project)["payment_path"]

        if action == "accept":
            exp = growth_loop.accept_recommendation(session, rec, pp)
            return {"ok": True, "experiment_id": exp.id if exp else None}
        if action == "defer":
            growth_loop.defer_recommendation(session, rec, days=body.days)
            return {"ok": True}
        growth_loop.reject_recommendation(session, rec, reason=body.reason)
        return {"ok": True}


@router.post("/api/growth/experiment/{exp_id}/cancel", dependencies=[Depends(require_admin)])
async def growth_cancel(exp_id: int, body: GrowthDecision | None = None, identity=Depends(require_admin)):
    from app import growth_loop
    from app.models import GrowthExperiment

    with get_session() as session:
        exp = session.get(GrowthExperiment, exp_id)
        if exp is None:
            raise HTTPException(status_code=404, detail="Проверка не найдена")
        _require_own_project_id(session, exp.project_id, identity)
        growth_loop.cancel_experiment(session, exp, reason=(body.reason if body else ""))
        return {"ok": True}


# ---------------------------------------------------------------------------
# Этапы воронки: человеческие названия шагов для каждого проекта
# ---------------------------------------------------------------------------
#
# Ядро работает на нормализованных ключах (signup, activation_1 ...), они
# универсальны, но клиенту показывать "activation_1" нельзя. У АвтоПоста
# названия известны, у чужого проекта -- нет, поэтому по умолчанию это
# честные "Этап 1", "Этап 2", а осмысленные имена приходят одним из двух
# путей: владелец переименовал руками либо ИИ посмотрел сайт проекта
# и предложил названия (autoname).


STAGE_ORDER = ["traffic", "signup", "activation_1", "activation_2",
               "payment_started", "payment_success", "revenue"]

# Названия, не зависящие от продукта: их смысл одинаков для любого проекта.
UNIVERSAL_STAGE_TITLES = {
    "traffic": "Трафик",
    "signup": "Регистрация",
    "payment_started": "Оплата начата",
    "payment_success": "Оплата прошла",
    "revenue": "Выручка",
}


def _default_stage_title(key: str) -> str:
    """Для activation_* продуктового смысла ядро не знает -- нумеруем честно."""
    if key in UNIVERSAL_STAGE_TITLES:
        return UNIVERSAL_STAGE_TITLES[key]
    if key.startswith("activation_"):
        return f"Этап {key.split('_')[-1]}"
    return key


def load_stage_titles(session, project_id: int) -> dict[str, str]:
    """Ключ воронки → название для показа. Своё название проекта имеет
    приоритет над дефолтным."""
    from app.models import FunnelStep

    rows = session.exec(select(FunnelStep).where(FunnelStep.project_id == project_id)).all()
    custom = {r.key: r.title for r in rows if r.title}
    return {key: custom.get(key) or _default_stage_title(key) for key in STAGE_ORDER}


@router.get("/api/projects/{project_id}/stages", dependencies=[Depends(require_admin)])
async def get_stages(project_id: int, identity=Depends(require_admin)):
    from app.models import FunnelStep

    with get_session() as session:
        project = _owned_project(session, project_id, identity)
        rows = {r.key: r for r in session.exec(
            select(FunnelStep).where(FunnelStep.project_id == project_id)).all()}
        mapping = (project.settings_json or {}).get("funnel_mapping") or {}
        return {
            "stages": [
                {
                    "key": key,
                    "title": (rows[key].title if key in rows and rows[key].title
                              else _default_stage_title(key)),
                    "is_custom": key in rows and bool(rows[key].title),
                    "description": rows[key].description if key in rows else None,
                    "source_metric": mapping.get(key),
                }
                for key in STAGE_ORDER
            ],
            "event_labels": (project.settings_json or {}).get("event_labels") or {},
        }


class StagesUpdate(BaseModel):
    titles: dict[str, str] = {}          # ключ воронки -> название
    event_labels: dict[str, str] = {}    # тип события -> название (для ленты)


@router.put("/api/projects/{project_id}/stages", dependencies=[Depends(require_admin)])
async def update_stages(project_id: int, body: StagesUpdate, identity=Depends(require_admin)):
    from app.models import FunnelStep

    with get_session() as session:
        project = _owned_project(session, project_id, identity)

        existing = {r.key: r for r in session.exec(
            select(FunnelStep).where(FunnelStep.project_id == project_id)).all()}
        for key, title in (body.titles or {}).items():
            title = (title or "").strip()
            if key in existing:
                existing[key].title = title
                session.add(existing[key])
            elif title:
                session.add(FunnelStep(
                    project_id=project_id, key=key, title=title,
                    order=STAGE_ORDER.index(key) if key in STAGE_ORDER else 99,
                ))
        if body.event_labels:
            sj = dict(project.settings_json or {})
            sj["event_labels"] = {**(sj.get("event_labels") or {}), **body.event_labels}
            project.settings_json = sj
            session.add(project)
        session.commit()
        return {"ok": True, "stages": (await get_stages(project_id, identity))["stages"]}


async def _project_page_text(base_url: Optional[str]) -> str:
    """Текст главной страницы проекта для подсказок ИИ. Сайт недоступен --
    возвращаем пустую строку: называть шаги по одним только кодам метрик
    хуже, но честнее, чем врать про недоступность."""
    import re

    if not base_url:
        return ""
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(base_url)
    except httpx.HTTPError:
        logger.info("autoname: сайт проекта недоступен")
        return ""
    if resp.status_code != 200:
        return ""
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", resp.text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()[:3000]


@router.post("/api/projects/{project_id}/stages/autoname", dependencies=[Depends(require_admin)])
async def autoname_stages(project_id: int, identity=Depends(require_admin)):
    """
    ИИ смотрит на сайт проекта и предлагает названия этапов вместо
    «Этап 1/2». Ничего не сохраняет сам -- возвращает предложение,
    владелец подтверждает (или правит) в интерфейсе.
    """
    import json
    import re

    from app import ask as ask_module

    settings = get_settings()
    if not ask_module.is_configured(settings):
        raise HTTPException(status_code=503, detail="Для автоназвания нужен настроенный LLM (LLM_PROVIDER=yandex)")

    with get_session() as session:
        project = _owned_project(session, project_id, identity)
        base_url = project.base_url
        project_name = project.name
        mapping = (project.settings_json or {}).get("funnel_mapping") or {}

    page_text = await _project_page_text(base_url)

    stages_to_name = [k for k in STAGE_ORDER if k.startswith("activation_") or k in mapping]
    prompt = (
        f"Проект «{project_name}», сайт: {base_url or 'неизвестен'}.\n"
        f"Текст главной страницы: {page_text or '(недоступен)'}\n\n"
        f"Метрики продукта по шагам воронки: {json.dumps(mapping, ensure_ascii=False)}\n\n"
        f"Назови шаги воронки простыми русскими словами, как их назвал бы владелец "
        f"продукта: что именно сделал пользователь на этом шаге (например «Создал канал», "
        f"«Загрузил файл», «Пригласил команду»). Нужны названия для ключей: "
        f"{', '.join(stages_to_name)}.\n"
        f"Ответь ТОЛЬКО JSON-объектом вида {{\"ключ\": \"Название\"}}, без пояснений. "
        f"Название — 1-3 слова, глагол в прошедшем времени или существительное."
    )
    answer = await ask_module.answer_question(
        prompt,
        "Ты помогаешь назвать шаги воронки подключаемого продукта.",
        settings,
    )
    if not answer:
        raise HTTPException(status_code=502, detail="LLM не ответил, попробуйте ещё раз")

    match = re.search(r"\{.*\}", answer, re.S)
    if not match:
        raise HTTPException(status_code=502, detail="LLM вернул ответ не в формате JSON")
    try:
        proposed = json.loads(match.group(0))
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Не удалось разобрать ответ LLM")

    proposed = {k: str(v)[:60] for k, v in proposed.items() if k in STAGE_ORDER and v}
    return {"ok": True, "proposed": proposed, "site_used": bool(page_text)}


# ---------------------------------------------------------------------------
# Названия событий живой ленты (B5)
# ---------------------------------------------------------------------------
#
# Шаги воронки у аналитика фиксированные, а типы событий -- нет: продукт
# присылает свои коды (`user_registered`, `trial_extended`, что угодно).
# Незнакомый код показывался владельцу как есть -- английский snake_case
# в русском интерфейсе. Придумывать перевод самим нельзя: код может значить
# что угодно, и выдуманное название врало бы про продукт. Поэтому: собираем
# коды, которые ПРИСЛАЛ продукт, а называет их владелец -- руками или
# приняв предложение ИИ.

EVENT_TYPES_PERIOD_MINUTES = 7 * 24 * 60  # неделя: за сутки редкие события не попадаются


async def _observed_event_types(project: Project) -> tuple[list[str], Optional[str]]:
    """Типы событий, реально присланные продуктом. Второй элемент -- причина,
    по которой список пуст (её показываем владельцу вместо пустоты)."""
    from app.connectors.user_events import fetch_user_events

    result = await fetch_user_events(
        project.base_url,
        project_internal_api_token(project),
        period_minutes=EVENT_TYPES_PERIOD_MINUTES,
        limit=500,
    )
    if not result.get("ok"):
        hint = {
            "not_configured": "У проекта не задан адрес или токен внутреннего API.",
            "not_found": "Продукт не отдаёт /api/internal/user-events — событий в ленте не будет.",
            "timeout": "Продукт не ответил вовремя.",
        }.get(result.get("status"), "Продукт не отдал события.")
        return [], hint

    types = sorted({e.get("event_type") for e in (result.get("events") or []) if e.get("event_type")})
    if not types:
        return [], "За последнюю неделю продукт не прислал ни одного события."
    return types, None


@router.get("/api/projects/{project_id}/events", dependencies=[Depends(require_admin)])
async def project_event_types(project_id: int, identity=Depends(require_admin)):
    """Типы событий проекта и их названия. Уже названные показываем всегда,
    даже если за неделю такое событие не приходило: иначе имя, данное
    владельцем, пропадало бы из настроек вместе с затишьем в продукте."""
    with get_session() as session:
        project = _owned_project(session, project_id, identity)
        labels = (project.settings_json or {}).get("event_labels") or {}
        detached = Project(
            id=project.id, name=project.name, type=project.type,
            base_url=project.base_url, settings_json=dict(project.settings_json or {}),
        )

    observed, hint = await _observed_event_types(detached)
    all_types = sorted(set(observed) | set(labels))
    return {
        "types": [
            {"key": key, "label": labels.get(key, ""), "observed": key in observed}
            for key in all_types
        ],
        "hint": hint if not all_types else None,
        "period_days": EVENT_TYPES_PERIOD_MINUTES // (24 * 60),
    }


@router.post("/api/projects/{project_id}/events/autoname", dependencies=[Depends(require_admin)])
async def autoname_event_types(project_id: int, identity=Depends(require_admin)):
    """ИИ предлагает русские названия для кодов событий. Ничего не
    сохраняет: подтверждает владелец -- аналитик не решает за него."""
    import json
    import re

    from app import ask as ask_module

    settings = get_settings()
    if not ask_module.is_configured(settings):
        raise HTTPException(status_code=503, detail="Для автоназвания нужен настроенный LLM (LLM_PROVIDER=yandex)")

    with get_session() as session:
        project = _owned_project(session, project_id, identity)
        project_name = project.name
        base_url = project.base_url
        detached = Project(
            id=project.id, name=project.name, type=project.type,
            base_url=project.base_url, settings_json=dict(project.settings_json or {}),
        )

    observed, hint = await _observed_event_types(detached)
    if not observed:
        raise HTTPException(status_code=422, detail=hint or "Продукт не прислал ни одного события")

    page_text = await _project_page_text(base_url)
    prompt = (
        f"Проект «{project_name}», сайт: {base_url or 'неизвестен'}.\n"
        f"Текст главной страницы: {page_text or '(недоступен)'}\n\n"
        f"Продукт присылает события таких типов: {', '.join(observed)}.\n\n"
        f"Назови каждое событие простыми русскими словами — так, как владелец "
        f"продукта рассказал бы, что сделал человек: «Зарегистрировался», "
        f"«Открыл тарифы», «Оплатил». Если по коду непонятно, что произошло, "
        f"не выдумывай — пропусти этот ключ.\n"
        f"Ответь ТОЛЬКО JSON-объектом вида {{\"код\": \"Название\"}}, без пояснений. "
        f"Название — 1-3 слова."
    )
    answer = await ask_module.answer_question(
        prompt, "Ты помогаешь назвать события продукта для живой ленты.", settings,
    )
    if not answer:
        raise HTTPException(status_code=502, detail="LLM не ответил, попробуйте ещё раз")

    match = re.search(r"\{.*\}", answer, re.S)
    if not match:
        raise HTTPException(status_code=502, detail="LLM вернул ответ не в формате JSON")
    try:
        proposed = json.loads(match.group(0))
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Не удалось разобрать ответ LLM")

    # Только те коды, которые продукт действительно присылал: иначе ИИ
    # придумает события, которых нет, и владелец решит, что они есть.
    proposed = {k: str(v)[:60] for k, v in proposed.items() if k in observed and v}
    return {"ok": True, "proposed": proposed, "site_used": bool(page_text)}


# ---------------------------------------------------------------------------
# Реклама: деньги и результат в одной таблице
# ---------------------------------------------------------------------------


@router.get("/api/ads", dependencies=[Depends(require_admin)])
async def ads_overview(identity=Depends(require_admin)):
    """
    Связка «откуда пришли» и «сколько это стоило». Расход и клики приходят
    из Директа, а что из этих людей выросло -- из разреза по источникам
    самого продукта. Цену регистрации считаем ТОЛЬКО для Директа: расход
    известен именно по нему, приписывать его другим источникам нельзя.
    """
    from app.connectors.traffic_sources import parse_source_breakdown

    with get_session() as session:
        project = _active_project(session, identity)
        ctx = _report_context(session, project)
        stage_titles = load_stage_titles(session, project.id)
        # Статусы необязательных источников: без них аналитик работает,
        # просто не знает про деньги и трафик. Интерфейс должен объяснять
        # это точно, а не догадываться по отсутствию чисел.
        optional_sources = {
            i.type.value: {
                "status": i.status.value,
                "last_error": i.last_error,
                "error_human": (humanize_error(i.last_error, i.type.value)
                                if i.status.value == "error" else None),
            }
            for i in session.exec(
                select(Integration).where(Integration.project_id == project.id)
            ).all()
            if i.type.value in ("direct", "metrika", "yookassa")
        }

    metrics = ctx["metrics"]
    pp = ctx["payment_path"]
    spend = getattr(metrics, "spend", None) if metrics else None
    clicks = getattr(metrics, "clicks", None) if metrics else None

    breakdown = parse_source_breakdown(pp) or {}
    rows = []
    for source_key, data in breakdown.items():
        if not isinstance(data, dict):
            continue
        regs = data.get("registrations")
        is_direct = source_key in ("yandex_direct", "direct")
        rows.append({
            "source": source_key,
            "registrations": regs,
            "activation": data.get("channels_created"),
            "pricing_viewed": data.get("pricing_viewed"),
            "payment_started": data.get("payment_started"),
            "payment_success": data.get("payment_success"),
            "spend": spend if is_direct else None,
            "clicks": clicks if is_direct else None,
            "cpa": (round(spend / regs) if is_direct and spend and regs else None),
        })
    rows.sort(key=lambda r: r["registrations"] or 0, reverse=True)

    # Цену регистрации считаем только по Директу: делить его расход на
    # регистрации из всех источников (включая бесплатные) -- значит занижать
    # реальную цену. Если разреза нет, честно говорим, что делили на всё.
    direct_regs = next((r["registrations"] for r in rows
                        if r["source"] in ("yandex_direct", "direct")), None)
    cpa_base = direct_regs if direct_regs else (pp or {}).get("registrations")
    return {
        "period": "7d",
        "totals": {
            "spend": spend,
            "clicks": clicks,
            "registrations": (pp or {}).get("registrations"),
            "payment_success": (pp or {}).get("payment_success"),
            "cpa": round(spend / cpa_base) if spend and cpa_base else None,
            "cpa_basis": ("регистрации из Директа" if direct_regs
                          else "все регистрации — разреза по источникам нет"),
        },
        "by_source": rows,
        "stage_titles": stage_titles,
        "sources": optional_sources,
        "as_of": ctx["snapshot_dt"].isoformat() if ctx["snapshot_dt"] else None,
    }


@router.post("/api/ads/deep-check", dependencies=[Depends(require_admin)])
async def ads_deep_check(identity=Depends(require_admin)):
    """
    Глубокая проверка Директа: granular-отчёты по фразам и площадкам --
    какие запросы жгут бюджет без регистраций. Дорогая операция (десятки
    секунд), поэтому только по кнопке, а не в обычном цикле.
    """
    from app.commercial_report import build_deep_direct_status
    from app.config import MANUAL_DEEP_DIRECT_TIMEOUT_SECONDS
    from app.scheduler import force_refresh_deep_diagnostics_sync_with_timeout

    with get_session() as session:
        project = _active_project(session, identity)
        project_id = project.id

    result = await asyncio.to_thread(
        force_refresh_deep_diagnostics_sync_with_timeout,
        project_id,
        MANUAL_DEEP_DIRECT_TIMEOUT_SECONDS,
    )
    text = None
    try:
        text = build_deep_direct_status(result.get("result") if result.get("ok") else None)
    except Exception:
        logger.exception("deep-check: не удалось собрать текст отчёта")
    return {
        "ok": bool(result.get("ok")),
        "timeout": bool(result.get("timeout")),
        "error": result.get("error"),
        "text": text,
        "result": result.get("result"),
    }


# ---------------------------------------------------------------------------
# История решений: что предлагали, что приняли, чем кончилось
# ---------------------------------------------------------------------------


@router.get("/api/history", dependencies=[Depends(require_admin)])
async def decisions_history(limit: int = 50, identity=Depends(require_admin)):
    """
    Журнал цикла роста. Ценность накапливается именно здесь: видно, какие
    гипотезы подтверждались, а какие нет -- и не предлагать снова то,
    что уже проверено.
    """
    from app.models import GrowthExperiment, GrowthRecommendation

    with get_session() as session:
        project = _active_project(session, identity)
        recs = session.exec(
            select(GrowthRecommendation)
            .where(GrowthRecommendation.project_id == project.id)
            .order_by(GrowthRecommendation.created_at.desc())
            .limit(limit)
        ).all()
        exps = {}
        for e in session.exec(
            select(GrowthExperiment).where(GrowthExperiment.project_id == project.id)
        ).all():
            exps[e.recommendation_id] = e

        items = []
        for r in recs:
            exp = exps.get(r.id)
            items.append({
                "id": r.id,
                "title": r.title,
                "area": r.area,
                "action": r.action,
                "hypothesis": r.hypothesis,
                "status": r.status.value,
                "created_at": r.created_at.isoformat(),
                "decided_at": r.decided_at.isoformat() if r.decided_at else None,
                "reject_reason": r.reject_reason,
                "experiment": None if exp is None else {
                    "id": exp.id,
                    "status": exp.status.value,
                    "verdict": exp.verdict,
                    "result_summary": exp.result_summary,
                    "started_at": exp.started_at.isoformat(),
                    "ended_at": exp.ended_at.isoformat() if exp.ended_at else None,
                    "current_sample": exp.current_sample,
                    "target_sample": exp.target_sample,
                },
            })
        return {"items": items}


# ---------------------------------------------------------------------------
# Передача задачи в разработку (Claude Code через GitHub Issue)
# ---------------------------------------------------------------------------


class DevTaskRequest(BaseModel):
    title: str
    body: str
    repo: Optional[str] = None   # owner/repo; по умолчанию из GITHUB_TASK_REPO
    mention_claude: bool = True


@router.post("/api/dev-task", dependencies=[Depends(require_admin)])
async def create_dev_task(body: DevTaskRequest, identity=Depends(require_admin)):
    """
    Создаёт issue в GitHub с диагнозом аналитика. Если в репозитории
    настроен workflow Claude Code (реагирует на @claude), задача уходит
    сразу в работу: правки лендинга и продукта делает Claude, аналитик
    остаётся источником фактов, а не исполнителем.

    Токен и репозиторий -- в переменных окружения аналитика; никакие
    ключи через интерфейс не передаются.
    """
    # Задача уходит в репозиторий владельца платформы, поэтому кнопка
    # доступна только ему: клиент не должен заводить задачи в чужом репозитории.
    if identity is not None and identity.user_id is not None:
        raise HTTPException(status_code=403, detail="Постановка задач доступна только владельцу платформы")

    settings = get_settings()
    token = settings.github_task_token
    repo = body.repo or settings.github_task_repo
    if not token or not repo:
        raise HTTPException(
            status_code=503,
            detail="Не настроено: задайте GITHUB_TASK_TOKEN и GITHUB_TASK_REPO в переменных окружения аналитика",
        )

    text = body.body
    if body.mention_claude:
        text = (
            "@claude\n\n" + text +
            "\n\n---\nЗадачу поставил Аналитик Воронки на основании своих данных. "
            "Числа выше — факты из продакшена, не выдуманные примеры."
        )

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"https://api.github.com/repos/{repo}/issues",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                json={"title": body.title[:250], "body": text},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub недоступен: {exc.__class__.__name__}")

    if resp.status_code >= 300:
        raise HTTPException(status_code=502, detail=f"GitHub ответил {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return {"ok": True, "url": data.get("html_url"), "number": data.get("number")}


# ---------------------------------------------------------------------------
# Действия по сигналам
# ---------------------------------------------------------------------------


@router.post("/api/alerts/{alert_id}/{action}", dependencies=[Depends(require_admin)])
async def alert_action(alert_id: int, action: str, identity=Depends(require_admin)):
    """«Понял» (acknowledged) и «Отложить» (snoozed на сутки) -- то же, что
    кнопки под сигналом в Telegram."""
    from datetime import timedelta

    from app.models import AlertStatus, utcnow

    if action not in ("ack", "snooze"):
        raise HTTPException(status_code=404, detail="Неизвестное действие")

    with get_session() as session:
        alert = session.get(Alert, alert_id)
        if alert is None:
            raise HTTPException(status_code=404, detail="Сигнал не найден")
        _require_own_project_id(session, alert.project_id, identity)
        if action == "ack":
            alert.status = AlertStatus.acknowledged
        else:
            alert.status = AlertStatus.snoozed
            alert.snooze_until = utcnow() + timedelta(hours=24)
        session.add(alert)
        session.commit()
        return {"ok": True, "status": alert.status.value}


@router.post("/api/run", dependencies=[Depends(require_admin)])
async def run_cycle(identity=Depends(require_admin)):
    from app.scheduler import run_cycle_once_sync_with_timeout

    # Явно передаём проект пришедшего: без этого кнопка «Проверить сейчас»
    # у клиента запускала бы цикл по чужому активному проекту.
    with get_session() as session:
        project_id = _active_project(session, identity).id

    try:
        result = await asyncio.to_thread(
            run_cycle_once_sync_with_timeout, project_id, RUN_CYCLE_TIMEOUT_SECONDS, "platform_run",
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
async def ask(body: AskRequest, identity=Depends(require_admin)):
    from app import ask as ask_module

    settings = get_settings()
    if not ask_module.is_configured(settings):
        raise HTTPException(
            status_code=503,
            detail="LLM не настроен: задайте LLM_PROVIDER=yandex и YANDEX_API_KEY/YANDEX_FOLDER_ID",
        )
    with get_session() as session:
        project = _active_project(session, identity)
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


@router.get("/api/connect-snippet", dependencies=[Depends(require_admin)])
async def connect_snippet(stack: str = connect_snippets.DEFAULT_STACK):
    """Готовый код endpoint'а под стек клиента. Самый дорогой шаг
    подключения -- написать его самому по документу; здесь он выдан кодом."""
    return {
        "stacks": connect_snippets.available_stacks(),
        **connect_snippets.build_snippet(stack),
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
        "notify_chat_ids": sj.get("notify_chat_ids") or [],
        "created_at": p.created_at.isoformat(),
    }


@router.get("/api/projects", dependencies=[Depends(require_admin)])
async def list_projects(identity=Depends(require_admin)):
    with get_session() as session:
        visible = _visible_project_ids(session, identity)
        query = select(Project).order_by(Project.id)
        if visible is not None:
            if not visible:
                return []
            query = query.where(Project.id.in_(visible))
        return [_project_to_dict(p) for p in session.exec(query).all()]


@router.post("/api/projects", dependencies=[Depends(require_admin)])
async def create_project(body: ProjectCreateRequest, identity=Depends(require_admin)):
    from app.connectors.truepost import DEFAULT_FUNNEL_MAPPING

    probe = await _probe_internal_api(body.base_url, body.internal_api_token)
    if not probe["ok"]:
        raise HTTPException(status_code=422, detail={"message": "Подключение не прошло проверку", "probe": probe})

    with get_session() as session:
        # Название уникально в пределах владельца, а не всей платформы:
        # иначе первый клиент, назвавший проект «Магазин», занял бы это
        # слово для всех остальных.
        visible = _visible_project_ids(session, identity)
        same_name = session.exec(select(Project).where(Project.name == body.name)).all()
        if visible is not None:
            same_name = [p for p in same_name if p.id in visible]
        if same_name:
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
        # Владение записывается сразу: проект, созданный клиентом, должен
        # быть его. Вход по паролю из окружения -- владелец платформы, у него
        # аккаунта может не быть, и связывать не с кем.
        if identity is not None and identity.user_id is not None:
            accounts.grant_project(session, project.id, identity.user_id)
        return {"ok": True, "project": _project_to_dict(project), "probe": probe}


@router.patch("/api/projects/{project_id}", dependencies=[Depends(require_admin)])
async def update_project(project_id: int, body: ProjectUpdateRequest, identity=Depends(require_admin)):
    with get_session() as session:
        project = _owned_project(session, project_id, identity)

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
        if body.notify_chat_ids is not None:
            # Пустой список -- осознанный выбор «не слать никуда», а не
            # ошибка: платформа тогда молчит и говорит об этом в интерфейсе.
            sj["notify_chat_ids"] = [str(c).strip() for c in body.notify_chat_ids if str(c).strip()]
        project.settings_json = sj

        session.add(project)
        session.commit()
        session.refresh(project)
        return {"ok": True, "project": _project_to_dict(project)}


@router.post("/api/projects/{project_id}/activate", dependencies=[Depends(require_admin)])
async def activate_project(project_id: int, identity=Depends(require_admin)):
    """Включает сбор данных по проекту.

    Раньше активным мог быть ровно один проект на всю платформу -- цикл брал
    первый попавшийся активный, и включение своего проекта останавливало
    сбор у соседа. Планировщик теперь обходит все включённые проекты
    (`run_cycle_for_all_active`), поэтому выключать чужие больше не нужно
    и нельзя.
    """
    with get_session() as session:
        project = _owned_project(session, project_id, identity)
        project.is_active = True
        session.add(project)
        session.commit()
        return {"ok": True, "is_active": True}


@router.post("/api/projects/{project_id}/pause", dependencies=[Depends(require_admin)])
async def pause_project(project_id: int, identity=Depends(require_admin)):
    """Выключает сбор. Нужен как пара к включению: иначе сбор можно только
    завести и никогда не остановить -- например, на время переезда продукта,
    когда его данные всё равно врут."""
    with get_session() as session:
        project = _owned_project(session, project_id, identity)
        project.is_active = False
        session.add(project)
        session.commit()
        return {"ok": True, "is_active": False}


@router.post("/api/projects/{project_id}/retest", dependencies=[Depends(require_admin)])
async def retest_project(project_id: int, identity=Depends(require_admin)):
    with get_session() as session:
        project = _owned_project(session, project_id, identity)
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
