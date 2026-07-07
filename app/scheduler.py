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
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import select

from app.config import (
    ANALYSIS_WINDOWS_HOURS,
    CONNECTOR_CALL_TIMEOUT_SECONDS,
    DEEP_DIAGNOSTICS_TIMEOUT_SECONDS,
    DIRECT_SUMMARY_MAX_RETRIES,
    DIRECT_SUMMARY_TIMEOUT_SECONDS,
    DIRECT_DEEP_REPORT_MAX_RETRIES,
    DIRECT_DEEP_REPORT_TIMEOUT_SECONDS,
    RUN_CYCLE_TIMEOUT_SECONDS,
    get_settings,
    MANUAL_DEEP_DIRECT_TIMEOUT_SECONDS,
)
from app.connectors import direct as direct_connector
from app.connectors import landing as landing_connector
from app.connectors import metrika as metrika_connector
from app.connectors import onboarding as onboarding_connector
from app.connectors import payment_path as payment_path_connector
from app.connectors import truepost as truepost_connector
from app.connectors import yookassa as yookassa_connector
from app.db import get_session
from app.diagnostics import run_diagnostics, get_query_clusters, analyze_onboarding, analyze_landing_funnel, finding_fingerprint
from app.query_classifier import classify_search_queries, DirectIntelligenceResult
from app.models import AttributionStatus, Integration, IntegrationStatus, IntegrationType, MetricSnapshot, NotificationLog, Project
from app.rules import NormalizedMetrics, checkable_categories
from app.analyzer import analyze
from app.service import (
    CycleResult,
    DIRECT_INTELLIGENCE_CACHE_PERIOD_KEY,
    LANDING_FUNNEL_CACHE_PERIOD_KEY,
    ONBOARDING_CACHE_PERIOD_KEY,
    PAYMENT_PATH_CACHE_PERIOD_KEY,
    USER_JOURNEYS_CACHE_PERIOD_KEY,
    check_integration_freshness,
    collect_milestone_notifications,
    extract_normalized_metrics_from_snapshot,
    get_cached_diagnostics,
    get_latest_cutoff,
    get_previous_snapshot,
    process_cycle,
    record_finding_shown,
    save_diagnostics_cache,
    save_snapshot,
    should_run_deep_diagnostics,
    should_run_landing_funnel_diagnostics,
    should_run_onboarding_diagnostics,
    should_show_deep_direct_button,
    should_show_landing_funnel_button,
    should_suppress_as_primary,
    should_show_onboarding_button,
)

logger = logging.getLogger("growth_agent.scheduler")
_RUN_CONTEXT: ContextVar[str] = ContextVar("growth_agent_run_context", default="scheduled_or_api")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _log_manual_source_failure(source_name: str, error: object) -> None:
    """Extra lifecycle logging for manual /run, including connector-level HTTP timeouts."""
    if _RUN_CONTEXT.get() != "manual_telegram_run":
        return
    message = str(error)
    if "timeout" in message.lower() or "не ответ" in message.lower():
        logger.warning("Manual /run source timeout: %s: %s", source_name, message)
    else:
        logger.warning("Manual /run source failed: %s: %s", source_name, message)


def _get_cached_source_from_latest_snapshot(
    project_id: int,
    period_key: str,
    source_name: str,
) -> tuple[dict | None, datetime | None]:
    """
    Returns the latest cached sub-payload for one source from combined snapshots.

    This is used only as a fallback when a live source fails or times out. It
    lets /run produce a business report from the best available data instead
    of returning only a timeout message.
    """
    with get_session() as session:
        snapshots = session.exec(
            select(MetricSnapshot)
            .where(
                MetricSnapshot.project_id == project_id,
                MetricSnapshot.period_key == period_key,
                MetricSnapshot.source == "combined",
            )
            .order_by(MetricSnapshot.created_at.desc())
            .limit(20)
        ).all()

        for snapshot in snapshots:
            data = (snapshot.metrics_json or {}).get(source_name)
            if data is not None:
                return data, snapshot.created_at

    return None, None


def _source_status(status: str, snapshot_created_at: datetime | None = None, error: str | None = None) -> dict:
    payload = {"status": status}
    if snapshot_created_at is not None:
        payload["snapshot_created_at"] = snapshot_created_at.isoformat()
    if error:
        payload["error"] = error
    return payload


async def _run_with_timeout(awaitable, timeout_seconds: float, source_name: str):
    """
    Runtime fuse for external API calls. Connector-level httpx timeouts are
    useful, but Direct Reports API can also spend time in retry/sleep cycles.
    This wrapper prevents one slow source from blocking the whole /run command.
    """
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        context = _RUN_CONTEXT.get()
        if context == "manual_telegram_run":
            logger.warning(
                "Manual /run source timeout: %s did not respond within %.1f sec -- continuing with this source unavailable",
                source_name, timeout_seconds,
            )
        else:
            logger.warning(
                "%s did not respond within %.1f sec -- continuing cycle with this source unavailable",
                source_name, timeout_seconds,
            )
        raise


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
        result = await _run_with_timeout(
            truepost_connector.fetch_metrics(
                base_url=project.base_url,
                api_token=settings.project_internal_api_token,
                period_hours=period_hours,
                funnel_mapping=funnel_mapping,
            ),
            CONNECTOR_CALL_TIMEOUT_SECONDS,
            "TruePost metrics",
        )
        return result, None, result.get("as_of")
    except asyncio.TimeoutError:
        return None, f"TruePost не ответил за {CONNECTOR_CALL_TIMEOUT_SECONDS:.0f} секунд", None
    except truepost_connector.TruePostConnectorError as exc:
        _log_manual_source_failure("TruePost metrics", exc)
        return None, str(exc), None


async def _fetch_metrika_metrics(project: Project, period_hours: int) -> tuple[dict | None, str | None]:
    settings = get_settings()
    goal_mapping = project.settings_json.get("metrika_goal_mapping", {})
    # Приоритет: per-project goal_ids в settings_json, иначе -- из .env
    # (METRIKA_GOAL_IDS_JSON). per-project позволяет в будущем настроить
    # разные goal_id для разных проектов без правки .env.
    goal_ids = project.settings_json.get("metrika_goal_ids") or settings.metrika_goal_ids
    try:
        result = await _run_with_timeout(
            metrika_connector.fetch_metrics(
                oauth_token=settings.yandex_oauth_token,
                counter_id=settings.metrika_counter_id,
                period_hours=period_hours,
                goal_mapping=goal_mapping,
                goal_ids=goal_ids,
            ),
            CONNECTOR_CALL_TIMEOUT_SECONDS,
            "Metrika metrics",
        )
        return result, None
    except metrika_connector.NotConfiguredError:
        return None, None  # not configured, не ошибка
    except asyncio.TimeoutError:
        return None, f"Яндекс.Метрика не ответила за {CONNECTOR_CALL_TIMEOUT_SECONDS:.0f} секунд"
    except metrika_connector.MetrikaConnectorError as exc:
        _log_manual_source_failure("Metrika metrics", exc)
        return None, str(exc)


async def _fetch_direct_metrics(period_hours: int) -> tuple[dict | None, str | None]:
    settings = get_settings()
    try:
        result = await _run_with_timeout(
            direct_connector.fetch_metrics(
                oauth_token=settings.effective_direct_oauth_token,
                client_login=settings.direct_client_login,
                campaign_ids=settings.direct_campaign_ids_list,
                period_hours=period_hours,
                sandbox=settings.direct_sandbox,
                timeout_seconds=DIRECT_SUMMARY_TIMEOUT_SECONDS,
                max_retries=DIRECT_SUMMARY_MAX_RETRIES,
            ),
            CONNECTOR_CALL_TIMEOUT_SECONDS,
            "Direct summary",
        )
        return result, None
    except direct_connector.NotConfiguredError:
        return None, None
    except asyncio.TimeoutError:
        return None, f"Яндекс.Директ не ответил за {CONNECTOR_CALL_TIMEOUT_SECONDS:.0f} секунд"
    except direct_connector.DirectConnectorError as exc:
        _log_manual_source_failure("Direct summary", exc)
        return None, str(exc)


async def _fetch_yookassa_metrics(period_hours: int) -> tuple[dict | None, str | None]:
    settings = get_settings()
    try:
        result = await _run_with_timeout(
            yookassa_connector.fetch_metrics(
                shop_id=settings.yookassa_shop_id,
                secret_key=settings.yookassa_secret_key,
                period_hours=period_hours,
            ),
            CONNECTOR_CALL_TIMEOUT_SECONDS,
            "YooKassa metrics",
        )
        return result, None
    except yookassa_connector.NotConfiguredError:
        return None, None
    except asyncio.TimeoutError:
        return None, f"ЮKassa не ответила за {CONNECTOR_CALL_TIMEOUT_SECONDS:.0f} секунд"
    except yookassa_connector.YooKassaConnectorError as exc:
        _log_manual_source_failure("YooKassa metrics", exc)
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

    # Источники независимы, поэтому собираем их параллельно. До этого они
    # шли последовательно, и один медленный Direct/Metrika вызов мог оставлять
    # Telegram-команду /run без финального ответа на несколько минут.
    product_result, metrika_result, direct_result, yookassa_result = await asyncio.gather(
        _fetch_product_metrics(project, period_hours),
        _fetch_metrika_metrics(project, period_hours),
        _fetch_direct_metrics(period_hours),
        _fetch_yookassa_metrics(period_hours),
    )

    source_statuses: dict[str, dict] = {}

    product_data, product_error, product_as_of = product_result
    if product_error:
        errors["product"] = product_error
        cached, cached_at = _get_cached_source_from_latest_snapshot(project.id, period_key, "product")
        if cached is not None:
            product_data = cached
            sources_ok.add("product")
            source_statuses["product"] = _source_status("stale", cached_at, product_error)
            logger.info(
                "cached fallback used: product metrics from %s after live error: %s",
                cached_at.isoformat() if cached_at else "unknown time",
                product_error,
            )
        else:
            source_statuses["product"] = _source_status("unavailable", error=product_error)
    elif product_data is not None:
        sources_ok.add("product")
        source_statuses["product"] = _source_status("fresh", error=None)
        logger.info("source fresh: product metrics (%s)", period_key)
    else:
        source_statuses["product"] = _source_status("unavailable")

    metrika_data, metrika_error = metrika_result
    if metrika_error:
        errors["metrika"] = metrika_error
        cached, cached_at = _get_cached_source_from_latest_snapshot(project.id, period_key, "metrika")
        if cached is not None:
            metrika_data = cached
            sources_ok.add("metrika")
            source_statuses["metrika"] = _source_status("stale", cached_at, metrika_error)
            logger.info(
                "cached fallback used: Metrika metrics from %s after live error: %s",
                cached_at.isoformat() if cached_at else "unknown time",
                metrika_error,
            )
        else:
            source_statuses["metrika"] = _source_status("unavailable", error=metrika_error)
    elif metrika_data is not None:
        sources_ok.add("metrika")
        source_statuses["metrika"] = _source_status("fresh")
        logger.info("source fresh: Metrika metrics (%s)", period_key)
    else:
        source_statuses["metrika"] = _source_status("unavailable")

    direct_data, direct_error = direct_result
    if direct_error:
        errors["direct"] = direct_error
        cached, cached_at = _get_cached_source_from_latest_snapshot(project.id, period_key, "direct")
        if cached is not None:
            direct_data = cached
            sources_ok.add("direct")
            source_statuses["direct"] = _source_status("stale", cached_at, direct_error)
            logger.info(
                "cached fallback used: Direct summary from %s after live error: %s",
                cached_at.isoformat() if cached_at else "unknown time",
                direct_error,
            )
        else:
            source_statuses["direct"] = _source_status("unavailable", error=direct_error)
    elif direct_data is not None:
        sources_ok.add("direct")
        source_statuses["direct"] = _source_status("fresh")
        logger.info("source fresh: Direct summary (%s)", period_key)
    else:
        source_statuses["direct"] = _source_status("unavailable")

    yookassa_data, yookassa_error = yookassa_result
    if yookassa_error:
        errors["yookassa"] = yookassa_error
        cached, cached_at = _get_cached_source_from_latest_snapshot(project.id, period_key, "yookassa")
        if cached is not None:
            yookassa_data = cached
            sources_ok.add("yookassa")
            source_statuses["yookassa"] = _source_status("stale", cached_at, yookassa_error)
            logger.info(
                "cached fallback used: YooKassa metrics from %s after live error: %s",
                cached_at.isoformat() if cached_at else "unknown time",
                yookassa_error,
            )
        else:
            source_statuses["yookassa"] = _source_status("unavailable", error=yookassa_error)
    elif yookassa_data is not None:
        sources_ok.add("yookassa")
        source_statuses["yookassa"] = _source_status("fresh")
        logger.info("source fresh: YooKassa metrics (%s)", period_key)
    else:
        source_statuses["yookassa"] = _source_status("unavailable")

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
        "source_statuses": source_statuses,
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

        manual_context = _RUN_CONTEXT.get() == "manual_telegram_run"
        if manual_context:
            logger.info("/run live refresh started (project_id=%s, build context=manual_telegram_run)", project.id)

        metrics_by_window: dict[str, NormalizedMetrics] = {}
        source_statuses_by_window: dict[str, dict] = {}
        all_sources_ok: set[str] = set()
        all_errors: dict[str, str] = {}
        product_as_of: datetime | None = None

        for period_key, period_hours in ANALYSIS_WINDOWS_HOURS.items():
            metrics, extra = await _collect_window(project, period_key, period_hours)
            metrics_by_window[period_key] = metrics
            source_statuses_by_window[period_key] = (extra.get("raw") or {}).get("source_statuses") or {}
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
        result.source_statuses_by_window = source_statuses_by_window

        # Deep diagnostics: гибридный режим. Автоматически запускается
        # только если (а) есть триггерящий алерт, (б) Direct настроен,
        # (в) нет свежего кэша на это окно. Использует 7d окно -- оно даёт
        # больше данных для granular-анализа, чем 3h/24h, и обычно содержит
        # достаточный объём кликов для MIN_CLICKS_FOR_DEEP_DIAGNOSTICS.
        settings = get_settings()
        direct_configured = bool(settings.effective_direct_oauth_token and settings.direct_client_login)
        product_configured = bool(project.base_url and settings.project_internal_api_token)
        metrics_7d = metrics_by_window.get("7d")

        # Флаги показа кнопок считаются ВСЕГДА, независимо от того, что
        # сейчас primary_candidate -- по решению: кнопки диагностики не
        # должны зависеть только от primary alert, пользователь может
        # вручную проверить рекламу/онбординг при наличии данных.
        result.show_deep_direct_button = should_show_deep_direct_button(direct_configured, metrics_7d)
        result.show_onboarding_button = should_show_onboarding_button(product_configured, metrics_7d)
        result.show_landing_funnel_button = should_show_landing_funnel_button(product_configured, metrics_7d)

        previous_metrics_7d = None
        previous_snapshot_7d = get_previous_snapshot(session, project.id, "7d")
        if previous_snapshot_7d is not None:
            previous_metrics_7d = extract_normalized_metrics_from_snapshot(previous_snapshot_7d)
        result.previous_metrics_by_window["7d"] = previous_metrics_7d

        # Regular manual /run should not run heavy deep Direct live, but it may
        # attach an already cached diagnostics result so the owner can still see
        # what to scale, observe or clean in search queries.
        if manual_context and direct_configured:
            cached_direct = get_cached_diagnostics(session, project.id, "7d")
            if cached_direct is not None:
                result.deep_diagnostics = dict(cached_direct.result_json or {})
                result.deep_diagnostics["_from_cache"] = True

        result.milestone_notifications = collect_milestone_notifications(
            session=session,
            project_id=project.id,
            metrics_7d=metrics_7d,
            previous_metrics=previous_metrics_7d,
        )

        if direct_configured and should_run_deep_diagnostics(result.primary_candidate):
            cached = get_cached_diagnostics(session, project.id, "7d")
            if cached is not None:
                result.deep_diagnostics = cached.result_json
                result.deep_diagnostics["_from_cache"] = True
            elif not manual_context:
                diag_result, diag_error = await run_deep_diagnostics_for_project(
                    project, settings, period_hours=ANALYSIS_WINDOWS_HOURS["7d"],
                )
                if diag_result is not None:
                    result.deep_diagnostics = diag_result.to_dict()
                    result.deep_diagnostics["_from_cache"] = False
                    save_diagnostics_cache(session, project.id, "7d", "alert_triggered", result.deep_diagnostics)
                else:
                    # Запуск не удался (ошибка Direct API на granular-запросе) --
                    # не падаем, не показываем deep diagnostics в этом цикле,
                    # сохраняем ok=False, чтобы не застрять на этой ошибке как
                    # на валидном кэше (см. service.save_diagnostics_cache).
                    save_diagnostics_cache(
                        session, project.id, "7d", "alert_triggered", {}, ok=False, error=diag_error,
                    )
                    logger.warning("Deep diagnostics failed: %s", diag_error)
            else:
                logger.info("Manual /run skipped live deep Direct diagnostics; cached result not available")

        # Onboarding diagnostics: симметрично Direct, но триггер -- продуктовые
        # категории (signups_no_activation), не рекламные.
        if product_configured and should_run_onboarding_diagnostics(result.primary_candidate):
            cached_onboarding = get_cached_diagnostics(session, project.id, ONBOARDING_CACHE_PERIOD_KEY)
            if cached_onboarding is not None:
                result.onboarding_diagnostics = cached_onboarding.result_json
                result.onboarding_diagnostics["_from_cache"] = True
            elif manual_context:
                logger.info("Manual /run skipped live onboarding diagnostics; cached result not available")
            else:
                onboarding_outcome = await run_onboarding_diagnostics_for_project(project, settings)
                if onboarding_outcome["status"] == "ok":
                    onboarding_outcome["result"]["_from_cache"] = False
                    result.onboarding_diagnostics = onboarding_outcome["result"]
                    save_diagnostics_cache(
                        session, project.id, ONBOARDING_CACHE_PERIOD_KEY,
                        "alert_triggered", result.onboarding_diagnostics,
                    )
                elif onboarding_outcome["status"] == "not_available":
                    # Не кэшируем -- см. force_refresh_onboarding_diagnostics
                    # docstring. Передаём статус в CycleResult, чтобы
                    # telegram_bot.py мог честно сообщить "пока недоступна",
                    # не молчать об этом.
                    result.onboarding_diagnostics = {"status": "not_available"}
                else:
                    save_diagnostics_cache(
                        session, project.id, ONBOARDING_CACHE_PERIOD_KEY, "alert_triggered",
                        {}, ok=False, error=onboarding_outcome["error"],
                    )
                    logger.warning("Onboarding diagnostics failed: %s", onboarding_outcome["error"])

        # Landing funnel diagnostics: триггерится обеими сторонами воронки
        # (traffic_no_signups И signups_no_activation), потому что сама
        # диагностика покрывает всю цепочку Direct clicks -> ... -> activation.
        #
        # ВАЖНО: используем 7d-окно (168ч) для ОБОИХ источников -- Direct
        # clicks (metrics_7d.clicks) и landing funnel (period_hours=168 ниже).
        # Раньше здесь был баг: landing запрашивался с дефолтным period_hours=24,
        # а клики брались за 7d -- это давало false positive "переход с
        # рекламы сломан" при сравнении кликов за неделю с просмотрами за
        # сутки. period_hours передаётся явно в обе стороны, чтобы это
        # несоответствие не могло повториться незаметно.
        if product_configured and should_run_landing_funnel_diagnostics(result.primary_candidate):
            cached_landing = get_cached_diagnostics(session, project.id, LANDING_FUNNEL_CACHE_PERIOD_KEY)
            if cached_landing is not None:
                result.landing_funnel_diagnostics = cached_landing.result_json
                result.landing_funnel_diagnostics["_from_cache"] = True
            elif manual_context:
                logger.info("Manual /run skipped live landing diagnostics; cached result not available")
            else:
                landing_period_hours = ANALYSIS_WINDOWS_HOURS["7d"]
                landing_outcome = await run_landing_funnel_diagnostics_for_project(
                    project, settings, metrics_7d.clicks if metrics_7d else None,
                    period_hours=landing_period_hours,
                )
                if landing_outcome["status"] == "ok":
                    landing_outcome["result"]["_from_cache"] = False
                    result.landing_funnel_diagnostics = landing_outcome["result"]
                    save_diagnostics_cache(
                        session, project.id, LANDING_FUNNEL_CACHE_PERIOD_KEY,
                        "alert_triggered", result.landing_funnel_diagnostics,
                    )
                elif landing_outcome["status"] == "not_configured":
                    result.landing_funnel_diagnostics = {"status": "not_configured"}
                else:
                    save_diagnostics_cache(
                        session, project.id, LANDING_FUNNEL_CACHE_PERIOD_KEY, "alert_triggered",
                        {}, ok=False, error=landing_outcome["error"],
                    )
                    logger.warning("Landing funnel diagnostics failed: %s", landing_outcome["error"])

        # Payment-path diagnostics: запускается всегда при manual /run и при
        # плановом цикле если product configured. Использует 7d-окно (168ч) --
        # наилучший охват для анализа попыток оплаты. Не кэшируется, так как
        # не является тяжёлым запросом (internal endpoint TruePost). Не
        # блокирует /run: ошибка endpoint'а переходит в статус, не в падение.
        #
        # В отличие от onboarding/landing -- не требует триггерного алерта:
        # путь до оплаты всегда полезен для понимания воронки, вне зависимости
        # от того, что сейчас primary_candidate.
        if product_configured:
            payment_path_period_hours = ANALYSIS_WINDOWS_HOURS["7d"]
            try:
                payment_path_outcome = await run_payment_path_diagnostics_for_project(
                    project, settings, period_hours=payment_path_period_hours,
                )
            except Exception as exc:
                # Последний рубеж: run_payment_path_diagnostics_for_project не должна
                # кидать исключения, но если что-то неожиданное (баг в коде, OOM и т.п.)
                # -- /run продолжает работу, блок просто не появится в отчёте.
                logger.exception("Unexpected error in payment_path diagnostics: %s", exc)
                payment_path_outcome = {"status": "error", "result": None, "error": str(exc)}

            if payment_path_outcome["status"] == "ok":
                result.payment_path_diagnostics = payment_path_outcome["result"]
                result.payment_path_diagnostics["_from_cache"] = False

                # Live notifications: сравниваем с ПРЕДЫДУЩИМ сохранённым
                # снимком payment_path ДО того как перезапишем кэш новым.
                # Если сравнивать нечего (первый прогон) -- notify_outcome
                # будет пустым, ничего не отправится.
                try:
                    from app.scheduler import notify_product_signal_deltas
                    await notify_product_signal_deltas(project, settings, result.payment_path_diagnostics)
                except Exception as notif_exc:
                    logger.warning("Live notifications failed (non-fatal): %s", notif_exc)

                # Growth Loop: обновить прогресс эксперимента / вынести вердикт /
                # предложить новую рекомендацию. Детерминированно, без LLM.
                try:
                    from app.scheduler import growth_loop_tick_and_notify
                    await growth_loop_tick_and_notify(project, settings, result.payment_path_diagnostics)
                except Exception as gl_exc:
                    logger.warning("Growth loop tick failed (non-fatal): %s", gl_exc)

                # Сохраняем в кэш — чтобы fallback /run тоже мог показать payment-path блок.
                # Очищаем datetime объекты — JSON column не принимает нативные datetime.
                try:
                    import json as _json
                    from datetime import datetime as _dt

                    def _make_json_safe(obj):
                        """Рекурсивно конвертируем datetime → isoformat строку."""
                        if isinstance(obj, dict):
                            return {k: _make_json_safe(v) for k, v in obj.items()}
                        if isinstance(obj, list):
                            return [_make_json_safe(v) for v in obj]
                        if isinstance(obj, _dt):
                            return obj.isoformat()
                        return obj

                    safe_payload = _make_json_safe(result.payment_path_diagnostics)
                    save_diagnostics_cache(
                        session, project.id, PAYMENT_PATH_CACHE_PERIOD_KEY,
                        "live_run", safe_payload, ok=True,
                    )
                    logger.info(
                        "Payment-path diagnostics cached (project_id=%s, key=%s)",
                        project.id, PAYMENT_PATH_CACHE_PERIOD_KEY,
                    )
                except Exception as cache_exc:
                    logger.warning("Failed to cache payment-path diagnostics: %s", cache_exc)
            elif payment_path_outcome["status"] == "not_configured":
                result.payment_path_diagnostics = {"status": "not_configured"}
            else:
                # Ошибка endpoint'а -- не падаем, передаём статус в отчёт
                result.payment_path_diagnostics = {
                    "status": "error",
                    "error": payment_path_outcome.get("error"),
                }
                logger.warning("Payment-path diagnostics failed: %s", payment_path_outcome.get("error"))

        # Direct Intelligence: /run ТОЛЬКО читает последний сохранённый кэш.
        # Тяжёлый Direct granular report НЕ запускается в /run --
        # только через /deep_direct или background refresh.
        # Читаем ORM-объект DeepDiagnosticsCache: поля .ok и .result_json,
        # а НЕ .get("ok")/.get("result") -- это не dict.
        direct_intel_cached = get_cached_diagnostics(
            session, project.id, DIRECT_INTELLIGENCE_CACHE_PERIOD_KEY
        )
        if direct_intel_cached is not None and direct_intel_cached.ok:
            result.direct_intelligence = dict(direct_intel_cached.result_json or {})
            logger.info(
                "Direct Intelligence cache read ok (project_id=%s, rows=%s, cache_key=%s)",
                project.id,
                (direct_intel_cached.result_json or {}).get("total_queries_analyzed", "?"),
                DIRECT_INTELLIGENCE_CACHE_PERIOD_KEY,
            )
        else:
            result.direct_intelligence = None
            logger.info(
                "Direct Intelligence cache miss (project_id=%s, cache_key=%s, found=%s)",
                project.id,
                DIRECT_INTELLIGENCE_CACHE_PERIOD_KEY,
                direct_intel_cached is not None,
            )

        return result


async def run_direct_intelligence_for_project(
    project: Project,
    settings,
    period_hours: int,
    payment_path_data: dict | None = None,
) -> dict:
    """
    Запускает goal-aware Direct search query report и classify_search_queries().
    Сохраняет результат в кэш DIRECT_INTELLIGENCE_CACHE_PERIOD_KEY.

    Вызывается из:
      - /deep_direct (принудительно)
      - фонового планировщика (ежедневно, не блокирует /run)

    /run НЕ вызывает эту функцию -- только читает кэш.

    Возвращает {"status": "ok"|"not_configured"|"error",
               "result": dict|None, "error": str|None}.

    payment_path_data -- опциональный dict для Spend Gate:
      registrations, channels_created, payment_started, payment_success,
      pricing_viewed. Берётся из последнего payment_path snapshot.
    """
    oauth_token = settings.effective_direct_oauth_token
    client_login = settings.direct_client_login
    campaign_ids = settings.direct_campaign_ids_list
    registration_goal_id = settings.direct_registration_goal_id

    if not oauth_token or not client_login:
        return {"status": "not_configured", "result": None, "error": None}

    # Определяем clean-period окно (если задан cutoff для Direct)
    date_from_override = None
    date_to_override = None
    with get_session() as session:
        cutoff = get_latest_cutoff(session, project.id, "direct")
    if cutoff is not None:
        from datetime import timezone as _tz
        cutoff_dt = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=_tz.utc)
        date_from_override = cutoff_dt.date().isoformat()
        from datetime import datetime as _dt
        date_to_override = _dt.now(_tz.utc).date().isoformat()
        logger.info(
            "Direct Intelligence using clean-period: %s -> %s",
            date_from_override, date_to_override,
        )

    # Direct Reports API не поддерживает per-goal разбивку конверсий в
    # SEARCH_QUERY_PERFORMANCE_REPORT. Поэтому registration_attribution="none"
    # всегда -- winner/safe-negative строятся только на семантике запросов,
    # не на данных конверсий из Директа.
    # registration_goal_id используется только как метаданные для будущей
    # интеграции с другими источниками (backend truth).
    try:
        raw_report = await _run_with_timeout(
            direct_connector.fetch_search_query_report(
                oauth_token=oauth_token,
                client_login=client_login,
                campaign_ids=campaign_ids,
                period_hours=period_hours,
                sandbox=settings.direct_sandbox,
                date_from_override=date_from_override,
                date_to_override=date_to_override,
            ),
            CONNECTOR_CALL_TIMEOUT_SECONDS,
            "Direct Intelligence (search query)",
        )
    except asyncio.TimeoutError:
        return {
            "status": "error",
            "result": None,
            "error": f"Direct search query report timeout ({CONNECTOR_CALL_TIMEOUT_SECONDS:.0f}s)",
        }
    except (direct_connector.DirectConnectorError, direct_connector.NotConfiguredError) as exc:
        return {"status": "error", "result": None, "error": str(exc)}

    rows = raw_report.get("rows", [])
    period_label = f"{date_from_override} — {date_to_override}" if date_from_override else f"{period_hours // 24}д"

    # Spend Gate данные: берём из payment_path_data если переданы
    spend_gate_data = None
    if payment_path_data and payment_path_data.get("registrations") is not None:
        from app.rules import NormalizedMetrics as _NM  # noqa: avoid circular at module level

        # Расход из Direct summary (не из query-level, чтобы не суммировать ещё раз)
        spend_gate_data = {
            "spend_rub": raw_report.get("_diagnostics", {}).get("total_cost", 0.0),
            "registrations": payment_path_data.get("registrations", 0),
            "channels_created": payment_path_data.get("channels_created", 0),
            "payment_started": payment_path_data.get("payment_started", 0),
            "payment_success": payment_path_data.get("payment_success", 0),
            "pricing_viewed": payment_path_data.get("pricing_viewed"),
        }

    di_result = classify_search_queries(
        query_rows=rows,
        period_label=period_label,
        registration_goal_id=None,   # Direct API не даёт per-goal attribution в SEARCH_QUERY
        spend_gate_data=spend_gate_data,
    )

    result_dict = di_result.to_dict()

    # Сохраняем в кэш
    with get_session() as session:
        save_diagnostics_cache(
            session, project.id, DIRECT_INTELLIGENCE_CACHE_PERIOD_KEY,
            "deep_direct", result_dict, ok=True, error=None,
        )

    return {"status": "ok", "result": result_dict, "error": None}


async def run_deep_diagnostics_for_project(project: Project, settings, period_hours: int):
    """
    Запускает granular Direct отчёты (ad_group + search_query) и
    diagnostics.run_diagnostics(). Возвращает (DiagnosticsResult, None) при
    успехе или (None, error_message) при сбое.

    Важно для runtime: отчёты Директа по группам и поисковым запросам могут
    готовиться медленно. Поэтому два granular-запроса запускаются параллельно
    и обрабатываются по принципу best-effort: если один отчёт пришёл, а второй
    не успел, показываем частичную диагностику, а не пустой timeout.
    """
    campaign_ids = settings.direct_campaign_ids_list

    if not campaign_ids:
        logger.warning(
            "DIRECT_CAMPAIGN_IDS not set -- deep diagnostics will analyze ALL campaigns "
            "in the Direct account, not just this project. Set DIRECT_CAMPAIGN_IDS to scope "
            "the analysis if the account has campaigns for other projects."
        )

    async def _fetch_ad_groups() -> tuple[str, dict | None, str | None]:
        try:
            report = await _run_with_timeout(
                direct_connector.fetch_ad_group_report(
                    oauth_token=settings.effective_direct_oauth_token,
                    client_login=settings.direct_client_login,
                    campaign_ids=campaign_ids,
                    period_hours=period_hours,
                    sandbox=settings.direct_sandbox,
                    timeout_seconds=DIRECT_DEEP_REPORT_TIMEOUT_SECONDS,
                    max_retries=DIRECT_DEEP_REPORT_MAX_RETRIES,
                ),
                DEEP_DIAGNOSTICS_TIMEOUT_SECONDS,
                "Direct ad group report",
            )
            return "ad_group", report, None
        except direct_connector.NotConfiguredError as exc:
            return "ad_group", None, str(exc)
        except asyncio.TimeoutError:
            return "ad_group", None, f"отчёт по группам не готов за {DEEP_DIAGNOSTICS_TIMEOUT_SECONDS:.0f} секунд"
        except direct_connector.DirectConnectorError as exc:
            _log_manual_source_failure("Direct ad group report", exc)
            return "ad_group", None, str(exc)

    async def _fetch_queries() -> tuple[str, dict | None, str | None]:
        try:
            report = await _run_with_timeout(
                direct_connector.fetch_search_query_report(
                    oauth_token=settings.effective_direct_oauth_token,
                    client_login=settings.direct_client_login,
                    campaign_ids=campaign_ids,
                    period_hours=period_hours,
                    sandbox=settings.direct_sandbox,
                    timeout_seconds=DIRECT_DEEP_REPORT_TIMEOUT_SECONDS,
                    max_retries=DIRECT_DEEP_REPORT_MAX_RETRIES,
                ),
                DEEP_DIAGNOSTICS_TIMEOUT_SECONDS,
                "Direct search query report",
            )
            return "query", report, None
        except direct_connector.NotConfiguredError as exc:
            return "query", None, str(exc)
        except asyncio.TimeoutError:
            return "query", None, f"отчёт по поисковым запросам не готов за {DEEP_DIAGNOSTICS_TIMEOUT_SECONDS:.0f} секунд"
        except direct_connector.DirectConnectorError as exc:
            _log_manual_source_failure("Direct search query report", exc)
            return "query", None, str(exc)

    ad_group_report = None
    query_report = None
    partial_errors: dict[str, str] = {}

    for report_name, report, error in await asyncio.gather(_fetch_ad_groups(), _fetch_queries()):
        if report_name == "ad_group":
            ad_group_report = report
        elif report_name == "query":
            query_report = report
        if error:
            partial_errors[report_name] = error

    if ad_group_report is None and query_report is None:
        joined = "; ".join(partial_errors.values()) or "Директ не вернул granular-отчёты"
        return None, joined

    ad_group_rows = (ad_group_report or {}).get("rows") or []
    query_rows = (query_report or {}).get("rows") or []

    # Если отчёт по группам не успел, но отчёт по запросам пришёл, строим
    # pseudo ad-group rows из query rows. Это позволяет посчитать total_clicks
    # и найти query-cluster сигналы, вместо ошибочного "данных мало: 0 кликов".
    if not ad_group_rows and query_rows:
        grouped: dict[tuple[str, str], dict] = {}
        for row in query_rows:
            key = (row.get("ad_group_id") or "unknown", row.get("ad_group_name") or "Без названия")
            if key not in grouped:
                grouped[key] = {
                    "campaign_id": row.get("campaign_id", ""),
                    "campaign_name": row.get("campaign_name", ""),
                    "ad_group_id": key[0],
                    "ad_group_name": key[1],
                    "impressions": 0,
                    "clicks": 0,
                    "cost": 0.0,
                    "ctr": 0.0,
                    "cpc": 0.0,
                }
            target = grouped[key]
            target["impressions"] += int(row.get("impressions") or 0)
            target["clicks"] += int(row.get("clicks") or 0)
            target["cost"] += float(row.get("cost") or 0.0)
        for target in grouped.values():
            impressions = target["impressions"]
            clicks = target["clicks"]
            cost = target["cost"]
            target["ctr"] = round((clicks / impressions * 100) if impressions else 0.0, 2)
            target["cpc"] = round((cost / clicks) if clicks else 0.0, 2)
            target["cost"] = round(cost, 2)
        ad_group_rows = list(grouped.values())
        partial_errors.setdefault("ad_group", "отчёт по группам не пришёл; группировка восстановлена из отчёта по запросам")

    query_clusters = get_query_clusters(project.settings_json)

    clean_period_cutoffs: dict[str, str] = {}
    present_ad_group_ids = {row.get("ad_group_id") for row in ad_group_rows if row.get("ad_group_id")}
    if present_ad_group_ids:
        with get_session() as cp_session:
            for ad_group_id in present_ad_group_ids:
                cutoff = get_latest_cutoff(cp_session, project.id, dimension_type="ad_group", dimension_id=ad_group_id)
                if cutoff is not None:
                    clean_period_cutoffs[ad_group_id] = cutoff.strftime("%d.%m.%Y %H:%M")

    attribution_status = AttributionStatus.not_available

    diag_result = run_diagnostics(
        period_key="7d",
        ad_group_rows=ad_group_rows,
        query_rows=query_rows,
        attribution_status=attribution_status,
        query_clusters=query_clusters,
        clean_period_cutoffs=clean_period_cutoffs,
    )

    known_risks: list = []

    if diag_result.findings:
        from app.diagnostics import _pick_main_finding, finding_key_metric_value

        candidates_in_order = []
        remaining_pool = list(diag_result.findings)
        while remaining_pool:
            top = _pick_main_finding(remaining_pool)
            candidates_in_order.append(top)
            remaining_pool = [f for f in remaining_pool if f is not top]

        with get_session() as ar_session:
            chosen = None
            for candidate in candidates_in_order:
                fp = finding_fingerprint(candidate)
                suppressed = should_suppress_as_primary(
                    ar_session, project.id, fp, "deep_direct", current_payload=candidate.payload,
                )
                if suppressed:
                    known_risks.append(candidate)
                    continue
                chosen = candidate
                record_finding_shown(
                    ar_session, project.id, fp, "deep_direct", candidate.payload,
                    key_metric_value=finding_key_metric_value(candidate),
                )
                break

            diag_result.main_finding = chosen

    diag_result.known_risks = known_risks
    return diag_result, partial_errors or None

def run_cycle_once_sync(project_id: int | None = None) -> CycleResult:
    """Синхронная обёртка для вызова из не-async контекста (например APScheduler job)."""
    return asyncio.run(run_cycle_once(project_id))


def run_cycle_once_sync_with_timeout(
    project_id: int | None = None,
    timeout_seconds: float = RUN_CYCLE_TIMEOUT_SECONDS,
    context_label: str = "scheduled_or_api",
) -> CycleResult:
    """
    Синхронный безопасный запуск полного цикла. Нужен для Telegram /run:
    сам тяжёлый цикл выполняется в отдельном thread через asyncio.to_thread(),
    поэтому Telegram dispatcher/event loop не блокируется внешними API.
    """
    token = _RUN_CONTEXT.set(context_label)
    try:
        return asyncio.run(asyncio.wait_for(run_cycle_once(project_id), timeout=timeout_seconds))
    finally:
        _RUN_CONTEXT.reset(token)



async def force_refresh_deep_diagnostics(project_id: int | None = None) -> dict:
    """
    Принудительный запуск deep diagnostics, минуя кэш -- используется
    кнопкой "Проверить глубже" в Telegram. В отличие от автоматического
    пути в run_cycle_once(), здесь НЕТ проверки should_run_deep_diagnostics()
    -- пользователь явно попросил, ему не нужно ждать триггера по алерту.

    Возвращает dict: {"ok": True, "result": {...}} или {"ok": False, "error": "..."}.
    Не бросает исключения наружу -- вызывающий Telegram-handler получает
    структурированный ответ для прямого показа пользователю.
    """
    settings = get_settings()

    if not (settings.effective_direct_oauth_token and settings.direct_client_login):
        return {"ok": False, "error": "Direct не настроен (нет OAuth-токена или client_login)"}

    with get_session() as session:
        if project_id is not None:
            project = session.get(Project, project_id)
        else:
            project = session.exec(select(Project).where(Project.is_active == True)).first()

        if project is None:
            return {"ok": False, "error": "Активный проект не найден"}

        diag_result, diag_error = await run_deep_diagnostics_for_project(
            project, settings, period_hours=ANALYSIS_WINDOWS_HOURS["7d"],
        )

        if diag_result is None:
            save_diagnostics_cache(session, project.id, "7d", "manual_refresh", {}, ok=False, error=diag_error)
            return {"ok": False, "error": diag_error}

        result_dict = diag_result.to_dict()
        result_dict["_from_cache"] = False
        if diag_error:
            result_dict["_partial"] = True
            result_dict["_partial_errors"] = diag_error
        save_diagnostics_cache(session, project.id, "7d", "manual_refresh", result_dict)

        return {"ok": True, "result": result_dict}


def force_refresh_deep_diagnostics_sync_with_timeout(
    project_id: int | None = None,
    timeout_seconds: float = MANUAL_DEEP_DIRECT_TIMEOUT_SECONDS,
) -> dict:
    """
    Synchronous safe wrapper for manual /deep_direct.

    Telegram handlers call this through asyncio.to_thread(), so a slow Direct
    Reports API cannot block the bot event loop. The wrapper always returns a
    structured dict; exceptions and timeouts do not leak into the handler.
    """
    token = _RUN_CONTEXT.set("manual_deep_direct")
    try:
        return asyncio.run(asyncio.wait_for(force_refresh_deep_diagnostics(project_id), timeout=timeout_seconds))
    except asyncio.TimeoutError:
        logger.warning(
            "Manual /deep_direct timed out after %.1f sec (project_id=%s)",
            timeout_seconds,
            project_id,
        )
        return {
            "ok": False,
            "timeout": True,
            "error": f"Директ не успел подготовить глубокий отчёт за {timeout_seconds:.0f} секунд",
        }
    except Exception as exc:
        logger.exception("Manual /deep_direct failed with traceback (project_id=%s)", project_id)
        return {"ok": False, "timeout": False, "error": f"Внутренняя ошибка: {exc.__class__.__name__}"}
    finally:
        _RUN_CONTEXT.reset(token)


# ---------------------------------------------------------------------------
# Product Onboarding Diagnostics
# ---------------------------------------------------------------------------


async def run_onboarding_diagnostics_for_project(project: Project, settings, period_hours: int = 24) -> dict:
    """
    Запускает onboarding diagnostics. Возвращает dict с тем же форматом
    исхода, что и Direct diagnostics, но с дополнительным учётом
    NotAvailableError -- endpoint в TruePost может просто не существовать,
    это не ошибка ("error"), а отдельный статус ("not_available").

    Возвращает {"status": "ok"|"not_available"|"error", "result": {...}|None,
    "error": "..."|None} -- единый формат для scheduler.py/telegram_bot.py,
    не бросает исключения наружу.
    """
    if not project.base_url or not settings.project_internal_api_token:
        return {"status": "not_available", "result": None, "error": None}

    try:
        connector_result = await _run_with_timeout(
            onboarding_connector.fetch_onboarding_diagnostics(
                base_url=project.base_url,
                api_token=settings.project_internal_api_token,
                period_hours=period_hours,
            ),
            CONNECTOR_CALL_TIMEOUT_SECONDS,
            "Onboarding diagnostics",
        )
    except onboarding_connector.NotAvailableError:
        # Ожидаемая ситуация -- endpoint не реализован. Не error.
        return {"status": "not_available", "result": None, "error": None}
    except asyncio.TimeoutError:
        return {"status": "error", "result": None, "error": f"Диагностика онбординга не ответила за {CONNECTOR_CALL_TIMEOUT_SECONDS:.0f} секунд"}
    except onboarding_connector.OnboardingConnectorError as exc:
        return {"status": "error", "result": None, "error": str(exc)}

    diag_result = analyze_onboarding(connector_result)
    return {"status": "ok", "result": diag_result.to_dict(), "error": None}


async def force_refresh_onboarding_diagnostics(project_id: int | None = None) -> dict:
    """
    Принудительный запуск onboarding diagnostics, минуя кэш -- для кнопки
    "Проверить онбординг" / команды /check_onboarding. Не падает на
    отсутствии endpoint -- возвращает status="not_available" с понятным
    текстом, который telegram_bot.py превращает в сообщение пользователю.
    """
    settings = get_settings()

    with get_session() as session:
        if project_id is not None:
            project = session.get(Project, project_id)
        else:
            project = session.exec(select(Project).where(Project.is_active == True)).first()

        if project is None:
            return {"status": "error", "result": None, "error": "Активный проект не найден"}

        outcome = await run_onboarding_diagnostics_for_project(project, settings)

        if outcome["status"] == "ok":
            outcome["result"]["_from_cache"] = False
            save_diagnostics_cache(
                session, project.id, ONBOARDING_CACHE_PERIOD_KEY, "manual_refresh", outcome["result"],
            )
        elif outcome["status"] == "error":
            save_diagnostics_cache(
                session, project.id, ONBOARDING_CACHE_PERIOD_KEY, "manual_refresh",
                {}, ok=False, error=outcome["error"],
            )
        # not_available не кэшируется вообще -- это не "результат с ошибкой",
        # это "источника пока нет", кэшировать нечего, и кэш только усложнил
        # бы переключение на "ok", когда endpoint наконец появится в TruePost.

        return outcome


# ---------------------------------------------------------------------------
# Landing Funnel Diagnostics
# ---------------------------------------------------------------------------


async def run_landing_funnel_diagnostics_for_project(
    project: Project, settings, direct_clicks: int | None, period_hours: int = 24,
) -> dict:
    """
    Запускает landing funnel diagnostics. Возвращает {"status": "ok"|
    "not_configured"|"error", "result": {...}|None, "error": "..."|None}.

    В отличие от onboarding diagnostics, здесь нет статуса "not_available"
    для случая "endpoint не реализован" -- этот endpoint УЖЕ существует и
    протестирован в TruePost (по контракту задачи), поэтому "not_configured"
    означает только "product connector не настроен вообще" (нет base_url/
    token), не "endpoint отсутствует".

    ВАЖНО: direct_clicks ОБЯЗАН быть посчитан за ТОТ ЖЕ period_hours, что
    передаётся сюда -- иначе сравнение Direct clicks (например, за 7 дней)
    с landing_views (например, за 24 часа) даёт false positive "переход с
    рекламы сломан", когда на самом деле это просто два разных окна.
    Вызывающий код (run_cycle_once / force_refresh_landing_funnel_diagnostics)
    обязан запрашивать оба за один и тот же period_hours -- не передавать
    сюда клики за другое окно, даже если оно "более показательное".
    """
    if not project.base_url or not settings.project_internal_api_token:
        return {"status": "not_configured", "result": None, "error": None}

    try:
        connector_result = await _run_with_timeout(
            landing_connector.fetch_landing_funnel_diagnostics(
                base_url=project.base_url,
                api_token=settings.project_internal_api_token,
                period_hours=period_hours,
            ),
            CONNECTOR_CALL_TIMEOUT_SECONDS,
            "Landing funnel diagnostics",
        )
    except landing_connector.NotConfiguredError:
        return {"status": "not_configured", "result": None, "error": None}
    except asyncio.TimeoutError:
        return {"status": "error", "result": None, "error": f"Диагностика лендинга не ответила за {CONNECTOR_CALL_TIMEOUT_SECONDS:.0f} секунд"}
    except landing_connector.LandingConnectorError as exc:
        return {"status": "error", "result": None, "error": str(exc)}

    diag_result = analyze_landing_funnel(connector_result, direct_clicks=direct_clicks, period_hours=period_hours)
    return {"status": "ok", "result": diag_result.to_dict(), "error": None}


async def _send_telegram_notification(settings, text: str) -> bool:
    """Общий helper отправки уведомления всем admin chat_ids. Не кидает исключений.
    Пробует HTML (<b>жирный</b>); при ошибке парсинга откатывается к plain."""
    import re
    import httpx
    api_url = f"https://api.telegram.org/bot{settings.bot_token}/sendMessage"
    plain = re.sub(r"</?(b|i|code|u|s)>", "", text)
    ok_any = False
    async with httpx.AsyncClient(timeout=10.0) as client:
        for chat_id in settings.admin_chat_ids_list:
            try:
                resp = await client.post(api_url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
                if resp.status_code != 200:
                    resp = await client.post(api_url, json={"chat_id": chat_id, "text": plain})
                if resp.status_code == 200:
                    ok_any = True
                else:
                    logger.warning("Notification send failed (chat_id=%s): HTTP %s", chat_id, resp.status_code)
            except Exception as exc:
                logger.warning("Notification send error (chat_id=%s): %s", chat_id, exc)
    return ok_any


async def notify_product_signal_deltas(project: Project, settings, current_payment_path: dict) -> None:
    """
    Live notifications о ключевых шагах пути пользователя.

    Главная точка входа, вызывается из run_cycle_once ПОСЛЕ получения свежих
    payment_path данных, НО ДО перезаписи кэша PAYMENT_PATH_CACHE_PERIOD_KEY.

    Стратегия (v2, приоритет сверху вниз):
    0. Проверяем alert_mode проекта -- если "off", ничего не отправляем.
    1. Пробуем discrete user-events endpoint (Founder Live Feed, fetch_user_events).
       Если доступен -- per-event уведомления с dedup по event_id, с учётом
       smart/founder режима.
    2. Если user-events недоступен -- пробуем per-user journeys endpoint
       (fetch_user_journeys, snapshot diffing). Более грубый, но per-user.
    3. Если journeys тоже недоступен -- откатываемся на старую delta-логику
       (compute_deltas по агрегатам). Гарантирует, что владелец не останется
       без уведомлений, даже если TruePost ничего из нового не задеплоил.

    Не кидает исключений наружу -- сбой live notifications не должен
    останавливать /run. Дедупликация через NotificationLog (event_key).
    """
    if not settings.bot_token or not settings.admin_chat_ids_list:
        return  # некому отправлять

    from app.service import get_alert_mode
    with get_session() as session:
        alert_mode = get_alert_mode(session, project.id)
    if alert_mode == "off":
        return

    # Уровень 1: discrete user-events (Founder Live Feed)
    if project.base_url and settings.project_internal_api_token:
        try:
            from app.connectors.user_events import fetch_user_events
            events_result = await fetch_user_events(
                base_url=project.base_url,
                api_token=settings.project_internal_api_token,
                period_minutes=120,
                limit=200,
            )
        except Exception as exc:
            logger.warning("user_events fetch failed unexpectedly (falling back to journeys): %s", exc)
            events_result = None

        if events_result is not None and events_result.get("ok"):
            events = events_result.get("events") or []
            logger.info("user_events fetched ok (project_id=%s, count=%s, mode=%s)", project.id, len(events), alert_mode)
            await _notify_from_events(project, settings, events, alert_mode)
            return

        if events_result is not None and not events_result.get("ok"):
            logger.info(
                "user_events unavailable (project_id=%s, status=%s, error=%s) -- falling back to journeys",
                project.id, events_result.get("status"), events_result.get("error"),
            )

    journeys_result = None
    if project.base_url and settings.project_internal_api_token:
        try:
            from app.connectors.user_journeys import fetch_user_journeys
            journeys_result = await fetch_user_journeys(
                base_url=project.base_url,
                api_token=settings.project_internal_api_token,
                period_hours=24,
                limit=100,
            )
        except Exception as exc:
            logger.warning("user_journeys fetch failed unexpectedly (falling back to deltas): %s", exc)
            journeys_result = None

    if journeys_result is not None and journeys_result.get("ok"):
        journeys = journeys_result.get("journeys") or []
        logger.info(
            "user_journeys fetched ok (project_id=%s, count=%s)",
            project.id, len(journeys),
        )
        try:
            with get_session() as session:
                save_diagnostics_cache(
                    session, project.id, USER_JOURNEYS_CACHE_PERIOD_KEY,
                    "live_run", {"journeys": journeys, "as_of": journeys_result.get("as_of")},
                    ok=True,
                )
        except Exception as cache_exc:
            logger.warning("Failed to cache user_journeys: %s", cache_exc)
        await _notify_from_journeys(project, settings, journeys)
        return

    if journeys_result is not None and not journeys_result.get("ok"):
        logger.info(
            "user_journeys unavailable (project_id=%s, status=%s, error=%s) -- falling back to delta notifications",
            project.id, journeys_result.get("status"), journeys_result.get("error"),
        )

    await _notify_from_deltas(project, settings, current_payment_path)


async def _notify_from_events(project: Project, settings, events: list[dict], alert_mode: str) -> None:
    """
    Founder Live Feed (v2): дискретные события с dedup по event_id.

    Фильтрует события по alert_mode (smart/founder), затем по anti-spam
    guardrail (>FOUNDER_FEED_DIGEST_THRESHOLD событий за цикл -> дайджест),
    затем шлёт оставшиеся по одному с проверкой NotificationLog.

    Отдельно детектирует stuck_tariff_screen из journey_snapshot каждого
    события (не отдельный API-запрос) -- если snapshot показывает
    pricing_viewed=True и payment_started=False, и в событии есть
    достаточно давний pricing_viewed_at, шлём synthetic stuck-уведомление.
    Т.к. discrete events endpoint не даёt minutes_since_last_step напрямую
    в этом контракте, stuck здесь остаётся приблизительным -- полноценный
    stuck detection остаётся на _notify_from_journeys (snapshot diffing).
    """
    from app.notifications import (
        should_notify_event, build_user_event_key, format_feed_event,
        format_feed_digest, FOUNDER_FEED_DIGEST_THRESHOLD,
    )
    from app.service import was_notified, mark_notified

    if not events:
        return

    relevant = [e for e in events if should_notify_event(e.get("event_type", ""), alert_mode)]
    if not relevant:
        return

    if len(relevant) > FOUNDER_FEED_DIGEST_THRESHOLD:
        digest_text = format_feed_digest(relevant)
        digest_signature = "-".join(sorted(e.get("event_id", "") for e in relevant))[:200]
        event_key = f"feed_digest:{project.id}:{hash(digest_signature)}"
        with get_session() as session:
            if was_notified(session, project.id, event_key):
                return
            sent_ok = await _send_telegram_notification(settings, digest_text)
            if sent_ok:
                mark_notified(session, project.id, event_key, "feed_digest")
                logger.info("Live notification sent: feed digest (project_id=%s, count=%s)", project.id, len(relevant))
        return

    # Батч-проверка dedup одним запросом
    candidate_keys = {build_user_event_key(e["event_id"]) for e in relevant}
    with get_session() as session:
        existing = session.exec(
            select(NotificationLog.event_key).where(
                NotificationLog.project_id == project.id,
                NotificationLog.event_key.in_(candidate_keys),
            )
        ).all()
        already_notified = set(existing)

    for event in relevant:
        event_key = build_user_event_key(event["event_id"])
        if event_key in already_notified:
            continue
        text = format_feed_event(event)
        if text is None:
            continue
        with get_session() as session:
            if was_notified(session, project.id, event_key):
                continue  # защита от гонки
            sent_ok = await _send_telegram_notification(settings, text)
            if sent_ok:
                mark_notified(session, project.id, event_key, event.get("event_type", "user_event"), user_id=event.get("user_key"))
                logger.info(
                    "Live notification sent: %s (project_id=%s, user_key=%s)",
                    event.get("event_type"), project.id, event.get("user_key"),
                )


async def _notify_from_journeys(project: Project, settings, journeys: list[dict]) -> None:
    """
    Per-user journey notifications (v1, основной режим).
    Anti-spam: если journeys > 20 за цикл -- дайджест вместо потока.
    """
    from app.notifications import build_journey_event_key, build_journey_notifications
    from app.service import was_notified, mark_notified

    if not journeys:
        return

    DIGEST_THRESHOLD_JOURNEYS = 20

    if len(journeys) > DIGEST_THRESHOLD_JOURNEYS:
        # Дайджест по journeys -- не шлём поток отдельных уведомлений
        progressed = sum(1 for j in journeys if j.get("channel_created_at"))
        priced = sum(1 for j in journeys if j.get("pricing_viewed_at"))
        paid = sum(1 for j in journeys if j.get("payment_success_at"))
        digest_text = (
            f"Активность пользователей — TruePost\n\n"
            f"За последний цикл: {len(journeys)} путей пользователей, "
            f"{progressed} с созданным каналом, {priced} дошли до тарифов, "
            f"{paid} оплатили."
        )
        digest_signature = "-".join(sorted(j.get("user_key", "") for j in journeys))[:200]
        event_key = f"journey_digest:{project.id}:{hash(digest_signature)}"
        with get_session() as session:
            if was_notified(session, project.id, event_key):
                return
            sent_ok = await _send_telegram_notification(settings, digest_text)
            if sent_ok:
                mark_notified(session, project.id, event_key, "journey_digest")
                logger.info("Live notification sent: journey digest (project_id=%s, count=%s)", project.id, len(journeys))
        return

    # Не дайджест -- проверяем какие event_key уже отправлены, одним запросом
    with get_session() as session:
        candidate_keys: set[str] = set()
        for journey in journeys:
            user_key = journey.get("user_key")
            if not user_key:
                continue
            if journey.get("pricing_viewed_at"):
                candidate_keys.add(build_journey_event_key(user_key, "pricing_viewed", journey.get("pricing_viewed_at")))
            if journey.get("payment_started_at"):
                candidate_keys.add(build_journey_event_key(user_key, "payment_started", journey.get("payment_started_at")))
            if journey.get("payment_success_at"):
                candidate_keys.add(build_journey_event_key(user_key, "payment_success", journey.get("payment_success_at")))
            if journey.get("payment_failed_at"):
                candidate_keys.add(build_journey_event_key(user_key, "payment_failed", journey.get("payment_failed_at")))
            candidate_keys.add(build_journey_event_key(user_key, "stuck_tariff_screen", journey.get("pricing_viewed_at")))

        already_notified: set[str] = set()
        if candidate_keys:
            existing = session.exec(
                select(NotificationLog.event_key).where(
                    NotificationLog.project_id == project.id,
                    NotificationLog.event_key.in_(candidate_keys),
                )
            ).all()
            already_notified = set(existing)

    pending = build_journey_notifications(journeys, already_notified)

    for event_key, text in pending:
        with get_session() as session:
            if was_notified(session, project.id, event_key):
                continue  # на случай гонки между чтением candidate_keys и отправкой
            sent_ok = await _send_telegram_notification(settings, text)
            if sent_ok:
                # event_type для NotificationLog -- извлекаем из event_key
                # (формат journey:<user_key>:<step>:<ts>), без user_id PII в event_type поле
                parts = event_key.split(":")
                step = parts[2] if len(parts) > 2 else "journey"
                user_key = parts[1] if len(parts) > 1 else None
                mark_notified(session, project.id, event_key, f"journey_{step}", user_id=user_key)
                logger.info("Live notification sent: journey_%s (project_id=%s)", step, project.id)


async def _notify_from_deltas(project: Project, settings, current_payment_path: dict) -> None:
    """
    Fallback: старая delta-логика по агрегатам (когда journeys endpoint
    недоступен). Полностью сохранена для обратной совместимости.
    """
    from app.notifications import (
        compute_deltas, build_notification_batch, build_event_key, format_notification,
    )
    from app.service import was_notified, mark_notified

    with get_session() as session:
        previous_cache = get_cached_diagnostics(session, project.id, PAYMENT_PATH_CACHE_PERIOD_KEY)
        previous_payload = dict(previous_cache.result_json or {}) if (previous_cache and previous_cache.ok) else None

    deltas = compute_deltas(previous_payload, current_payment_path)
    if not deltas:
        return

    batch = build_notification_batch(deltas)

    if batch.is_digest:
        digest_signature = ":".join(f"{d.event_type}={d.current_value}" for d in sorted(deltas, key=lambda x: x.event_type))
        event_key = f"digest:{project.id}:{digest_signature}"
        with get_session() as session:
            if was_notified(session, project.id, event_key):
                return
            sent_ok = await _send_telegram_notification(settings, batch.digest_text)
            if sent_ok:
                mark_notified(session, project.id, event_key, "digest")
                logger.info("Live notification sent: digest (project_id=%s)", project.id)
        return

    for delta in deltas:
        event_key = build_event_key(project.id, delta.event_type, delta.current_value)
        with get_session() as session:
            if was_notified(session, project.id, event_key):
                continue
            text = format_notification(delta)
            sent_ok = await _send_telegram_notification(settings, text)
            if sent_ok:
                mark_notified(session, project.id, event_key, delta.event_type)
                logger.info(
                    "Live notification sent: %s (project_id=%s, value=%s)",
                    delta.event_type, project.id, delta.current_value,
                )


async def run_payment_path_diagnostics_for_project(
    project: Project, settings, period_hours: int = 168,
) -> dict:
    """
    Запускает payment-path diagnostics для проекта.

    Возвращает {"status": "ok"|"not_configured"|"error",
                "result": {...}|None,
                "error": "..."|None}.

    Не кидает исключений наружу -- все ошибки переходят в status="error".
    Не блокирует /run: вызывается с тем же _run_with_timeout, что и другие
    internal-источники.

    period_hours: используем 7d (168ч) -- максимальное окно, которое
    даёт достаточно данных о попытках оплаты для вывода. Не 24h, потому что
    у малого проекта в сутки может быть 0 событий оплаты -- это не сигнал.
    """
    if not project.base_url or not settings.project_internal_api_token:
        return {"status": "not_configured", "result": None, "error": None}

    try:
        connector_result = await _run_with_timeout(
            payment_path_connector.fetch_payment_path_diagnostics(
                base_url=project.base_url,
                api_token=settings.project_internal_api_token,
                period_hours=period_hours,
            ),
            CONNECTOR_CALL_TIMEOUT_SECONDS,
            "Payment-path diagnostics",
        )
    except payment_path_connector.NotConfiguredError:
        return {"status": "not_configured", "result": None, "error": None}
    except asyncio.TimeoutError:
        return {
            "status": "error",
            "result": None,
            "error": f"Payment-path diagnostics не ответил за {CONNECTOR_CALL_TIMEOUT_SECONDS:.0f} секунд",
        }
    except payment_path_connector.PaymentPathConnectorError as exc:
        return {"status": "error", "result": None, "error": str(exc)}

    return {"status": "ok", "result": connector_result, "error": None}


async def force_refresh_landing_funnel_diagnostics(project_id: int | None = None) -> dict:
    """
    Принудительный запуск landing funnel diagnostics, минуя кэш -- для
    кнопки "Проверить лендинг" / команды /check_landing. Симметрично
    force_refresh_onboarding_diagnostics.

    Использует 7d-окно (168ч) для ОБОИХ источников -- Direct clicks и
    landing funnel -- по тем же причинам, что в run_cycle_once (см.
    комментарий там): сравнение метрик за разные периоды даёт false
    positive в правиле A. period_hours передаётся в оба запроса явно.
    """
    settings = get_settings()
    period_hours = ANALYSIS_WINDOWS_HOURS["7d"]

    with get_session() as session:
        if project_id is not None:
            project = session.get(Project, project_id)
        else:
            project = session.exec(select(Project).where(Project.is_active == True)).first()

        if project is None:
            return {"status": "error", "result": None, "error": "Активный проект не найден"}

        # Берём свежие Direct-клики за ТО ЖЕ окно, что landing funnel ниже --
        # быстрый дополнительный запрос к campaign-level summary (лёгкий, не granular).
        direct_clicks = None
        if settings.effective_direct_oauth_token and settings.direct_client_login:
            try:
                direct_summary = await _run_with_timeout(
                    direct_connector.fetch_metrics(
                        oauth_token=settings.effective_direct_oauth_token,
                        client_login=settings.direct_client_login,
                        campaign_ids=settings.direct_campaign_ids_list,
                        period_hours=period_hours,
                        sandbox=settings.direct_sandbox,
                        timeout_seconds=DIRECT_SUMMARY_TIMEOUT_SECONDS,
                        max_retries=DIRECT_SUMMARY_MAX_RETRIES,
                    ),
                    CONNECTOR_CALL_TIMEOUT_SECONDS,
                    "Direct summary for landing funnel",
                )
                direct_clicks = direct_summary.get("clicks")
            except asyncio.TimeoutError:
                logger.warning("Could not fetch Direct clicks for landing funnel context: timeout")
            except (direct_connector.NotConfiguredError, direct_connector.DirectConnectorError) as exc:
                logger.warning("Could not fetch Direct clicks for landing funnel context: %s", exc)

        outcome = await run_landing_funnel_diagnostics_for_project(
            project, settings, direct_clicks, period_hours=period_hours,
        )

        if outcome["status"] == "ok":
            outcome["result"]["_from_cache"] = False
            save_diagnostics_cache(
                session, project.id, LANDING_FUNNEL_CACHE_PERIOD_KEY, "manual_refresh", outcome["result"],
            )
        elif outcome["status"] == "error":
            save_diagnostics_cache(
                session, project.id, LANDING_FUNNEL_CACHE_PERIOD_KEY, "manual_refresh",
                {}, ok=False, error=outcome["error"],
            )

        return outcome


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
            result = await asyncio.wait_for(run_cycle_once(), timeout=RUN_CYCLE_TIMEOUT_SECONDS)
            logger.info(
                "Cycle complete: notifiable=%s primary=%s",
                result.has_notifiable_changes,
                result.primary_candidate.title if result.primary_candidate else None,
            )
        except asyncio.TimeoutError:
            logger.warning("Scheduled cycle timed out after %.1f sec", RUN_CYCLE_TIMEOUT_SECONDS)
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


# ---------------------------------------------------------------------------
# Ежедневная утренняя сводка владельцу: /board + ДИНАМИКА, push раз в день
# ---------------------------------------------------------------------------

DAILY_BOARD_EVENT_TYPE = "daily_board"


async def send_daily_board(
    force: bool = False,
    *,
    _send=None,
    _session_factory=None,
    _settings=None,
) -> bool:
    """
    Отправляет владельцу утреннюю сводку: доска (/board) + блок ДИНАМИКА
    по дням. Отправляется РАЗ В ДЕНЬ даже если изменений нет -- владелец
    должен видеть, что система работает и сколько данных осталось до
    решения, не дергая /board руками.

    Дедупликация: NotificationLog, event_key = "daily_board:{YYYY-MM-DD}".
    Рестарт процесса/повторный запуск cron в тот же день не шлёт дубль.
    force=True (команда /daily) игнорирует дедуп -- ручной запрос владельца
    всегда отвечает, но точку динамики за день всё равно не дублирует
    (save_daily_counters идемпотентен на день).

    Никогда не бросает исключения наружу -- утренняя сводка не должна
    ронять процесс.

    _send/_session_factory/_settings -- инъекция для тестов, в проде None.
    """
    from app import growth_loop
    from app.commercial_report import (
        build_board_report,
        build_daily_board_message,
        build_morning_story,
        experiment_one_liner,
    )
    from app.service import (
        load_daily_counters_history,
        mark_notified,
        save_daily_counters,
        was_notified,
    )

    send = _send or _send_telegram_notification
    session_factory = _session_factory or get_session
    settings = _settings or get_settings()

    try:
        if not settings.daily_board_enabled:
            return False
        if not settings.bot_token or not settings.admin_chat_ids_list:
            logger.info("Daily board skipped: bot_token/admin_chat_ids not configured")
            return False

        day = datetime.now(timezone.utc).date().isoformat()
        event_key = f"{DAILY_BOARD_EVENT_TYPE}:{day}"

        with session_factory() as session:
            project = session.exec(
                select(Project).where(Project.is_active == True)  # noqa: E712
            ).first()
            if project is None:
                logger.info("Daily board skipped: no active project")
                return False

            if not force and was_notified(session, project.id, event_key):
                return False

            pp_cached = get_cached_diagnostics(
                session, project.id, PAYMENT_PATH_CACHE_PERIOD_KEY
            )
            pp_dict = dict(pp_cached.result_json or {}) if (pp_cached and pp_cached.ok) else None

            # Точка динамики за сегодня -- ДО чтения истории, чтобы сводка
            # включала сегодняшний день. Идемпотентно на день.
            save_daily_counters(session, project.id, pp_dict, day)
            history = load_daily_counters_history(session, project.id, days=7)

            # Состояние цикла -> строка эксперимента + что нужно от владельца.
            experiment_line = None
            owner_action = "ничего — копим данные."
            running = growth_loop.get_running_experiment(session, project.id)
            rec = growth_loop.get_active_recommendation(session, project.id)
            if running is not None:
                progress = growth_loop.experiment_progress(running, pp_dict)
                experiment_line = experiment_one_liner(running, progress)
                owner_action = "ничего — эксперимент копит данные, не менять запертые переменные."
            elif rec is not None:
                experiment_line = f"Ждёт твоего решения: «{rec.title}»."
                owner_action = "открыть /board и нажать Принять / Отклонить."
            else:
                last = growth_loop.get_last_finished_experiment(session, project.id)
                if last is not None and last.ended_at is not None:
                    experiment_line = f"Последний вердикт: {last.verdict} — «{last.title}»."

            story = build_morning_story(history, experiment_line, owner_action)
            board_text = build_board_report(
                project.name,
                None,  # NormalizedMetrics не обязателен: регистрации берём из payment_path
                payment_path=pp_dict,
                new_registrations_since_deploy=(pp_dict or {}).get("registrations"),
                skip_decision=True,  # рассказ выше уже сказал, что происходит
            )
            text = story + "\n\n" + build_daily_board_message(board_text, history)
            project_id = project.id

        sent_ok = await send(settings, text)
        if sent_ok and not force:
            with session_factory() as session:
                mark_notified(
                    session, project_id, event_key, DAILY_BOARD_EVENT_TYPE,
                    payload={"day": day},
                )
        return bool(sent_ok)
    except Exception:
        logger.exception("Daily board failed")
        return False


# ---------------------------------------------------------------------------
# Growth Loop: тик после каждого обновления payment_path + уведомления
# ---------------------------------------------------------------------------

async def growth_loop_tick_and_notify(
    project: "Project",
    settings,
    payment_path: dict | None,
    *,
    _send=None,
    _session_factory=None,
) -> dict:
    """
    Один шаг Growth Loop после обновления payment_path:
    1) обновить прогресс идущего эксперимента; если порог достигнут --
       автоматический вердикт + уведомление владельцу;
    2) если нет ни эксперимента, ни активной рекомендации -- предложить
       новую рекомендацию + уведомление с призывом открыть /board.

    Дедупликация уведомлений через NotificationLog:
    - вердикт: "gl_verdict:{experiment_id}"
    - предложение: "gl_proposed:{recommendation_id}"
    Никогда не бросает исключения наружу.
    """
    from app import growth_loop
    from app.truepost_playbook import truepost_playbook
    from app.service import mark_notified, was_notified

    send = _send or _send_telegram_notification
    session_factory = _session_factory or get_session

    outcome = {"verdict_sent": False, "proposal_sent": False}
    try:
        with session_factory() as session:
            db_project = session.get(Project, project.id)
            if db_project is None:
                return outcome
            result = growth_loop.tick(
                session, db_project.id, payment_path,
                truepost_playbook, db_project.settings_json,
            )
            finished = result["finished_experiment"]
            new_rec = result["new_recommendation"]

            finished_payload = None
            if finished is not None:
                finished_payload = {
                    "id": finished.id,
                    "title": finished.title,
                    "verdict": finished.verdict,
                    "summary": finished.result_summary,
                }
            rec_payload = None
            if new_rec is not None:
                rec_payload = {
                    "id": new_rec.id,
                    "title": new_rec.title,
                    "action": new_rec.action,
                }
            project_id = db_project.id

        if finished_payload is not None:
            key = f"gl_verdict:{finished_payload['id']}"
            with session_factory() as session:
                already = was_notified(session, project_id, key)
            if not already:
                text = (
                    f"⚖️ <b>ВЕРДИКТ — {finished_payload['verdict']}</b>\n\n"
                    f"{finished_payload['title']}\n\n"
                    f"{finished_payload['summary']}\n\n"
                    f"Следующий шаг: /board"
                )
                if await send(settings, text):
                    with session_factory() as session:
                        mark_notified(session, project_id, key, "gl_verdict")
                    outcome["verdict_sent"] = True

        if rec_payload is not None:
            key = f"gl_proposed:{rec_payload['id']}"
            with session_factory() as session:
                already = was_notified(session, project_id, key)
            if not already:
                text = (
                    f"💡 <b>НОВОЕ ПРЕДЛОЖЕНИЕ</b>\n\n"
                    f"{rec_payload['title']}\n\n"
                    f"{rec_payload['action']}\n\n"
                    f"Принять/отклонить: /board"
                )
                if await send(settings, text):
                    with session_factory() as session:
                        mark_notified(session, project_id, key, "gl_proposed")
                    outcome["proposal_sent"] = True
    except Exception:
        logger.exception("growth_loop_tick_and_notify failed")
    return outcome
