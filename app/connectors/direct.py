"""
Connector для Яндекс.Директа (Reports Service).

Документация: https://yandex.ru/dev/direct/doc/reports/reports.html

Read-only. Никаких изменений ставок, бюджетов, кампаний, объявлений --
этот модуль только запрашивает статистику.

Reports Service устроен иначе, чем Метрика Reports API:
- Запрос -- POST с JSON-телом report definition (ReportDefinition), не GET
  с query-параметрами.
- Ответ может быть НЕ готов сразу: API возвращает 201 ("отчёт принят в
  очередь") или 202 ("отчёт формируется") с заголовком RetryIn (секунды
  до повторного запроса). Нужен retry-цикл, не одна попытка.
- При успехе (200) тело ответа -- TSV (tab-separated values), не JSON:
  первая строка -- заголовки колонок, последняя содержательная строка --
  Total (агрегат), возможна финальная пустая строка.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.config import DIRECT_REPORT_RETRY_SLEEP_CAP_SECONDS

logger = logging.getLogger("growth_agent.connectors.direct")


REPORTS_API_URL = "https://api.direct.yandex.com/json/v5/reports"
SANDBOX_API_URL = "https://api-sandbox.direct.yandex.com/json/v5/reports"

DEFAULT_MAX_RETRIES = 5
DEFAULT_RETRY_FALLBACK_SECONDS = 5  # если сервер не присылает RetryIn


class DirectConnectorError(Exception):
    pass


class NotConfiguredError(Exception):
    pass


class ReportStillProcessingError(Exception):
    """
    Внутреннее исключение -- отчёт ещё формируется после всех попыток
    retry. Наружу из fetch_metrics() это превращается в
    DirectConnectorError с понятным сообщением, не утекает как отдельный
    тип за пределы модуля.
    """
    pass


def _period_to_dates(period_hours: int) -> tuple:
    """
    Reports Service Директа, как и Метрика, работает по датам (day-level),
    не по часам -- DateRangeType CUSTOM_DATE с DateFrom/DateTo в формате
    YYYY-MM-DD. Это известное ограничение API, не connector'а (см. также
    metrika.py -- та же логика).
    """
    now = datetime.now(timezone.utc)
    if period_hours <= 24:
        date_from = now.date().isoformat()
    else:
        date_from = (now - timedelta(hours=period_hours)).date().isoformat()
    date_to = now.date().isoformat()
    return date_from, date_to


def _build_report_definition(campaign_ids: list, date_from: str, date_to: str) -> dict:
    selection_criteria = {"DateFrom": date_from, "DateTo": date_to}
    if campaign_ids:
        selection_criteria["Filter"] = [
            {"Field": "CampaignId", "Operator": "IN", "Values": [str(c) for c in campaign_ids]}
        ]

    return {
        "params": {
            "SelectionCriteria": selection_criteria,
            "FieldNames": [
                "CampaignId",
                "CampaignName",
                "Impressions",
                "Clicks",
                "Cost",
                "Ctr",
                "AvgCpc",
            ],
            "ReportName": f"GrowthAgent_{date_from}_{date_to}",
            "ReportType": "CAMPAIGN_PERFORMANCE_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        }
    }


def _parse_tsv(text: str) -> tuple[list, list]:
    """
    Парсит TSV-ответ Reports Service. Структура (см. документацию):
        строка 1: заголовки колонок
        строки 2..N: данные построчно (одна строка на кампанию)
        последняя содержательная строка: "Total rows" + агрегаты
        возможна финальная пустая строка

    Возвращает (header, data_rows) -- data_rows БЕЗ строки Total (она
    обрабатывается отдельно вызывающим кодом, потому что Cost в Total --
    уже агрегированная сумма, не нужно складывать её ещё раз с построчными
    данными).

    Если TSV пустой или содержит только заголовок без данных и без Total --
    это считается "нет данных за период" (нули), не ошибкой -- такое
    бывает у кампании без показов за выбранную дату.
    """
    lines = [line for line in text.strip().split("\n") if line.strip()]

    if not lines:
        return [], []

    header = lines[0].split("\t")
    data_rows = []

    for line in lines[1:]:
        cells = line.split("\t")
        if cells and cells[0].strip().lower() == "total rows":
            continue  # строка Total пропускается -- агрегируем сами из data_rows
        if len(cells) != len(header):
            # malformed-строка (не совпадает число колонок с заголовком) --
            # пропускаем эту строку, но не весь отчёт целиком, и логируем,
            # чтобы не терять остальные валидные кампании из-за одной
            # повреждённой строки.
            logger.warning("Skipping malformed TSV row (column count mismatch): %r", line)
            continue
        data_rows.append(dict(zip(header, cells)))

    return header, data_rows


def _aggregate_rows(data_rows: list) -> dict:
    """
    Складывает Impressions/Clicks/Cost по всем строкам (кампаниям) и
    пересчитывает CTR/CPC из агрегатов, а не усредняет готовые Ctr/AvgCpc
    из построчных данных -- усреднение процентов/средних по нескольким
    кампаниям даёт неверный результат (например, среднее двух CTR не
    равно общему CTR, если у кампаний разное количество показов).

    Cost в Reports Service приходит в "условных единицах" * 1,000,000
    (микро-единицы валюты) -- это особенность API Директа, делим на
    1_000_000 чтобы получить рубли.
    """
    total_impressions = 0
    total_clicks = 0
    total_cost_micros = 0

    for row in data_rows:
        try:
            total_impressions += int(row.get("Impressions", 0) or 0)
        except ValueError:
            pass
        try:
            total_clicks += int(row.get("Clicks", 0) or 0)
        except ValueError:
            pass
        try:
            cost_raw = row.get("Cost", "0") or "0"
            total_cost_micros += int(float(cost_raw))
        except ValueError:
            pass

    spend = total_cost_micros / 1_000_000
    ctr = (total_clicks / total_impressions * 100) if total_impressions > 0 else 0.0
    cpc = (spend / total_clicks) if total_clicks > 0 else 0.0

    return {
        "spend": spend,
        "clicks": total_clicks,
        "impressions": total_impressions,
        "ctr": round(ctr, 2),
        "cpc": round(cpc, 2),
        "campaigns_count": len(data_rows),
    }


def _build_headers(oauth_token: str, client_login: Optional[str]) -> dict:
    headers = {
        "Authorization": f"Bearer {oauth_token}",
        "Accept-Language": "ru",
        "processingMode": "auto",  # сервер сам решает: сразу отдать или поставить в очередь
        "returnMoneyInMicros": "true",  # явно фиксируем единицы Cost, не полагаемся на дефолт
        "skipReportHeader": "true",
        "skipReportSummary": "true",  # без summary-секции в начале, чище парсинг TSV
        "skipColumnHeader": "false",
    }
    if client_login:
        # Client-Login нужен только для агентских аккаунтов, управляющих
        # чужими рекламными кабинетами. Для прямого рекламодателя (обычный
        # случай) этот заголовок не обязателен -- передаём его только если
        # значение реально задано, не пустую строку.
        headers["Client-Login"] = client_login
    return headers


async def _execute_report_request(
    report_definition: dict,
    oauth_token: str,
    client_login: Optional[str],
    sandbox: bool,
    timeout_seconds: float,
    max_retries: int,
) -> tuple[str, list]:
    """
    Общий retry-цикл для любого типа отчёта Reports Service (campaign,
    ad_group, query). Возвращает (response_text, attempt_statuses) при
    успехе (200), бросает DirectConnectorError при ошибке/превышении
    max_retries -- одна реализация retry-логики для всех уровней
    granular-диагностики, чтобы три копии этого кода не разошлись при
    будущих правках.
    """
    url = SANDBOX_API_URL if sandbox else REPORTS_API_URL
    headers = _build_headers(oauth_token, client_login)

    attempt_statuses = []

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.post(url, headers=headers, json=report_definition)
        except httpx.TimeoutException as exc:
            raise DirectConnectorError(f"Timeout calling Direct Reports API: {exc}") from exc
        except httpx.HTTPError as exc:
            raise DirectConnectorError(f"HTTP error calling Direct Reports API: {exc}") from exc

        attempt_statuses.append(response.status_code)

        if response.status_code == 200:
            return response.text, attempt_statuses

        if response.status_code in (201, 202):
            retry_in = response.headers.get("retryIn") or response.headers.get("RetryIn")
            requested_wait_seconds = float(retry_in) if retry_in else DEFAULT_RETRY_FALLBACK_SECONDS
            wait_seconds = min(requested_wait_seconds, DIRECT_REPORT_RETRY_SLEEP_CAP_SECONDS)
            if wait_seconds < requested_wait_seconds:
                logger.info(
                    "Direct report requested retry in %s sec; capped to %s sec for runtime safety",
                    requested_wait_seconds, wait_seconds,
                )
            logger.info(
                "Direct report not ready yet (status=%s), retrying in %s sec (attempt %d/%d)",
                response.status_code, wait_seconds, attempt + 1, max_retries,
            )
            await asyncio.sleep(wait_seconds)
            continue

        error_message = _parse_error_body(response.status_code, response.text)
        raise DirectConnectorError(error_message)

    raise DirectConnectorError(
        f"Report still processing after {max_retries} attempts (statuses: {attempt_statuses})"
    )


async def fetch_metrics(
    oauth_token: Optional[str],
    client_login: Optional[str],
    campaign_ids: list,
    period_hours: int,
    sandbox: bool = False,
    timeout_seconds: float = 30.0,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """
    Campaign-level summary (как было). Возвращает dict: spend, clicks,
    impressions, ctr, cpc, campaigns_count, "_diagnostics".

    campaign_ids может быть пустым списком -- тогда отчёт строится по ВСЕМ
    кампаниям аккаунта (без фильтра CampaignId). Это сознательное
    поведение: если DIRECT_CAMPAIGN_IDS не задан, агент видит всю
    рекламную активность аккаунта, не "ничего".

    Бросает NotConfiguredError, если токен/client_login отсутствуют.
    Бросает DirectConnectorError при сетевой ошибке/таймауте/401/403/
    invalid report spec/превышении max_retries при "отчёт всё ещё формируется".
    """
    if not oauth_token or not client_login:
        raise NotConfiguredError("DIRECT_OAUTH_TOKEN (or YANDEX_OAUTH_TOKEN) or DIRECT_CLIENT_LOGIN not set")

    date_from, date_to = _period_to_dates(period_hours)
    report_definition = _build_report_definition(campaign_ids, date_from, date_to)

    text, attempt_statuses = await _execute_report_request(
        report_definition, oauth_token, client_login, sandbox, timeout_seconds, max_retries,
    )
    return _handle_success(text, attempt_statuses, date_from, date_to)


def _handle_success(text: str, attempt_statuses: list, date_from: str, date_to: str) -> dict:
    header, data_rows = _parse_tsv(text)

    if not header:
        # Полностью пустой ответ -- нет данных за период, не ошибка.
        logger.info("Direct report is empty -- treating as zero data, not an error")
        result = {"spend": 0.0, "clicks": 0, "impressions": 0, "ctr": 0.0, "cpc": 0.0, "campaigns_count": 0}
    else:
        result = _aggregate_rows(data_rows)

    result["_diagnostics"] = {
        "attempt_statuses": attempt_statuses,
        "date_from": date_from,
        "date_to": date_to,
        "rows_returned": len(data_rows) if header else 0,
    }
    return result


# ---------------------------------------------------------------------------
# Granular reports (ad_group / search_query level) -- Direct Deep Diagnostics
# ---------------------------------------------------------------------------
#
# Reports Service не отдаёт keyword-уровень отдельным ReportType для всех
# типов кампаний одинаково -- доступность зависит от типа кампании (поиск/
# РСЯ/смарт-баннеры). Поэтому keyword-уровень в v1 не реализован отдельной
# функцией: ad_group и query уровни покрывают диагностику, а keyword будет
# добавлен отдельно, если понадобится, без изменения существующих функций.


def _build_ad_group_report_definition(campaign_ids: list, date_from: str, date_to: str) -> dict:
    selection_criteria = {"DateFrom": date_from, "DateTo": date_to}
    if campaign_ids:
        selection_criteria["Filter"] = [
            {"Field": "CampaignId", "Operator": "IN", "Values": [str(c) for c in campaign_ids]}
        ]

    return {
        "params": {
            "SelectionCriteria": selection_criteria,
            "FieldNames": [
                "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
                "Impressions", "Clicks", "Cost", "Ctr", "AvgCpc",
            ],
            "ReportName": f"GrowthAgent_AdGroup_{date_from}_{date_to}",
            "ReportType": "ADGROUP_PERFORMANCE_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        }
    }


def _build_search_query_report_definition(campaign_ids: list, date_from: str, date_to: str) -> dict:
    selection_criteria = {"DateFrom": date_from, "DateTo": date_to}
    if campaign_ids:
        selection_criteria["Filter"] = [
            {"Field": "CampaignId", "Operator": "IN", "Values": [str(c) for c in campaign_ids]}
        ]

    return {
        "params": {
            "SelectionCriteria": selection_criteria,
            # AdGroupName в SEARCH_QUERY_PERFORMANCE_REPORT не всегда
            # доступен (зависит от типа кампании) -- запрашиваем, но
            # parsing должен пережить его отсутствие в ответе (см.
            # _parse_tsv: malformed/недостающие колонки не валят весь отчёт).
            "FieldNames": [
                "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
                "Query", "Impressions", "Clicks", "Cost", "Ctr", "AvgCpc",
            ],
            "ReportName": f"GrowthAgent_Query_{date_from}_{date_to}",
            "ReportType": "SEARCH_QUERY_PERFORMANCE_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        }
    }


def _row_metrics(row: dict) -> dict:
    """
    Превращает одну TSV-строку (dict из заголовка и значений) в нормальные
    числа -- impressions/clicks как int, cost как рубли (делим micros),
    ctr/cpc как float. Используется одинаково для ad_group и query уровней,
    чтобы не дублировать парсинг чисел.
    """
    def _to_int(value):
        try:
            return int(value)
        except (ValueError, TypeError):
            return 0

    def _to_float(value):
        try:
            return float(value)
        except (ValueError, TypeError):
            return 0.0

    impressions = _to_int(row.get("Impressions", 0))
    clicks = _to_int(row.get("Clicks", 0))
    # int() перед делением -- та же обработка, что в _aggregate_rows() для
    # campaign-level (см. _aggregate_rows: total_cost_micros += int(float(...))),
    # чтобы оба пути расчёта Cost давали идентичный результат на одних и тех
    # же исходных данных, а не расходились на доли копейки из-за разного
    # порядка округления.
    cost_micros = int(_to_float(row.get("Cost", 0)))
    cost_rub = cost_micros / 1_000_000

    return {
        "impressions": impressions,
        "clicks": clicks,
        "cost": round(cost_rub, 2),
        "ctr": round((clicks / impressions * 100) if impressions > 0 else 0.0, 2),
        "cpc": round((cost_rub / clicks) if clicks > 0 else 0.0, 2),
    }


async def fetch_ad_group_report(
    oauth_token: Optional[str],
    client_login: Optional[str],
    campaign_ids: list,
    period_hours: int,
    sandbox: bool = False,
    timeout_seconds: float = 30.0,
    max_retries: int = DEFAULT_MAX_RETRIES,
    date_from_override: Optional[str] = None,
    date_to_override: Optional[str] = None,
) -> dict:
    """
    Возвращает dict: {"rows": [{"campaign_id", "campaign_name", "ad_group_id",
    "ad_group_name", "impressions", "clicks", "cost", "ctr", "cpc"}, ...],
    "_diagnostics": {...}}.

    Это более тяжёлый запрос, чем fetch_metrics() -- вызывающий код
    (diagnostics.py) должен решать, когда его реально запускать (см.
    should_run_deep_diagnostics() в service.py), не на каждый /run.

    date_from_override/date_to_override -- явное окно в формате YYYY-MM-DD,
    переопределяющее period_hours. Нужно для clean-period анализа (см.
    service.get_latest_cutoff): если для сегмента зарегистрирован cutoff
    внутри обычного периода, Growth Agent должен перезапросить Director
    именно за окно cutoff_at -> сейчас, а не пытаться отфильтровать уже
    агрегированные построчные данные постфактум -- Reports API не отдаёт
    per-row timestamp клика, только дневные суммы, поэтому точная
    постфактум-фильтрация физически невозможна без отдельного запроса.

    Бросает те же исключения, что fetch_metrics().
    """
    if not oauth_token or not client_login:
        raise NotConfiguredError("DIRECT_OAUTH_TOKEN (or YANDEX_OAUTH_TOKEN) or DIRECT_CLIENT_LOGIN not set")

    if date_from_override is not None and date_to_override is not None:
        date_from, date_to = date_from_override, date_to_override
    else:
        date_from, date_to = _period_to_dates(period_hours)
    report_definition = _build_ad_group_report_definition(campaign_ids, date_from, date_to)

    text, attempt_statuses = await _execute_report_request(
        report_definition, oauth_token, client_login, sandbox, timeout_seconds, max_retries,
    )

    header, data_rows = _parse_tsv(text)
    rows = []
    for row in data_rows:
        metrics = _row_metrics(row)
        rows.append({
            "campaign_id": row.get("CampaignId", ""),
            "campaign_name": row.get("CampaignName", ""),
            "ad_group_id": row.get("AdGroupId", ""),
            "ad_group_name": row.get("AdGroupName", ""),
            **metrics,
        })

    return {
        "rows": rows,
        "_diagnostics": {
            "attempt_statuses": attempt_statuses,
            "date_from": date_from,
            "date_to": date_to,
            "rows_returned": len(rows),
        },
    }


async def fetch_search_query_report(
    oauth_token: Optional[str],
    client_login: Optional[str],
    campaign_ids: list,
    period_hours: int,
    sandbox: bool = False,
    timeout_seconds: float = 30.0,
    max_retries: int = DEFAULT_MAX_RETRIES,
    date_from_override: Optional[str] = None,
    date_to_override: Optional[str] = None,
) -> dict:
    """
    Возвращает dict: {"rows": [{"campaign_id", "campaign_name", "ad_group_id",
    "ad_group_name", "query", "impressions", "clicks", "cost", "ctr", "cpc"},
    ...], "_diagnostics": {...}}.

    SEARCH_QUERY_PERFORMANCE_REPORT доступен только для кампаний с
    поисковыми показами -- для кампаний только в РСЯ запрос может вернуть
    пустой отчёт (graceful: нули/пустой список, не ошибка, см. обработку
    пустого TSV в _parse_tsv).

    date_from_override/date_to_override -- см. docstring fetch_ad_group_report,
    та же логика для clean-period.
    """
    if not oauth_token or not client_login:
        raise NotConfiguredError("DIRECT_OAUTH_TOKEN (or YANDEX_OAUTH_TOKEN) or DIRECT_CLIENT_LOGIN not set")

    if date_from_override is not None and date_to_override is not None:
        date_from, date_to = date_from_override, date_to_override
    else:
        date_from, date_to = _period_to_dates(period_hours)
    report_definition = _build_search_query_report_definition(campaign_ids, date_from, date_to)

    text, attempt_statuses = await _execute_report_request(
        report_definition, oauth_token, client_login, sandbox, timeout_seconds, max_retries,
    )

    header, data_rows = _parse_tsv(text)
    rows = []
    for row in data_rows:
        metrics = _row_metrics(row)
        query = row.get("Query", "").strip()
        if not query:
            # Строки без текста запроса (например, технический "---" для
            # показов без явного запроса) не несут диагностической ценности
            # для кластеризации -- пропускаем, не падаем.
            continue
        rows.append({
            "campaign_id": row.get("CampaignId", ""),
            "campaign_name": row.get("CampaignName", ""),
            "ad_group_id": row.get("AdGroupId", ""),
            "ad_group_name": row.get("AdGroupName", ""),
            "query": query,
            **metrics,
        })

    return {
        "rows": rows,
        "_diagnostics": {
            "attempt_statuses": attempt_statuses,
            "date_from": date_from,
            "date_to": date_to,
            "rows_returned": len(rows),
        },
    }


def _parse_error_body(status_code: int, text: str) -> str:
    import json

    try:
        body = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return f"HTTP {status_code}: {text[:300]}"

    error = body.get("error", {})
    error_code = error.get("error_code", "")
    error_string = error.get("error_string", "")
    error_detail = error.get("error_detail", "")

    parts = [p for p in [error_string, error_detail] if p]
    detail = "; ".join(parts) if parts else text[:300]

    return f"HTTP {status_code} (error_code={error_code}): {detail}"


def _build_search_query_goal_report_definition(
    campaign_ids: list, date_from: str, date_to: str, goal_ids: list[int]
) -> dict:
    """
    Report definition для SEARCH_QUERY_PERFORMANCE_REPORT с полем Conversions.

    ВАЖНОЕ ОГРАНИЧЕНИЕ API Яндекс.Директ:
    - SelectionCriteria.Goals -- фильтрует кампании/группы у которых есть
      хотя бы одна из указанных целей. Это НЕ разбивка по GoalId.
    - Поле "Conversions" в SEARCH_QUERY_PERFORMANCE_REPORT всегда возвращает
      СУММАРНЫЕ конверсии по ВСЕМ целям, не только по переданным goal_ids.
    - Per-goal разбивка (GoalsConversions) доступна только в
      CAMPAIGN_PERFORMANCE_REPORT и AD_PERFORMANCE_REPORT, не в SEARCH_QUERY.

    Таким образом: "Conversions" нельзя считать регистрациями даже при
    передаче registration_goal_id. Атрибуция остаётся "unreliable".
    Единственная польза goal_ids здесь -- фильтрация кампаний.

    Эта функция оставлена для будущего использования когда/если API изменится.
    Сейчас classifier получает registration_attribution="unreliable".
    """
    selection_criteria = {
        "DateFrom": date_from,
        "DateTo": date_to,
    }
    if goal_ids:
        # Фильтрует кампании/группы, у которых есть эти цели.
        # НЕ разбивает Conversions по целям.
        selection_criteria["Goals"] = [str(g) for g in goal_ids]
    if campaign_ids:
        selection_criteria["Filter"] = [
            {"Field": "CampaignId", "Operator": "IN", "Values": [str(c) for c in campaign_ids]}
        ]

    return {
        "params": {
            "SelectionCriteria": selection_criteria,
            "FieldNames": [
                "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
                "Query", "Impressions", "Clicks", "Cost", "Ctr", "AvgCpc",
                "Conversions",  # СУММАРНЫЕ по всем целям, не per-goal
            ],
            "ReportName": f"GrowthAgent_QueryGoal_{date_from}_{date_to}",
            "ReportType": "SEARCH_QUERY_PERFORMANCE_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        }
    }


async def fetch_search_query_goal_report(
    oauth_token: Optional[str],
    client_login: Optional[str],
    campaign_ids: list,
    goal_ids: list[int],
    period_hours: int,
    sandbox: bool = False,
    timeout_seconds: float = 30.0,
    max_retries: int = DEFAULT_MAX_RETRIES,
    date_from_override: Optional[str] = None,
    date_to_override: Optional[str] = None,
) -> dict:
    """
    Поисковые запросы с полем Conversions (суммарным).

    КРИТИЧЕСКОЕ ОГРАНИЧЕНИЕ: Яндекс.Директ SEARCH_QUERY_PERFORMANCE_REPORT
    не поддерживает per-goal разбивку конверсий. Поле "Conversions" всегда
    возвращает total по всем целям кампании, независимо от goal_ids.

    По этой причине:
    - registration_attribution в строках = "unreliable" (не "reliable")
    - Данные конверсий НЕЛЬЗЯ использовать как число регистраций
    - classify_search_queries() при attribution="none"|"unreliable" не строит
      winner-классификацию на основе этих данных

    goal_ids используется только как фильтр кампаний (SelectionCriteria.Goals),
    не как per-goal разбивка.

    Если goal_ids пустой -- работает как fetch_search_query_report + Conversions.
    Не бросает ValueError (в отличие от предыдущей версии) -- фильтр по целям
    просто не применяется.

    Для надёжной атрибуции регистраций к поисковым запросам нужны данные из
    AutoPost backend (product truth), а не из Direct API.
    """
    if not oauth_token or not client_login:
        raise NotConfiguredError(
            "DIRECT_OAUTH_TOKEN (or YANDEX_OAUTH_TOKEN) or DIRECT_CLIENT_LOGIN not set"
        )

    if date_from_override is not None and date_to_override is not None:
        date_from, date_to = date_from_override, date_to_override
    else:
        date_from, date_to = _period_to_dates(period_hours)

    report_definition = _build_search_query_goal_report_definition(
        campaign_ids, date_from, date_to, goal_ids
    )

    text, attempt_statuses = await _execute_report_request(
        report_definition, oauth_token, client_login, sandbox, timeout_seconds, max_retries,
    )

    header, data_rows = _parse_tsv(text)
    rows = []
    for row in data_rows:
        metrics = _row_metrics(row)
        query = row.get("Query", "").strip()
        if not query:
            continue

        # Conversions -- суммарные, НЕ per-goal. Не считать регистрациями.
        try:
            conversions = int(row.get("Conversions", 0) or 0)
        except (ValueError, TypeError):
            conversions = 0

        rows.append({
            "campaign_id": row.get("CampaignId", ""),
            "campaign_name": row.get("CampaignName", ""),
            "ad_group_id": row.get("AdGroupId", ""),
            "ad_group_name": row.get("AdGroupName", ""),
            "query": query,
            "conversions_total": conversions,        # total, не регистрации
            "registrations": None,                   # нет надёжной per-goal атрибуции
            "registration_attribution": "unreliable", # НЕ reliable
            **metrics,
        })

    return {
        "rows": rows,
        "goal_ids": goal_ids,
        "registration_attribution": "unreliable",   # честная маркировка
        "attribution_note": (
            "SEARCH_QUERY_PERFORMANCE_REPORT не поддерживает per-goal разбивку. "
            "Conversions = total по всем целям. Используйте backend product truth "
            "для атрибуции регистраций."
        ),
        "_diagnostics": {
            "attempt_statuses": attempt_statuses,
            "date_from": date_from,
            "date_to": date_to,
            "rows_returned": len(rows),
            "goal_ids": goal_ids,
        },
    }



    import json

    try:
        body = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return f"HTTP {status_code}: {text[:300]}"

    error = body.get("error", {})
    error_code = error.get("error_code", "")
    error_string = error.get("error_string", "")
    error_detail = error.get("error_detail", "")

    parts = [p for p in [error_string, error_detail] if p]
    detail = "; ".join(parts) if parts else text[:300]

    return f"HTTP {status_code} (error_code={error_code}): {detail}"


# ---------------------------------------------------------------------------
# Debug-функция
# ---------------------------------------------------------------------------


async def test_direct_connection(
    oauth_token: Optional[str],
    client_login: Optional[str],
    campaign_ids: Optional[list] = None,
    sandbox: bool = False,
) -> dict:
    """
    Минимальная проверка: токен валиден, client_login имеет доступ, отчёт
    за текущие календарные сутки (UTC) строится успешно -- не "последние
    24 часа" в смысле скользящего окна (см. _period_to_dates: Reports
    Service работает по DateFrom/DateTo датам, не точным timestamp).
    Не бросает исключения наружу.
    """
    if not oauth_token or not client_login:
        return {"ok": False, "error": "DIRECT_OAUTH_TOKEN or DIRECT_CLIENT_LOGIN not set", "stage": "config"}

    try:
        result = await fetch_metrics(
            oauth_token=oauth_token,
            client_login=client_login,
            campaign_ids=campaign_ids or [],
            period_hours=24,
            sandbox=sandbox,
        )
    except NotConfiguredError as exc:
        return {"ok": False, "error": str(exc), "stage": "config"}
    except DirectConnectorError as exc:
        return {"ok": False, "error": str(exc), "stage": "api_call"}

    return {
        "ok": True,
        "spend": result.get("spend"),
        "clicks": result.get("clicks"),
        "impressions": result.get("impressions"),
        "campaigns_count": result.get("campaigns_count"),
    }
