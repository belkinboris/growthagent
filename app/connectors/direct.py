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
    Возвращает dict: spend, clicks, impressions, ctr, cpc, campaigns_count,
    "_diagnostics" (raw status codes по попыткам, период, кол-во строк).

    campaign_ids может быть пустым списком -- тогда отчёт строится по ВСЕМ
    кампаниям аккаунта (без фильтра CampaignId). Это сознательное
    поведение: если DIRECT_CAMPAIGN_IDS_JSON не задан, агент видит всю
    рекламную активность аккаунта, не "ничего".

    Бросает NotConfiguredError, если токен/client_login отсутствуют.
    Бросает DirectConnectorError при сетевой ошибке/таймауте/401/403/
    invalid report spec/превышении max_retries при "отчёт всё ещё формируется".
    """
    if not oauth_token or not client_login:
        raise NotConfiguredError("DIRECT_OAUTH_TOKEN (or YANDEX_OAUTH_TOKEN) or DIRECT_CLIENT_LOGIN not set")

    url = SANDBOX_API_URL if sandbox else REPORTS_API_URL
    date_from, date_to = _period_to_dates(period_hours)
    report_definition = _build_report_definition(campaign_ids, date_from, date_to)

    headers = {
        "Authorization": f"Bearer {oauth_token}",
        "Client-Login": client_login,
        "Accept-Language": "ru",
        "processingMode": "auto",  # сервер сам решает: сразу отдать или поставить в очередь
        "returnMoneyInMicros": "true",  # явно фиксируем единицы Cost, не полагаемся на дефолт
        "skipReportHeader": "true",
        "skipReportSummary": "true",  # без summary-секции в начале, чище парсинг TSV
        "skipColumnHeader": "false",
    }

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
            return _handle_success(response.text, attempt_statuses, date_from, date_to)

        if response.status_code in (201, 202):
            retry_in = response.headers.get("retryIn") or response.headers.get("RetryIn")
            wait_seconds = float(retry_in) if retry_in else DEFAULT_RETRY_FALLBACK_SECONDS
            logger.info(
                "Direct report not ready yet (status=%s), retrying in %s sec (attempt %d/%d)",
                response.status_code, wait_seconds, attempt + 1, max_retries,
            )
            await asyncio.sleep(wait_seconds)
            continue

        # 4xx/5xx -- разбираем тело ошибки, если оно JSON (Директ обычно
        # возвращает {"error": {"error_code": ..., "error_string": ...,
        # "error_detail": ...}} для ошибок отчёта).
        error_message = _parse_error_body(response.status_code, response.text)
        raise DirectConnectorError(error_message)

    raise DirectConnectorError(
        f"Report still processing after {max_retries} attempts (statuses: {attempt_statuses})"
    )


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
    за последние 24 часа строится успешно. Не бросает исключения наружу.
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
