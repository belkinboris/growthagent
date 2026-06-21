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
from app.connectors import landing as landing_connector
from app.connectors import metrika as metrika_connector
from app.connectors import onboarding as onboarding_connector
from app.connectors import truepost as truepost_connector
from app.connectors import yookassa as yookassa_connector
from app.db import get_session
from app.diagnostics import run_diagnostics, get_query_clusters, analyze_onboarding, analyze_landing_funnel
from app.models import AttributionStatus, Integration, IntegrationStatus, IntegrationType, Project
from app.rules import NormalizedMetrics, checkable_categories
from app.analyzer import analyze
from app.service import (
    CycleResult,
    LANDING_FUNNEL_CACHE_PERIOD_KEY,
    ONBOARDING_CACHE_PERIOD_KEY,
    check_integration_freshness,
    get_cached_diagnostics,
    process_cycle,
    save_diagnostics_cache,
    save_snapshot,
    should_run_deep_diagnostics,
    should_run_landing_funnel_diagnostics,
    should_run_onboarding_diagnostics,
    should_show_deep_direct_button,
    should_show_landing_funnel_button,
    should_show_onboarding_button,
)

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

        if direct_configured and should_run_deep_diagnostics(result.primary_candidate):
            cached = get_cached_diagnostics(session, project.id, "7d")
            if cached is not None:
                result.deep_diagnostics = cached.result_json
                result.deep_diagnostics["_from_cache"] = True
            else:
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

        # Onboarding diagnostics: симметрично Direct, но триггер -- продуктовые
        # категории (signups_no_activation), не рекламные.
        if product_configured and should_run_onboarding_diagnostics(result.primary_candidate):
            cached_onboarding = get_cached_diagnostics(session, project.id, ONBOARDING_CACHE_PERIOD_KEY)
            if cached_onboarding is not None:
                result.onboarding_diagnostics = cached_onboarding.result_json
                result.onboarding_diagnostics["_from_cache"] = True
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
        if product_configured and should_run_landing_funnel_diagnostics(result.primary_candidate):
            cached_landing = get_cached_diagnostics(session, project.id, LANDING_FUNNEL_CACHE_PERIOD_KEY)
            if cached_landing is not None:
                result.landing_funnel_diagnostics = cached_landing.result_json
                result.landing_funnel_diagnostics["_from_cache"] = True
            else:
                landing_outcome = await run_landing_funnel_diagnostics_for_project(
                    project, settings, metrics_7d.clicks if metrics_7d else None,
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

        return result


async def run_deep_diagnostics_for_project(project: Project, settings, period_hours: int):
    """
    Запускает granular Direct отчёты (ad_group + search_query) и
    diagnostics.run_diagnostics(). Возвращает (DiagnosticsResult, None) при
    успехе или (None, error_message) при сбое -- не бросает исключение
    наружу, чтобы вызывающий код (run_cycle_once или Telegram-кнопка force
    refresh) мог решить, что делать с ошибкой, не оборачивая каждый вызов
    в try/except по отдельности.
    """
    campaign_ids = settings.direct_campaign_ids_list

    if not campaign_ids:
        # В отличие от fetch_metrics() (campaign-level summary), где "без
        # фильтра -- видим весь аккаунт" безопасно как агрегат, для deep
        # diagnostics это рискованнее: если в Director-аккаунте есть другие
        # кампании, не относящиеся к этому проекту, granular-анализ начнёт
        # искать "проблемные группы" и "нерелевантные запросы" в чужой
        # рекламе и присылать по ней находки. Логируем явно, чтобы это не
        # прошло незаметно при первом включении на новом аккаунте.
        logger.warning(
            "DIRECT_CAMPAIGN_IDS not set -- deep diagnostics will analyze ALL campaigns "
            "in the Direct account, not just this project. Set DIRECT_CAMPAIGN_IDS to scope "
            "the analysis if the account has campaigns for other projects."
        )

    try:
        ad_group_report = await direct_connector.fetch_ad_group_report(
            oauth_token=settings.effective_direct_oauth_token,
            client_login=settings.direct_client_login,
            campaign_ids=campaign_ids,
            period_hours=period_hours,
            sandbox=settings.direct_sandbox,
        )
        query_report = await direct_connector.fetch_search_query_report(
            oauth_token=settings.effective_direct_oauth_token,
            client_login=settings.direct_client_login,
            campaign_ids=campaign_ids,
            period_hours=period_hours,
            sandbox=settings.direct_sandbox,
        )
    except direct_connector.NotConfiguredError as exc:
        return None, str(exc)
    except direct_connector.DirectConnectorError as exc:
        return None, str(exc)

    query_clusters = get_query_clusters(project.settings_json)

    # attribution_status: в v1 у нас нет сквозной UTM-атрибуции между
    # Direct и TruePost (TruePost отдаёт просто число регистраций за
    # период, не привязанное к источнику трафика) -- поэтому всегда
    # not_available. Это явное, не угаданное значение: если в будущем
    # появится UTM-трекинг, здесь будет реальная проверка, не константа.
    attribution_status = AttributionStatus.not_available

    diag_result = run_diagnostics(
        period_key="7d",
        ad_group_rows=ad_group_report["rows"],
        query_rows=query_report["rows"],
        attribution_status=attribution_status,
        query_clusters=query_clusters,
    )
    return diag_result, None


def run_cycle_once_sync(project_id: int | None = None) -> CycleResult:
    """Синхронная обёртка для вызова из не-async контекста (например APScheduler job)."""
    return asyncio.run(run_cycle_once(project_id))


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
        save_diagnostics_cache(session, project.id, "7d", "manual_refresh", result_dict)

        return {"ok": True, "result": result_dict}


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
        connector_result = await onboarding_connector.fetch_onboarding_diagnostics(
            base_url=project.base_url,
            api_token=settings.project_internal_api_token,
            period_hours=period_hours,
        )
    except onboarding_connector.NotAvailableError:
        # Ожидаемая ситуация -- endpoint не реализован. Не error.
        return {"status": "not_available", "result": None, "error": None}
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
    project: Project, settings, direct_clicks_7d: int | None, period_hours: int = 24,
) -> dict:
    """
    Запускает landing funnel diagnostics. Возвращает {"status": "ok"|
    "not_configured"|"error", "result": {...}|None, "error": "..."|None}.

    В отличие от onboarding diagnostics, здесь нет статуса "not_available"
    для случая "endpoint не реализован" -- этот endpoint УЖЕ существует и
    протестирован в TruePost (по контракту задачи), поэтому "not_configured"
    означает только "product connector не настроен вообще" (нет base_url/
    token), не "endpoint отсутствует".

    direct_clicks_7d передаётся явно -- клики Director за 7d-окно, нужны
    для правила A (см. diagnostics.analyze_landing_funnel). Используется
    7d-окно для кликов независимо от period_hours самого landing-запроса,
    потому что воронка лендинга анализируется за 24h (более свежий снимок),
    а Direct-контекст -- за более длинное окно для устойчивости сравнения.
    """
    if not project.base_url or not settings.project_internal_api_token:
        return {"status": "not_configured", "result": None, "error": None}

    try:
        connector_result = await landing_connector.fetch_landing_funnel_diagnostics(
            base_url=project.base_url,
            api_token=settings.project_internal_api_token,
            period_hours=period_hours,
        )
    except landing_connector.NotConfiguredError:
        return {"status": "not_configured", "result": None, "error": None}
    except landing_connector.LandingConnectorError as exc:
        return {"status": "error", "result": None, "error": str(exc)}

    diag_result = analyze_landing_funnel(connector_result, direct_clicks=direct_clicks_7d)
    return {"status": "ok", "result": diag_result.to_dict(), "error": None}


async def force_refresh_landing_funnel_diagnostics(project_id: int | None = None) -> dict:
    """
    Принудительный запуск landing funnel diagnostics, минуя кэш -- для
    кнопки "Проверить лендинг" / команды /check_landing. Симметрично
    force_refresh_onboarding_diagnostics.
    """
    settings = get_settings()

    with get_session() as session:
        if project_id is not None:
            project = session.get(Project, project_id)
        else:
            project = session.exec(select(Project).where(Project.is_active == True)).first()

        if project is None:
            return {"status": "error", "result": None, "error": "Активный проект не найден"}

        # Берём свежие Direct-клики за 7d для контекста правила A -- быстрый
        # дополнительный запрос к campaign-level summary (лёгкий, не granular).
        direct_clicks_7d = None
        if settings.effective_direct_oauth_token and settings.direct_client_login:
            try:
                direct_summary = await direct_connector.fetch_metrics(
                    oauth_token=settings.effective_direct_oauth_token,
                    client_login=settings.direct_client_login,
                    campaign_ids=settings.direct_campaign_ids_list,
                    period_hours=ANALYSIS_WINDOWS_HOURS["7d"],
                    sandbox=settings.direct_sandbox,
                )
                direct_clicks_7d = direct_summary.get("clicks")
            except (direct_connector.NotConfiguredError, direct_connector.DirectConnectorError) as exc:
                logger.warning("Could not fetch Direct clicks for landing funnel context: %s", exc)

        outcome = await run_landing_funnel_diagnostics_for_project(project, settings, direct_clicks_7d)

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
