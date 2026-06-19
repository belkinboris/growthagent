"""
Scheduler / orchestration layer.

run_cycle_once() -- главная функция, которую вызывают:
- APScheduler (по таймеру каждые WATCH_INTERVAL_SECONDS);
- POST /api/run;
- /run в Telegram.

Она не отправляет Telegram и не вызывает LLM -- только собирает данные,
нормализует, сохраняет снэпшоты, запускает analyzer.py и service.py,
возвращает CycleResult. Эта функция и есть "оркестратор", который знает
про ВСЕ коннекторы одновременно -- это единственное место в проекте,
где TruePost/Метрика/Директ/YooKassa встречаются вместе.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import select

from app.config import ANALYSIS_WINDOWS_HOURS, get_settings
from app.connectors import direct as direct_connector
from app.connectors import metrika as metrika_connector
from app.connectors import truepost as truepost_connector
from app.connectors import yookassa as yookassa_connector
from app.db import get_session
from app.models import Integration, IntegrationStatus, IntegrationType, Project
from app.rules import NormalizedMetrics, checkable_categories
from app.analyzer import analyze
from app.service import CycleResult, check_integration_freshness, process_cycle, save_snapshot

logger = logging.getLogger("growth_agent.scheduler")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Сбор данных из одного источника для одного окна, с унифицированной
# обработкой "не настроено" vs "ошибка".
# ---------------------------------------------------------------------------


async def _fetch_product_metrics(project: Project, period_hours: int) -> tuple[dict | None, str | None, datetime | None]:
    """
    Возвращает (normalized_dict, error, as_of). normalized_dict is None,
    если источник не настроен (нет base_url/token) -- это не error, просто
    интеграция отсутствует, и в sources_ok "product" не попадёт.
    """
    settings = get_settings()
    if not project.base_url or not settings.project_internal_api_token:
        return None, None, None  # not configured, не ошибка

    funnel_mapping = project.settings_json.get("funnel_mapping", truepost_connector.DEFAULT_FUNNEL_MAPPING)

    try:
        result = await truepost_connector.fetch_metrics(
            base_url=project.base_url,
            api_token=settings.project_internal_api_token,
            period_hours=period_hours,
            funnel_mapping=funnel_mapping,
        )
        return result, None, result.get("as_of")
    except truepost_connector.TruePostConnectorError as exc:
        return None, str(exc), None


async def _fetch_metrika_metrics(project: Project, period_hours: int) -> tuple[dict | None, str | None]:
    settings = get_settings()
    goal_mapping = project.settings_json.get("metrika_goal_mapping", {})
    # Приоритет: per-project goal_ids в settings_json, иначе -- из .env
    # (METRIKA_GOAL_IDS_JSON). per-project позволяет в будущем настроить
    # разные goal_id для разных проектов без правки .env.
    goal_ids = project.settings_json.get("metrika_goal_ids") or settings.metrika_goal_ids
    try:
        result = await metrika_connector.fetch_metrics(
            oauth_token=settings.yandex_oauth_token,
            counter_id=settings.metrika_counter_id,
            period_hours=period_hours,
            goal_mapping=goal_mapping,
            goal_ids=goal_ids,
        )
        return result, None
    except metrika_connector.NotConfiguredError:
        return None, None  # not configured, не ошибка
    except metrika_connector.MetrikaConnectorError as exc:
        return None, str(exc)


async def _fetch_direct_metrics(period_hours: int) -> tuple[dict | None, str | None]:
    settings = get_settings()
    try:
        result = await direct_connector.fetch_metrics(
            oauth_token=settings.effective_direct_oauth_token,
            client_login=settings.direct_client_login,
            campaign_ids=settings.direct_campaign_ids_list,
            period_hours=period_hours,
            sandbox=settings.direct_sandbox,
        )
        return result, None
    except direct_connector.NotConfiguredError:
        return None, None
    except direct_connector.DirectConnectorError as exc:
        return None, str(exc)


async def _fetch_yookassa_metrics(period_hours: int) -> tuple[dict | None, str | None]:
    settings = get_settings()
    try:
        result = await yookassa_connector.fetch_metrics(
            shop_id=settings.yookassa_shop_id,
            secret_key=settings.yookassa_secret_key,
            period_hours=period_hours,
        )
        return result, None
    except yookassa_connector.NotConfiguredError:
        return None, None
    except yookassa_connector.YooKassaConnectorError as exc:
        return None, str(exc)


# ---------------------------------------------------------------------------
# Сбор и нормализация метрик для одного окна
# ---------------------------------------------------------------------------


async def _collect_window(project: Project, period_key: str, period_hours: int) -> tuple[NormalizedMetrics, dict]:
    """
    Собирает данные всех источников для одного окна, объединяет в
    NormalizedMetrics. Возвращает также errors -- dict {integration_type: error}
    для тех источников, которые ответили ошибкой (не "не настроено").

    sources_ok заполняется только для источников, которые РЕАЛЬНО ответили
    без ошибки. "Не настроено" не попадает ни в sources_ok, ни в errors --
    это нейтральное "источника просто нет".
    """
    errors: dict[str, str] = {}
    sources_ok: set[str] = set()

    period_end = utcnow()
    period_start = period_end - timedelta(hours=period_hours)

    product_data, product_error, product_as_of = await _fetch_product_metrics(project, period_hours)
    if product_error:
        errors["product"] = product_error
    elif product_data is not None:
        sources_ok.add("product")

    metrika_data, metrika_error = await _fetch_metrika_metrics(project, period_hours)
    if metrika_error:
        errors["metrika"] = metrika_error
    elif metrika_data is not None:
        sources_ok.add("metrika")

    direct_data, direct_error = await _fetch_direct_metrics(period_hours)
    if direct_error:
        errors["direct"] = direct_error
    elif direct_data is not None:
        sources_ok.add("direct")

    yookassa_data, yookassa_error = await _fetch_yookassa_metrics(period_hours)
    if yookassa_error:
        errors["yookassa"] = yookassa_error
    elif yookassa_data is not None:
        sources_ok.add("yookassa")

    metrics = NormalizedMetrics(
        period_key=period_key,
        sources_ok=sources_ok,
    )

    if product_data:
        metrics.signup = product_data.get("signup")
        metrics.activation_1 = product_data.get("activation_1")
        metrics.activation_2 = product_data.get("activation_2")
        metrics.payment_started = product_data.get("payment_started")
        metrics.payment_success = product_data.get("payment_success")
        metrics.revenue = product_data.get("revenue")
        metrics.pending_payments = product_data.get("pending_payments")

    if direct_data:
        metrics.spend = direct_data.get("spend")
        metrics.clicks = direct_data.get("clicks")
        metrics.impressions = direct_data.get("impressions")
        metrics.ctr = direct_data.get("ctr")

    if metrika_data:
        metrics.metrika_signup = metrika_data.get("signup")
        if metrics.clicks is None:
            metrics.clicks = metrika_data.get("traffic")  # fallback, если Директ не настроен

    raw_for_snapshot = {
        "product": _jsonify(product_data),
        "metrika": _jsonify(metrika_data),
        "direct": _jsonify(direct_data),
        "yookassa": _jsonify(yookassa_data),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
    }

    return metrics, {"raw": raw_for_snapshot, "errors": errors, "product_as_of": product_as_of}


def _jsonify(data: dict | None) -> dict | None:
    """
    Коннекторы возвращают datetime-объекты (as_of) внутри своих dict.
    Перед сохранением в JSON-колонку MetricSnapshot.metrics_json их нужно
    превратить в строки -- иначе sqlite/JSON-сериализатор падает. Не трогаем
    "_raw" вложенный dict глубже одного уровня, потому что коннекторы кладут
    туда исходный ответ источника как уже JSON-совместимый dict (он же
    пришёл как response.json()).
    """
    if data is None:
        return None
    result = {}
    for key, value in data.items():
        if isinstance(value, datetime):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Главная точка входа
# ---------------------------------------------------------------------------


async def run_cycle_once(project_id: int | None = None) -> CycleResult:
    """
    Запускает один полный цикл наблюдения для проекта. Если project_id не
    передан -- берёт единственный активный проект (практика v1: один проект).

    Используется из APScheduler, POST /api/run, и /run в Telegram -- все
    три точки вызова получают идентичный результат.
    """
    with get_session() as session:
        if project_id is not None:
            project = session.get(Project, project_id)
        else:
            project = session.exec(select(Project).where(Project.is_active == True)).first()

        if project is None:
            raise ValueError("No active project found")

        metrics_by_window: dict[str, NormalizedMetrics] = {}
        all_sources_ok: set[str] = set()
        all_errors: dict[str, str] = {}
        product_as_of: datetime | None = None

        for period_key, period_hours in ANALYSIS_WINDOWS_HOURS.items():
            metrics, extra = await _collect_window(project, period_key, period_hours)
            metrics_by_window[period_key] = metrics
            all_sources_ok |= metrics.sources_ok
            all_errors.update(extra["errors"])
            if extra.get("product_as_of"):
                product_as_of = extra["product_as_of"]

            save_snapshot(
                session=session,
                project_id=project.id,
                period_key=period_key,
                period_start=datetime.fromisoformat(extra["raw"]["period_start"]),
                period_end=datetime.fromisoformat(extra["raw"]["period_end"]),
                source="combined",
                metrics=extra["raw"],
                as_of=extra.get("product_as_of"),
            )

        # integration_down: проверяем свежесть/доступность для каждого источника
        # ОТДЕЛЬНО от бизнес-анализа. Эта проверка использует данные собранные
        # выше за 24h окно как репрезентативные для статуса интеграции.
        integration_changes = []
        integration_changes.append(
            check_integration_freshness(
                session, project, IntegrationType.project_metrics_api,
                as_of=product_as_of, error=all_errors.get("product"),
            )
        )
        integration_changes.append(
            check_integration_freshness(
                session, project, IntegrationType.metrika,
                as_of=None, error=all_errors.get("metrika"),
            )
        )
        integration_changes.append(
            check_integration_freshness(
                session, project, IntegrationType.direct,
                as_of=None, error=all_errors.get("direct"),
            )
        )
        integration_changes.append(
            check_integration_freshness(
                session, project, IntegrationType.yookassa,
                as_of=None, error=all_errors.get("yookassa"),
            )
        )
        integration_changes = [c for c in integration_changes if c is not None]

        # Бизнес-анализ
        thresholds = project.settings_json.get("thresholds")
        candidates = analyze(project_id=project.id, metrics_by_window=metrics_by_window, thresholds=thresholds)
        checked_categories = checkable_categories(all_sources_ok, thresholds)

        result = process_cycle(
            session=session,
            project_id=project.id,
            candidates=candidates,
            checked_categories=checked_categories,
            metrics_by_window=metrics_by_window,
        )
        result.integration_down_changes = integration_changes

        return result


def run_cycle_once_sync(project_id: int | None = None) -> CycleResult:
    """Синхронная обёртка для вызова из не-async контекста (например APScheduler job)."""
    return asyncio.run(run_cycle_once(project_id))


# ---------------------------------------------------------------------------
# APScheduler setup
# ---------------------------------------------------------------------------

_scheduler: AsyncIOScheduler | None = None


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    settings = get_settings()

    if _scheduler is not None:
        return _scheduler

    _scheduler = AsyncIOScheduler()

    async def _job():
        try:
            result = await run_cycle_once()
            logger.info(
                "Cycle complete: notifiable=%s primary=%s",
                result.has_notifiable_changes,
                result.primary_candidate.title if result.primary_candidate else None,
            )
        except Exception:
            logger.exception("Scheduled cycle failed")

    _scheduler.add_job(_job, "interval", seconds=settings.watch_interval_seconds, id="watch_cycle")
    _scheduler.start()
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown()
        _scheduler = None
