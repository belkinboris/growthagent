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
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import or_, select

from app.config import (
    ANALYSIS_WINDOWS_HOURS,
    BUILD_MARKER,
    CORE_FUNNEL_KEYS,
    RUN_CYCLE_TIMEOUT_SECONDS,
    get_settings,
)
from app import accounts, connect_snippets
from app.error_text import humanize_error
from app.readiness import (
    CHECKS,
    ENOUGH_FOR_A_CONCLUSION,
    MIN_FOR_A_TREND,
    WINDOW_DAYS,
    assess,
)
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
    # Чат конкретной вкладки-агента сужает контекст до её данных; общий чат
    # на доске фаундера не передаёт agent вовсе -- видит всё, как раньше.
    agent: Optional[str] = None


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


# Сигнал считается "требующим внимания" (виден в списке и в счётчике),
# пока владелец не сказал обратное: acknowledged/resolved -- это "понял" и
# "решилось", их прятать; snoozed виден заново, как только истёк снуз.
# Общая функция для /api/overview (счётчик) и /api/alerts (сам список) --
# раньше они считали по-разному, и счётчик наверху не совпадал со списком.
def _visible_alerts_filter():
    from app.models import utcnow
    return or_(
        Alert.status.in_(["open", "sent", "escalated"]),
        (Alert.status == "snoozed")
        & or_(Alert.snooze_until.is_(None), Alert.snooze_until <= utcnow()),
    )


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
            select(Alert).where(Alert.project_id == project.id, _visible_alerts_filter())
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
            .where(Alert.project_id == project.id, _visible_alerts_filter())
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


# ---------------------------------------------------------------------------
# Доска фаундера: сигналы и рекомендация — одним списком, с ярлыком «кто нашёл»
# ---------------------------------------------------------------------------
#
# Раньше, чтобы понять, с чего начать день, нужно было обойти по кругу
# «Обзор» (сигналы), «Реклама» (трафик), «Историю» (эксперименты) — каждый
# честно отвечал на свой вопрос, но собрать из них одну картину «что делать
# сначала» мог только сам владелец. Здесь то же самое, что уже показывают
# /api/alerts и /api/growth, просто сведено в один список и подписано, какой
# агент (вкладка) отвечает за эту проблему -- чтобы понять, куда идти
# разбираться дальше.
#
# Категории/области -- то, что УЖЕ решает rules.py/growth_loop.py; здесь
# только подпись, кто за какую область отвечает на экране. Если область
# окажется на стыке (так и есть с onboarding/pricing_screen) -- это строка
# в словаре, а не переделка.

CATEGORY_TO_AGENT: dict[str, str] = {
    "traffic_no_signups": "marketer",
    "signups_no_activation": "diagnostician",
    "activation_drop": "diagnostician",
    "payments_started_no_success": "diagnostician",
    "pending_payments": "diagnostician",
    "metrics_discrepancy": "diagnostician",
    # Диагност находит расхождение по данным воронки, но чинить -- цели/
    # пиксели -- хозяйство Маркетолога (задача F6).
    "payments_invisible_in_metrika": "marketer",
    "integration_down": "diagnostician",
}

AREA_TO_AGENT: dict[str, str] = {
    "tracking": "diagnostician",
    "collect_data": "diagnostician",
    "onboarding": "diagnostician",
    "payment_path": "diagnostician",
    "scale": "diagnostician",
    "pricing_screen": "marketer",
    "first_post": "product",
    "collect_feedback": "product",
    "commercial_bridge": "product",
}

AGENT_TITLES: dict[str, str] = {
    "diagnostician": "Диагност", "marketer": "Маркетолог",
    "product": "Продакт", "tester": "Тестировщик",
}

# Уровни автономии (задача F6) -- один переключатель на проект, а не по
# одному на каждого агента: владелец договаривается с "аналитиком как с
# гендиром" целиком, а не настраивает четыре независимых роли.
AUTONOMY_LEVELS = {
    1: {
        "title": "Гендир только докладывает",
        "description": "Все агенты находят и предлагают. Ничего не применяется "
                        "без вашей кнопки.",
    },
    2: {
        "title": "Гендир решает мелкое сам",
        "description": "Обратимые мелкие правки (минус-слова, цели в Метрике для "
                        "уже надёжно фиксируемых событий) применяются сами. "
                        "Всё крупное -- на ваше решение.",
    },
    3: {
        "title": "Гендир действует, отчитывается постфактум",
        "description": "Всё, что технически можно записать (цели Метрики, "
                        "ставки и минус-слова Директа), применяется автоматически. "
                        "Выключено по умолчанию -- включайте осознанно.",
    },
}


def autonomy_level(project: Project) -> int:
    """Текущий уровень делегирования для проекта. По умолчанию 1 -- ничего
    не меняется автоматически, ровно как было до F6."""
    try:
        level = int((project.settings_json or {}).get("autonomy_level", 1))
    except (TypeError, ValueError):
        level = 1
    return level if level in AUTONOMY_LEVELS else 1


AGENT_ACTION_STATUS_LABELS = {
    "proposed": "предложено",
    "applied": "применено само",
    "rejected": "отклонено",
    "blocked_not_configured": "не настроено",
}


def _agent_action_to_dict(a) -> dict:
    return {
        "id": a.id, "agent": a.agent, "agent_title": AGENT_TITLES.get(a.agent, a.agent),
        "domain": a.domain, "action": a.action, "reasoning": a.reasoning,
        "payload": a.payload_json or {}, "status": a.status,
        "status_label": AGENT_ACTION_STATUS_LABELS.get(a.status, a.status),
        "created_at": a.created_at.isoformat(),
        "applied_at": a.applied_at.isoformat() if a.applied_at else None,
    }


@router.get("/api/dashboard", dependencies=[Depends(require_admin)])
async def dashboard(identity=Depends(require_admin)):
    """Открытые проблемы всех агентов одним списком -- доска фаундера."""
    from app import growth_loop
    from app.models import AgentAction

    with get_session() as session:
        project = _active_project(session, identity)

        alert_rows = session.exec(
            select(Alert).where(Alert.project_id == project.id, _visible_alerts_filter())
            .order_by(Alert.last_seen_at.desc())
        ).all()

        rec = growth_loop.get_active_recommendation(session, project.id)
        running = growth_loop.get_running_experiment(session, project.id)

        # "Что сделали агенты" -- задача F6: последние предложения/действия
        # агентов отдельной секцией, независимо от их статуса, чтобы было
        # видно и то, что применилось само, и то, что предложено, и то, что
        # агент хотел сделать сам, но не смог (не настроена запись).
        agent_action_rows = session.exec(
            select(AgentAction).where(AgentAction.project_id == project.id)
            .order_by(AgentAction.created_at.desc()).limit(20)
        ).all()
        level = autonomy_level(project)

    cards = []
    for a in alert_rows:
        agent = CATEGORY_TO_AGENT.get(a.category.value, "diagnostician")
        cards.append({
            "agent": agent, "agent_title": AGENT_TITLES[agent],
            "source": "alert", "id": a.id, "severity": a.severity.value,
            "title": a.title, "message": a.message,
            "found_by": f"{AGENT_TITLES[agent]} обнаружил",
            "tested_by": None,
            "actions": [{"action": "ack", "label": "Понял"},
                        {"action": "snooze", "label": "Отложить на сутки"}],
        })

    if rec is not None:
        # Ждёт решения владельца: принять можно только одну рекомендацию за
        # раз (см. growth_loop.propose_if_needed), поэтому пока она не
        # принята, эксперимента по ней ещё нет.
        agent = AREA_TO_AGENT.get(rec.area, "diagnostician")
        cards.append({
            "agent": agent, "agent_title": AGENT_TITLES[agent],
            "source": "recommendation", "id": rec.id, "severity": None,
            "title": rec.title, "message": rec.action,
            "found_by": f"{AGENT_TITLES[agent]} предложил",
            "tested_by": None,
            "actions": [{"action": "accept", "label": "Сделаю"},
                        {"action": "reject", "label": "Не буду"}],
        })
    elif running is not None:
        # Рекомендацию уже приняли -- она больше не "proposed", но проверка
        # идёт, и владельцу важно видеть её на доске, а не только во вкладке
        # Тестировщика.
        agent = AREA_TO_AGENT.get(running.area, "diagnostician")
        cards.append({
            "agent": agent, "agent_title": AGENT_TITLES[agent],
            "source": "experiment", "id": running.id, "severity": None,
            "title": running.title, "message": running.hypothesis,
            "found_by": f"{AGENT_TITLES[agent]} предложил",
            "tested_by": f"{AGENT_TITLES['tester']} проверяет: "
                        f"{running.current_sample}/{running.target_sample}",
            "actions": [{"action": "cancel", "label": "Отменить проверку"}],
        })

    return {
        "cards": cards, "hint": None if cards else
            "Открытых проблем нет: по всем проверяемым правилам всё в норме.",
        "autonomy_level": level,
        "autonomy_levels": {str(k): v for k, v in AUTONOMY_LEVELS.items()},
        "agent_actions": [_agent_action_to_dict(a) for a in agent_action_rows],
    }


@router.post("/api/agent-actions/{action_id}/{decision}", dependencies=[Depends(require_admin)])
async def agent_action_decide(action_id: int, decision: str, identity=Depends(require_admin)):
    """Решение владельца по предложению агента (не по тому, что агент уже
    применил сам -- те статусы менять поздно, действие уже сделано)."""
    from app.models import AgentAction, AgentActionStatus, utcnow

    if decision not in ("apply", "reject"):
        raise HTTPException(status_code=404, detail="Неизвестное действие")
    with get_session() as session:
        action = session.get(AgentAction, action_id)
        if action is None:
            raise HTTPException(status_code=404, detail="Действие не найдено")
        _require_own_project_id(session, action.project_id, identity)
        if action.status != AgentActionStatus.proposed.value:
            raise HTTPException(status_code=409, detail="По этому предложению уже есть решение")

        if decision == "apply":
            # Владелец применил предложение сам (руками, вне платформы) --
            # фиксируем как решённое, без вызова write-клиента: применить
            # автоматически может только сам агент на уровне автономии 3.
            action.status = AgentActionStatus.applied.value
            action.applied_at = utcnow()
        else:
            action.status = AgentActionStatus.rejected.value
        session.add(action)
        session.commit()
        _record_action(
            session, identity,
            "agent_action_applied" if decision == "apply" else "agent_action_rejected",
            f"«{action.reasoning[:120]}»" + (" -- сделал сам" if decision == "apply" else " -- отклонил"),
            action.project_id,
        )
        return {"ok": True, "status": action.status}


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
            _record_action(session, identity, "recommendation_accepted",
                           f"Принял предложение «{rec.title}» — началась проверка", project.id)
            return {"ok": True, "experiment_id": exp.id if exp else None}
        if action == "defer":
            growth_loop.defer_recommendation(session, rec, days=body.days)
            _record_action(session, identity, "recommendation_deferred",
                           f"Отложил предложение «{rec.title}» на {body.days} дн.", project.id)
            return {"ok": True}
        growth_loop.reject_recommendation(session, rec, reason=body.reason)
        _record_action(session, identity, "recommendation_rejected",
                       f"Отклонил предложение «{rec.title}»"
                       + (f": {body.reason}" if body.reason else ""), project.id)
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
        _record_action(session, identity, "experiment_cancelled",
                       f"Остановил проверку «{exp.title}»"
                       + (f": {body.reason}" if body and body.reason else ""), exp.project_id)
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
        parts = []
        if body.titles:
            parts.append(f"названия шагов воронки ({len(body.titles)})")
        if body.event_labels:
            parts.append(f"названия событий ленты ({len(body.event_labels)})")
        if parts:
            _record_action(session, identity, "names_changed",
                           "Изменил " + " и ".join(parts), project_id)
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
# Сравнение периодов: неделя к неделе (D2)
# ---------------------------------------------------------------------------
#
# Одно число «за 7 дней» не отвечает на вопрос, ради которого владелец
# открывает экран: стало лучше или хуже. Раньше это можно было понять
# только по графику динамики глазами, а глаз на четырёх точках ошибается.
#
# Сравниваем снимок недельного окна сейчас и такой же снимок недельной
# давности: тогда «предыдущая неделя» -- это ровно предыдущие семь дней,
# посчитанные тем же способом, а не пересобранные задним числом.

# Ниже этого числа событий разница почти наверняка случайна, и показывать
# проценты как факт нельзя. Порог общий для всей платформы (app/readiness.py):
# продукт должен говорить об уверенности одинаково во всех местах.
COMPARE_MIN_SAMPLE = MIN_FOR_A_TREND


@router.get("/api/compare", dependencies=[Depends(require_admin)])
async def compare_periods(identity=Depends(require_admin)):
    """Неделя к предыдущей неделе по каждому шагу воронки."""
    from app.models import utcnow
    from app.service import extract_normalized_metrics_from_snapshot

    with get_session() as session:
        project = _active_project(session, identity)
        stage_titles = load_stage_titles(session, project.id)

        def _snapshot(before=None):
            query = (
                select(MetricSnapshot)
                .where(MetricSnapshot.project_id == project.id)
                .where(MetricSnapshot.period_key == "7d")
                .where(MetricSnapshot.source.in_(("combined", "project_metrics_api")))
            )
            if before is not None:
                query = query.where(MetricSnapshot.created_at <= before)
            return session.exec(query.order_by(MetricSnapshot.created_at.desc())).first()

        current = _snapshot()
        previous = _snapshot(before=utcnow() - timedelta(days=7))

        if current is None:
            return {"ok": False, "rows": [],
                    "hint": "Снимков ещё нет. Первое сравнение появится, когда аналитик "
                            "соберёт данные хотя бы один раз."}
        if previous is None:
            return {
                "ok": False, "rows": [],
                "hint": "Сравнивать пока не с чем: аналитик наблюдает меньше двух недель. "
                        "Сравнение появится, когда накопится вторая неделя.",
            }

        now_values = extract_normalized_metrics_from_snapshot(current)
        was_values = extract_normalized_metrics_from_snapshot(previous)

    rows = []
    for key in ("signup", "activation_1", "activation_2", "payment_started", "payment_success"):
        now_v, was_v = now_values.get(key), was_values.get(key)
        if now_v is None and was_v is None:
            continue
        rows.append(_compare_row(key, stage_titles.get(key, key), now_v, was_v))

    return {
        "ok": True,
        "rows": rows,
        "current_at": current.created_at.isoformat(),
        "previous_at": previous.created_at.isoformat(),
        "hint": None,
    }


def _compare_row(key: str, title: str, now_v, was_v) -> dict:
    """Одна строка сравнения. Проценты считаем, только когда они что-то
    значат: на двух событиях «рост на 100%» -- это шум, а не рост."""
    now_n = None if now_v is None else float(now_v)
    was_n = None if was_v is None else float(was_v)
    delta = None if (now_n is None or was_n is None) else now_n - was_n
    percent = None
    if delta is not None and was_n:
        percent = round(delta / was_n * 100)

    biggest = max(now_n or 0, was_n or 0)
    if now_n is None or was_n is None:
        verdict = "Не с чем сравнить: данных за одну из недель нет."
    elif biggest < COMPARE_MIN_SAMPLE:
        # Честность про выборку -- часть продукта: маленькие числа
        # колеблются сами по себе, и называть это динамикой нельзя.
        verdict = "Событий слишком мало, чтобы говорить о динамике."
    elif delta == 0:
        verdict = "Без изменений."
    elif delta > 0:
        verdict = "Стало больше."
    else:
        verdict = "Стало меньше."

    return {
        "key": key, "title": title,
        "now": now_v, "was": was_v, "delta": delta, "percent": percent,
        "verdict": verdict,
        "reliable": bool(now_n is not None and was_n is not None and biggest >= COMPARE_MIN_SAMPLE),
    }


# ---------------------------------------------------------------------------
# Тариф платформы (E1/E2)
# ---------------------------------------------------------------------------
#
# COMMERCIAL_MODE выключен по умолчанию -- пока платформа обслуживает
# только своего владельца, лимитов и оплаты нет вообще. Когда владелец
# решит отдавать аналитик другим фаундерам за деньги, включение одной
# переменной делает лимиты и кнопку оплаты видимыми, без правок кода.


@router.get("/api/billing/plans", dependencies=[Depends(require_admin)])
async def billing_plans(identity=Depends(require_admin)):
    from app import accounts
    from app.plans import PLANS, current_plan

    settings = get_settings()
    with get_session() as session:
        plan = current_plan(session, identity.user_id)
        owned = len(accounts.user_project_ids(session, identity.user_id)) if identity.user_id else None

    return {
        "commercial_mode": settings.commercial_mode,
        "current_plan": plan,
        "plans": [{"key": key, **info} for key, info in PLANS.items()],
        "usage": {"projects": owned},
    }


class CheckoutRequest(BaseModel):
    plan: str


@router.post("/api/billing/checkout", dependencies=[Depends(require_admin)])
async def billing_checkout(body: CheckoutRequest, identity=Depends(require_admin)):
    from app import billing_platform
    from app.models import PlatformSubscription
    from app.plans import PLANS

    settings = get_settings()
    if not settings.commercial_mode:
        raise HTTPException(status_code=404, detail="Оплата тарифов сейчас не предлагается")
    if identity.user_id is None:
        raise HTTPException(status_code=400, detail="Оплата привязывается к аккаунту — войдите по почте")
    plan_info = PLANS.get(body.plan)
    if plan_info is None or body.plan == "free":
        raise HTTPException(status_code=400, detail="Неизвестный или бесплатный тариф")

    try:
        payment = await billing_platform.create_checkout(
            settings, user_id=identity.user_id, plan=body.plan,
            price_rub=plan_info["price_rub"],
            description=f"Аналитик Воронки — тариф {plan_info['title']}",
        )
    except billing_platform.YooKassaError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    with get_session() as session:
        session.add(PlatformSubscription(
            user_id=identity.user_id, plan=body.plan, status="pending",
            price_rub=plan_info["price_rub"], payment_id=payment["id"],
        ))
        session.commit()

    return {"ok": True, "confirmation_url": payment["confirmation"]["confirmation_url"]}


@router.post("/api/billing/yookassa/notify")
async def billing_yookassa_notify(request: Request):
    """Вебхук ЮKassa. Без require_admin -- вызывает ЮKassa, не владелец.

    Вебхуку самому не доверяем (его можно подделать): по payment_id из
    тела запрашиваем актуальный статус напрямую у ЮKassa и начисляем
    тариф только по её ответу.
    """
    from datetime import timedelta

    from app import billing_platform
    from app.models import PlatformSubscription, utcnow

    body = await request.json()
    payment_id = ((body.get("object") or {}).get("id")) or ""
    if not payment_id:
        return {"ok": True}  # нечего обрабатывать, но и падать незачем

    settings = get_settings()
    try:
        payment = await billing_platform.get_payment(settings, payment_id)
    except billing_platform.YooKassaError:
        logger.exception("billing: не удалось проверить платёж %s в ЮKassa", payment_id)
        return {"ok": True}

    with get_session() as session:
        sub = session.exec(
            select(PlatformSubscription).where(PlatformSubscription.payment_id == payment_id)
        ).first()
        if sub is None:
            return {"ok": True}  # платёж не наш (или уже не найти) -- не ошибка вебхука
        if sub.status == "active":
            return {"ok": True}  # уже начислено -- вебхук может прийти повторно

        if payment.get("status") == "succeeded" and payment.get("paid"):
            sub.status = "active"
            sub.paid_until = utcnow() + timedelta(days=30)
        elif payment.get("status") in ("canceled",):
            sub.status = "failed"
        session.add(sub)
        session.commit()

    return {"ok": True}


# ---------------------------------------------------------------------------
# Что изменилось после выкатки (D6)
# ---------------------------------------------------------------------------
#
# Сравнение недель отвечает «стало лучше или хуже», но не отвечает на вопрос,
# который человек задаёт на самом деле: помогло ли то, что я сделал. Календарь
# не знает, когда была выкатка, и изменение, случившееся в среду, размазывается
# по двум неделям сразу.
#
# Отметку ставит человек: аналитик не угадывает по излому графика, что именно
# и когда выкатили. Догадка тут хуже пустоты -- на ней строится вывод «помогло».


class ChangeIn(BaseModel):
    title: str
    description: str | None = None
    at: str | None = None  # ISO-время; пусто -- значит «только что»


def _parse_change_payload(payload: ChangeIn) -> tuple[str, datetime]:
    """Общая проверка для отметки об изменении -- одна и та же что для
    владельца в интерфейсе, что для машины через входящий API (D7):
    правило честности («дата не в будущем») не должно зависеть от того,
    кто именно поставил отметку."""
    from app.models import utcnow

    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Напишите, что именно вы изменили")

    cutoff = utcnow()
    if payload.at:
        try:
            cutoff = datetime.fromisoformat(payload.at.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Не разобрал дату изменения")
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
    # Изменение «в будущем» -- это опечатка в дате, а не план: сравнение по
    # такой отметке потом молча покажет пустоту вместо ошибки.
    if cutoff > utcnow() + timedelta(minutes=5):
        raise HTTPException(status_code=400, detail="Дата изменения — в будущем")
    return title, cutoff


@router.post("/api/projects/{project_id}/changes", dependencies=[Depends(require_admin)])
async def add_project_change(project_id: int, payload: ChangeIn, identity=Depends(require_admin)):
    """Владелец отмечает: «я выкатил X тогда-то»."""
    from app.service import add_change_event

    title, cutoff = _parse_change_payload(payload)

    with get_session() as session:
        _require_own_project_id(session, project_id, identity)
        event = add_change_event(
            session, project_id, title, cutoff,
            description=(payload.description or "").strip() or None,
            created_by="владелец",
        )
        _record_action(session, identity, "change_marked",
                       f"Отметил изменение: {title}", project_id=project_id)
        return {"ok": True, "id": event.id}


# ---------------------------------------------------------------------------
# Вход для машины (D7): продукт или соседний агент сообщает о выкатке сам
# ---------------------------------------------------------------------------
#
# До сих пор отметку мог поставить только человек руками -- удобно один
# раз, но при частых релизах владелец либо забывает отмечать, либо
# перестаёт это делать вовсе, и сравнение «до/после» остаётся неполным.
#
# Токен отдельный от `internal_api_token` (тем платформа ХОДИТ В продукт),
# здесь наоборот -- продукт стучится В платформу, и это другое направление
# доверия: чужой скрипт деплоя не должен получить доступ ни к чему, кроме
# права поставить одну отметку. Хранится не сам токен, а его хэш (тем же
# pbkdf2, что и пароли) -- утечка базы не должна означать утечку токенов.

INBOUND_TOKEN_KEY = "inbound_token_hash"


@router.post("/api/projects/{project_id}/inbound-token", dependencies=[Depends(require_admin)])
async def rotate_inbound_token(project_id: int, identity=Depends(require_admin)):
    """Выпускает новый токен для входящих отметок об изменениях, старый
    (если был) перестаёт работать. Показывается один раз -- как обычно
    для секретов, платформа хранит только хэш и не может показать снова."""
    import secrets

    from app import accounts

    token = secrets.token_urlsafe(32)
    with get_session() as session:
        project = _owned_project(session, project_id, identity)
        project.settings_json = {**(project.settings_json or {}), INBOUND_TOKEN_KEY: accounts.hash_password(token)}
        session.add(project)
        session.commit()
        _record_action(session, identity, "inbound_token_rotated",
                       "Выпустил новый токен для автоматических отметок об изменениях",
                       project_id=project_id)

    return {"ok": True, "token": token,
            "hint": "Сохраните сейчас — второй раз показать не сможем."}


@router.post("/api/public/projects/{project_id}/changes")
async def add_project_change_public(project_id: int, payload: ChangeIn, authorization: str | None = Header(default=None)):
    """Публичный (не требует входа владельца) вход для машины: продукт
    сам сообщает о выкатке. Проверяется токеном проекта, не сессией."""
    from app import accounts
    from app.service import add_change_event

    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Нужен заголовок Authorization: Bearer <токен>")

    with get_session() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Проект не найден")
        stored_hash = (project.settings_json or {}).get(INBOUND_TOKEN_KEY)
        if not stored_hash or not accounts.verify_password(token, stored_hash):
            raise HTTPException(status_code=401, detail="Неверный токен")

        title, cutoff = _parse_change_payload(payload)
        event = add_change_event(
            session, project_id, title, cutoff,
            description=(payload.description or "").strip() or None,
            created_by="продукт (автоматически)",
        )
        return {"ok": True, "id": event.id}


@router.get("/api/projects/{project_id}/changes", dependencies=[Depends(require_admin)])
async def list_project_changes(project_id: int, identity=Depends(require_admin)):
    from app.models import ProjectChangeEvent

    with get_session() as session:
        _require_own_project_id(session, project_id, identity)
        rows = session.exec(
            select(ProjectChangeEvent)
            .where(ProjectChangeEvent.project_id == project_id)
            .order_by(ProjectChangeEvent.cutoff_at.desc())
        ).all()
        # База хранит время без часового пояса, но это UTC. Отдавать его
        # «голым» нельзя: браузер прочитает такую строку как местное время
        # и покажет выкатку на три часа раньше, чем она была.
        return {"changes": [
            {"id": r.id, "title": r.title, "description": r.description,
             "at": _as_utc(r.cutoff_at).isoformat(), "by": r.created_by}
            for r in rows
        ]}


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@router.get("/api/changes/{change_id}/effect", dependencies=[Depends(require_admin)])
async def change_effect(change_id: int, identity=Depends(require_admin)):
    """Что стало с воронкой после этой выкатки."""
    from app.models import ProjectChangeEvent, utcnow
    from app.service import extract_normalized_metrics_from_snapshot

    with get_session() as session:
        event = session.get(ProjectChangeEvent, change_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Изменение не найдено")
        _require_own_project_id(session, event.project_id, identity)
        stage_titles = load_stage_titles(session, event.project_id)

        cutoff = _as_utc(event.cutoff_at)

        def _snapshot(before=None, after=None):
            query = (
                select(MetricSnapshot)
                .where(MetricSnapshot.project_id == event.project_id)
                .where(MetricSnapshot.period_key == "7d")
                .where(MetricSnapshot.source.in_(("combined", "project_metrics_api")))
            )
            if before is not None:
                query = query.where(MetricSnapshot.created_at <= before)
            if after is not None:
                query = query.where(MetricSnapshot.created_at >= after)
            order = MetricSnapshot.created_at.desc() if before is not None \
                else MetricSnapshot.created_at.asc()
            return session.exec(query.order_by(order)).first()

        # «До» -- последний снимок, целиком лежащий до выкатки. «После» --
        # первый снимок, чьё недельное окно началось уже после неё: иначе
        # в одном окне смешаны старая и новая версии продукта, и разница
        # показывает не изменение, а долю дней.
        was = _snapshot(before=cutoff)
        now = _snapshot(after=cutoff + timedelta(days=WINDOW_DAYS))

        head = {"id": event.id, "title": event.title, "description": event.description,
                "at": cutoff.isoformat()}

        if was is None:
            return {**head, "ok": False, "rows": [],
                    "hint": "Снимка до этого изменения нет: аналитик начал наблюдать позже. "
                            "Сравнивать не с чем."}
        if now is None:
            days_left = max(1, math.ceil(
                (cutoff + timedelta(days=WINDOW_DAYS) - utcnow()).total_seconds() / 86400))
            return {**head, "ok": False, "rows": [],
                    "hint": f"Чистой недели после изменения ещё не набралось — осталось около "
                            f"{days_left} дн. До этого срока в окне смешаны старая и новая версия."}

        was_values = extract_normalized_metrics_from_snapshot(was)
        now_values = extract_normalized_metrics_from_snapshot(now)

    rows = []
    for key in ("signup", "activation_1", "activation_2", "payment_started", "payment_success"):
        now_v, was_v = now_values.get(key), was_values.get(key)
        if now_v is None and was_v is None:
            continue
        rows.append(_compare_row(key, stage_titles.get(key, key), now_v, was_v))

    return {
        **head, "ok": True, "rows": rows,
        "was_at": was.created_at.isoformat(), "now_at": now.created_at.isoformat(),
        # Одна точка «до» и одна «после» -- это совпадение во времени, а не
        # доказательство. Сказать это обязан сам экран: иначе владелец
        # припишет изменению чужой результат и будет катить дальше вслепую.
        "caution": "Это совпадение во времени, а не доказательство: в те же дни могли "
                   "измениться реклама, сезон или что-то ещё. Чтобы проверить одну "
                   "причину, запустите эксперимент — он держит остальное неизменным.",
        "hint": None,
    }


# ---------------------------------------------------------------------------
# Когда выводам можно будет верить (D4)
# ---------------------------------------------------------------------------
#
# «Событий слишком мало» аналитик пишет в трёх местах, а когда станет
# достаточно -- не писал нигде. Ожидание без срока выглядит бесконечным,
# и человек либо перестаёт верить экрану, либо принимает решение по числу,
# про которое ему честно сказали «не верьте».


@router.get("/api/readiness", dependencies=[Depends(require_admin)])
async def readiness(identity=Depends(require_admin)):
    """Готов ли каждый вывод и, если нет, чего ждать."""
    from app.service import extract_normalized_metrics_from_snapshot

    with get_session() as session:
        project = _active_project(session, identity)
        stage_titles = load_stage_titles(session, project.id)

        query = (
            select(MetricSnapshot)
            .where(MetricSnapshot.project_id == project.id)
            .where(MetricSnapshot.period_key == "7d")
            .where(MetricSnapshot.source.in_(("combined", "project_metrics_api")))
        )
        latest = session.exec(query.order_by(MetricSnapshot.created_at.desc())).first()
        first = session.exec(query.order_by(MetricSnapshot.created_at.asc())).first()

    if latest is None:
        return {
            "ok": False, "checks": [], "observed_days": None,
            "hint": "Аналитик ещё не собрал ни одного снимка. Готовность выводов "
                    "появится после первого цикла сбора.",
        }

    # Сколько мы вообще наблюдаем: недельное окно у проекта, подключённого
    # вчера, заполнено на день, и мерить его порогом недели -- обман.
    observed_days = (latest.created_at - first.created_at).total_seconds() / 86400.0
    values = extract_normalized_metrics_from_snapshot(latest)

    checks = []
    for check in CHECKS:
        row = assess(check, values.get(check.metric), observed_days)
        row["metric_title"] = stage_titles.get(check.metric, check.metric)
        checks.append(row)

    return {
        "ok": True, "checks": checks,
        "observed_days": round(observed_days, 1),
        "window_days": WINDOW_DAYS,
        "hint": None,
    }


# ---------------------------------------------------------------------------
# Когорты по источнику: доходят ли до оплаты по-разному (D3)
# ---------------------------------------------------------------------------
#
# Суммарная воронка отвечает «сколько людей теряется», но не отвечает
# «кого мы приводим». Источник, который даёт много регистраций и ноль
# оплат, в общей сумме выглядит как польза: он поднимает верх воронки
# и топит конверсию, а виноватым кажется продукт.
#
# Разбивку присылает сам продукт (`source_breakdown` в разборе пути до
# оплаты) -- аналитик не додумывает источник за него: пометить человека
# каналом может только тот, кто видел, откуда человек пришёл.

# Доля оплат на маленьком числе пришедших -- случайность: один оплативший
# из трёх даёт «33%», которые завтра станут нулём. Порог выше, чем в
# сравнении недель (там считаются события, здесь -- доля от людей).
SOURCE_MIN_SAMPLE = ENOUGH_FOR_A_CONCLUSION

# Поле продукта → ключ воронки. Названия шагов берём общие, чтобы экран
# источников звал их так же, как остальные экраны.
_SOURCE_FIELDS = (
    ("registrations", "signup"),
    ("channels_created", "activation_1"),
    ("payment_started", "payment_started"),
    ("payment_success", "payment_success"),
)


# ---------------------------------------------------------------------------
# Отзывы о результате: что людям не нравится (Продакт)
# ---------------------------------------------------------------------------
#
# Причины отказов уже собирались, но были видны только внутри длинных
# текстовых отчётов (/pay, /checks) -- владелец не мог посмотреть их
# отдельно, не читая всё остальное. Эндпоинт просто открывает то, что уже
# считает планировщик, отдельной JSON-выдачей -- новой логики здесь нет.

FEEDBACK_MIN_SAMPLE = MIN_FOR_A_TREND


@router.get("/api/feedback", dependencies=[Depends(require_admin)])
async def feedback(identity=Depends(require_admin)):
    """Отзывы о первом результате: сколько понравилось, сколько нет и почему."""
    from app.service import PAYMENT_PATH_CACHE_PERIOD_KEY, get_cached_diagnostics
    from app.vocabulary import feedback_reason_label

    with get_session() as session:
        project = _active_project(session, identity)
        cached = get_cached_diagnostics(session, project.id, PAYMENT_PATH_CACHE_PERIOD_KEY)

    if cached is None or not cached.ok:
        return {
            "ok": False, "good": None, "bad": None, "reasons": [], "checked_at": None,
            "hint": "Разбор пути до оплаты ещё не собирался. Отзывы появятся "
                    "после первого полного цикла сбора.",
        }

    pp = cached.result_json or {}
    good = int(pp.get("first_post_feedback_good") or 0)
    bad = int(pp.get("first_post_feedback_bad") or 0)
    total = good + bad
    checked_at = cached.created_at.isoformat() if cached.created_at else None

    if total == 0:
        return {
            "ok": True, "good": 0, "bad": 0, "total": 0, "reliable": False,
            "reasons": [], "checked_at": checked_at,
            "hint": "За этот период никто не оставил отзыв о первом результате.",
        }

    reasons_raw = pp.get("first_post_feedback_reasons") or {}
    reasons = sorted(
        (
            {"key": key, "label": feedback_reason_label(key), "count": int(count)}
            for key, count in reasons_raw.items()
            if isinstance(count, (int, float)) and count > 0
        ),
        key=lambda r: -r["count"],
    )

    # Доля плохих отзывов на 1-2 ответах ничего не значит -- тот же порог,
    # что и у сравнения недель, чтобы платформа не говорила об уверенности
    # по-разному в разных местах.
    reliable = total >= FEEDBACK_MIN_SAMPLE
    return {
        "ok": True, "good": good, "bad": bad, "total": total, "reliable": reliable,
        "bad_share_percent": round(bad / total * 100) if reliable else None,
        "reasons": reasons, "checked_at": checked_at,
        "hint": None if reliable else
                f"Отзывов пока {total} — этого мало, чтобы говорить о доле хороших и плохих.",
    }


@router.get("/api/sources", dependencies=[Depends(require_admin)])
async def sources_cohorts(identity=Depends(require_admin)):
    """Кто приходит из каждого канала и доходит ли до оплаты."""
    from app.connectors.traffic_sources import aggregate_by_label, parse_source_breakdown
    from app.service import PAYMENT_PATH_CACHE_PERIOD_KEY, get_cached_diagnostics

    with get_session() as session:
        project = _active_project(session, identity)
        stage_titles = load_stage_titles(session, project.id)
        cached = get_cached_diagnostics(session, project.id, PAYMENT_PATH_CACHE_PERIOD_KEY)

    titles = {key: stage_titles.get(key, key) for _, key in _SOURCE_FIELDS}

    if cached is None or not cached.ok:
        return {
            "ok": False, "sources": [], "titles": titles, "summary": None, "checked_at": None,
            "hint": "Разбор пути до оплаты ещё не собирался. Разбивка по источникам "
                    "появится после первого полного цикла сбора.",
        }

    breakdown = parse_source_breakdown(cached.result_json or {})
    checked_at = cached.created_at.isoformat() if cached.created_at else None
    if not breakdown:
        return {
            "ok": False, "sources": [], "titles": titles, "summary": None,
            "checked_at": checked_at,
            # Честно называем, чего не хватает и на чьей стороне это чинится:
            # источник знает только продукт, аналитик его не угадает.
            "hint": "Продукт не присылает разбивку по источникам, поэтому все люди "
                    "лежат в одной куче. Чтобы она появилась, сохраняйте utm_source "
                    "(или start-параметр бота) при регистрации и отдавайте поле "
                    "source_breakdown в ответе /api/internal/payment-path-diagnostics — "
                    "формат описан в CONTRACT.md.",
        }

    rows = [_source_row(label, data) for label, data in aggregate_by_label(breakdown).items()]
    # Сверху -- те, кто привёл больше людей: с них и начинается разговор.
    rows.sort(key=lambda r: (-(r["signup"] or 0), r["label"]))

    return {
        "ok": True, "sources": rows, "titles": titles,
        "summary": _sources_summary(rows),
        "checked_at": checked_at,
        "hint": None if rows else "За последнюю неделю из источников не пришёл никто.",
    }


def _source_row(label: str, data: dict) -> dict:
    """Одна когорта. Долю оплат считаем только там, где она что-то значит."""
    values = {key: (data.get(field) or 0) for field, key in _SOURCE_FIELDS}
    came = values["signup"]
    paid = values["payment_success"]
    reliable = came >= SOURCE_MIN_SAMPLE

    if not reliable:
        note = (f"Пришло {came} — доля оплат на таких числах случайна."
                if came else "Из этого канала пока никто не пришёл.")
    elif paid == 0:
        note = f"Из {came} до оплаты не дошёл никто."
    else:
        note = f"Оплатили {paid} из {came}."

    return {
        "label": label,
        **values,
        # Процент прячем, а не показываем серым: увиденное число остаётся
        # в голове, даже когда рядом написано «не верьте ему».
        "conversion": round(paid / came * 100, 1) if reliable and came else None,
        "reliable": reliable,
        "note": note,
    }


def _sources_summary(rows: list[dict]) -> str:
    """Вывод словами. Аналитик называет разницу только когда она есть
    на достаточных числах -- и не советует «отключить канал»: решение
    про рекламу принимает человек."""
    reliable = [r for r in rows if r["reliable"]]
    if not rows:
        return "Данных по источникам за эту неделю нет."
    if len(reliable) < 2:
        enough = f"хотя бы {SOURCE_MIN_SAMPLE} человек"
        if not reliable:
            return f"Сравнивать источники ещё рано: ни из одного не пришло {enough}."
        return (f"Сравнивать не с чем: {enough} пришло только из одного канала "
                f"«{reliable[0]['label']}».")

    if all(r["payment_success"] == 0 for r in reliable):
        return "До оплаты не дошёл никто ни из одного канала — сравнивать пока нечего."

    best = max(reliable, key=lambda r: r["conversion"])
    worst = min(reliable, key=lambda r: r["conversion"])
    if best["conversion"] == worst["conversion"]:
        return "Разницы между источниками не видно: доходят до оплаты одинаково."
    return (f"Лучше всех доходит до оплаты «{best['label']}»: {_percent(best['conversion'])} "
            f"из {best['signup']}. Хуже всех «{worst['label']}»: {_percent(worst['conversion'])} "
            f"из {worst['signup']}. Что с этим делать — решать вам: "
            f"аналитик рекламу не трогает.")


def _percent(value: float) -> str:
    """«10%», а не «10.0%»: в таблице рядом стоит то же число, и два разных
    написания одной цифры выглядят как две разные цифры."""
    return f"{value:.1f}".rstrip("0").rstrip(".") + "%"


@router.get("/api/ads/negative-keywords", dependencies=[Depends(require_admin)])
async def negative_keywords(identity=Depends(require_admin)):
    """
    Готовый список минус-фраз по данным последней глубокой проверки Директа.

    Аналитик их НЕ применяет: правки рекламы делает человек своими руками —
    это сознательное ограничение продукта. Задача платформы — собрать
    список, объяснить, почему каждая фраза туда попала, и дать скопировать
    одним движением.

    Новую тяжёлую проверку здесь не запускаем: читаем то, что уже посчитано
    (кнопка «Проверить глубже» стоит рядом). Иначе открытие вкладки
    заказывало бы десятки секунд работы Директа.
    """
    from app.service import DIRECT_INTELLIGENCE_CACHE_PERIOD_KEY, get_cached_diagnostics

    with get_session() as session:
        project = _active_project(session, identity)
        cached = get_cached_diagnostics(
            session, project.id, DIRECT_INTELLIGENCE_CACHE_PERIOD_KEY
        )

    if cached is None or not cached.ok:
        return {
            "ok": False,
            "phrases": [],
            "hint": "Глубокой проверки Директа ещё не было. Нажмите «Проверить глубже» — "
                    "аналитик посмотрит поисковые запросы и соберёт список.",
        }

    data = dict(cached.result_json or {})
    rows = data.get("safe_negatives") or []
    phrases = [
        {
            "query": r.get("query"),
            "clicks": r.get("clicks"),
            "cost": r.get("cost"),
            # Почему фраза в списке -- показываем словами. Без причины это
            # просьба доверять аналитику вслепую, а решение принимает человек.
            "reason": r.get("reason") or "",
            "campaign": r.get("campaign_name"),
        }
        for r in rows if r.get("query")
    ]
    total_cost = round(sum(float(p["cost"] or 0) for p in phrases), 2)
    return {
        "ok": True,
        "phrases": phrases,
        "text": "\n".join(p["query"] for p in phrases),
        "total_cost": total_cost,
        "period_label": data.get("period_label"),
        "checked_at": cached.created_at.isoformat() if cached.created_at else None,
        # Атрибуция регистраций ненадёжна -- об этом надо сказать до того,
        # как человек вырежет фразы: без неё «нет регистраций» может
        # означать «мы их не увидели».
        "attribution_note": data.get("registration_attribution_note") or "",
        "has_registration_attribution": bool(data.get("has_registration_attribution")),
        "hint": None if phrases else
                "Фраз, которые можно безопасно отминусовать, аналитик не нашёл: "
                "либо все запросы приносят результат, либо данных пока мало.",
    }


# ---------------------------------------------------------------------------
# История решений: что предлагали, что приняли, чем кончилось
# ---------------------------------------------------------------------------


def _record_action(session, identity, action: str, summary: str, project_id=None) -> None:
    """Записывает действие владельца в журнал.

    Журнал не должен ронять само действие: если запись не удалась, человек
    всё равно сделал то, что хотел, и получить ошибку вместо результата
    было бы хуже, чем остаться без строчки в истории.
    """
    from app.models import OwnerAction

    try:
        user_id = getattr(identity, "user_id", None)
        actor = "владелец платформы"
        if user_id is not None:
            user = session.get(PlatformUser, user_id)
            actor = user.email if user is not None else f"аккаунт #{user_id}"
        session.add(OwnerAction(
            project_id=project_id, user_id=user_id, actor=actor,
            action=action, summary=summary,
        ))
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("не удалось записать действие владельца: %s", action)


@router.get("/api/actions", dependencies=[Depends(require_admin)])
async def owner_actions(limit: int = 50, identity=Depends(require_admin)):
    """Журнал действий владельца по выбранному проекту."""
    from app.models import OwnerAction

    with get_session() as session:
        project = _find_project(session, identity)
        if project is None:
            return {"actions": []}
        rows = session.exec(
            select(OwnerAction)
            .where(OwnerAction.project_id == project.id)
            .order_by(OwnerAction.created_at.desc())
            .limit(max(1, min(limit, 200)))
        ).all()
        return {"actions": [
            {
                "actor": a.actor,
                "action": a.action,
                "summary": a.summary,
                "created_at": a.created_at.isoformat(),
            }
            for a in rows
        ]}


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
        _record_action(
            session, identity,
            "alert_acknowledged" if action == "ack" else "alert_snoozed",
            ("Отметил сигнал «{}» как понятный" if action == "ack"
             else "Отложил сигнал «{}» на сутки").format(alert.title),
            alert.project_id,
        )
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
    agent = body.agent if body.agent in ("diagnostician", "marketer", "product", "tester") else None
    with get_session() as session:
        project = _active_project(session, identity)
        context_text = ask_module.build_context(session, project, agent)

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
        "autonomy_level": autonomy_level(p),
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
    from app.plans import can_create_project

    with get_session() as session:
        # Лимит тарифа проверяем ДО тяжёлой проверки подключения: если всё
        # равно откажем, незачем дёргать чужой /api/internal/metrics.
        allowed, reason = can_create_project(session, identity.user_id, get_settings())
        if not allowed:
            raise HTTPException(status_code=402, detail=reason)

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
        _record_action(session, identity, "project_connected",
                       f"Подключил проект «{project.name}» ({project.base_url})", project.id)
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
        # Перечисляем, что именно поменяли: «изменил настройки» через неделю
        # не объясняет, почему числа стали другими.
        changed = [name for name, value in (
            ("название", body.name), ("адрес", body.base_url), ("токен", body.internal_api_token),
            ("тип", body.type), ("разметку воронки", body.funnel_mapping),
            ("счётчик Метрики", body.metrika_counter_id), ("логин Директа", body.direct_client_login),
            ("адресатов уведомлений", body.notify_chat_ids),
        ) if value is not None]
        if changed:
            _record_action(session, identity, "project_updated",
                           "Изменил " + ", ".join(changed) + f" у проекта «{project.name}»",
                           project.id)
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
        _record_action(session, identity, "collection_on",
                       f"Включил сбор данных по проекту «{project.name}»", project.id)
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
        _record_action(session, identity, "collection_off",
                       f"Выключил сбор данных по проекту «{project.name}»", project.id)
        return {"ok": True, "is_active": False}


class AutonomyLevelRequest(BaseModel):
    level: int


@router.post("/api/projects/{project_id}/autonomy", dependencies=[Depends(require_admin)])
async def set_autonomy_level(project_id: int, body: AutonomyLevelRequest, identity=Depends(require_admin)):
    """Меняет уровень делегирования агентам для проекта (задача F6)."""
    if body.level not in AUTONOMY_LEVELS:
        raise HTTPException(status_code=422, detail="Уровень должен быть 1, 2 или 3")
    with get_session() as session:
        project = _owned_project(session, project_id, identity)
        sj = dict(project.settings_json or {})
        sj["autonomy_level"] = body.level
        project.settings_json = sj
        session.add(project)
        session.commit()
        _record_action(
            session, identity, "autonomy_level_changed",
            f"Установил уровень автономии {body.level} («{AUTONOMY_LEVELS[body.level]['title']}») "
            f"у проекта «{project.name}»",
            project.id,
        )
        return {"ok": True, "level": body.level}


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
