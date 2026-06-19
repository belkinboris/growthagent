"""
Connector для Яндекс.Метрики (Reports API).

Документация: https://yandex.ru/dev/metrika/doc/api2/reporting/intro.html

Ключевая особенность API: цели не передаются как фильтр одного запроса --
каждая цель -- это отдельная МЕТРИКА вида ym:s:goalNNNNNNreaches, где
NNNNNN -- это сам goal_id. Чтобы получить достижения нескольких целей за
один вызов, все эти метрики передаются через запятую в параметре "metrics"
вместе с visits/users.

Лимит Reports API -- 20 метрик на один запрос (см. документацию). Сейчас
используется 2 (visits, users) + 5 целей = 7 метрик, запас большой. Если
число целей вырастет так, что 2 + N целей превысит 20 (то есть больше 18
целей), запрос нужно будет дробить на несколько пачек -- это НЕ сделано
в v1, так как сейчас не нужно, но если решите добавить много целей --
добавьте проверку len(goal_ids) <= 18 и разбивку на батчи в
_build_metrics_param().

Работаем через goal_id (число), не через название цели (register_success
и т.п.) -- названия не гарантированно стабильны для построения метрики,
а goal_id -- да.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger("growth_agent.connectors.metrika")


REPORTS_API_URL = "https://api-metrika.yandex.net/stat/v1/data"


class MetrikaConnectorError(Exception):
    """Сетевая ошибка, таймаут, или ошибка API (401/403/400/5xx)."""

    def __init__(self, message: str, is_integration_down: bool = True):
        super().__init__(message)
        self.is_integration_down = is_integration_down


class NotConfiguredError(Exception):
    """Источник не настроен (нет токена/counter_id/goal_ids) -- не ошибка."""
    pass


@dataclass
class MetrikaDiagnostics:
    """
    Диагностический payload, сохраняемый вместе с результатом -- для
    дебага и для будущего показа в /debug_alert. Не используется
    analyzer.py напрямую, но идёт в "_raw" секцию снэпшота.
    """

    status_code: Optional[int] = None
    data_lag: Optional[int] = None
    sampled: Optional[bool] = None
    sample_size: Optional[int] = None
    sample_share: Optional[float] = None
    requested_goal_ids: dict = field(default_factory=dict)
    total_rows: Optional[int] = None


def _build_metrics_param(goal_ids: dict) -> tuple[str, list]:
    """
    Строит строку metrics для запроса: visits, users + по одной метрике
    goalNNNreaches на каждую цель. Возвращает (metrics_param, ordered_keys),
    где ordered_keys -- нормализованные ключи в том же порядке, что и
    соответствующие колонки в ответе API (после visits, users).
    """
    base = ["ym:s:visits", "ym:s:users"]
    ordered_keys = list(goal_ids.keys())
    goal_metrics = [f"ym:s:goal{goal_id}reaches" for goal_id in goal_ids.values()]
    return ",".join(base + goal_metrics), ordered_keys


def _period_to_dates(period_hours: int) -> tuple:
    """
    Reports API принимает date1/date2 в формате YYYY-MM-DD (день, не час) --
    нет параметра "последние N часов". Для 3h/24h это даёт более грубую
    точность (весь сегодняшний день вместо последних 3 часов), это
    известное ограничение API, не connector'а. Для 7d -- естественно
    точнее, разница в гранулярности не критична.
    """
    now = datetime.now(timezone.utc)
    if period_hours <= 24:
        date1 = now.date().isoformat()
    else:
        date1 = (now - timedelta(hours=period_hours)).date().isoformat()
    date2 = now.date().isoformat()
    return date1, date2


def _parse_api_error(status_code: int, body) -> str:
    """
    Метрика возвращает ошибки как {"errors": [{"error_type": "...",
    "message": "..."}], "message": "..."} с разными HTTP-статусами,
    включая 400 для невалидных goal_id, не только 401/403. Парсим тело,
    чтобы дать понятный last_error, а не просто "HTTP 400".
    """
    if not body:
        return f"HTTP {status_code} (no response body)"

    message = body.get("message", "")
    errors = body.get("errors", [])
    if errors:
        details = "; ".join(
            f"{e.get('error_type', 'unknown')}: {e.get('message', '')}" for e in errors
        )
        return f"HTTP {status_code}: {details}" if not message else f"HTTP {status_code}: {message} ({details})"

    return f"HTTP {status_code}: {message}" if message else f"HTTP {status_code}"


async def fetch_metrics(
    oauth_token,
    counter_id,
    period_hours: int,
    goal_mapping: dict,
    goal_ids=None,
    timeout_seconds: float = 15.0,
) -> dict:
    """
    Возвращает dict с нормализованными ключами воронки (signup,
    activation_1, activation_2, payment_started, payment_success) плюс
    "traffic" (визиты) и "_diagnostics" (MetrikaDiagnostics как dict).

    goal_mapping оставлен в сигнатуре для совместимости вызова из
    scheduler.py, но фактически не используется для построения запроса --
    запрос строится через goal_ids (числовые ID целей), см. docstring
    модуля про то, почему названия целей не подходят для Reports API metrics.

    Бросает NotConfiguredError, если токен/counter_id/goal_ids отсутствуют.
    Бросает MetrikaConnectorError при сетевой ошибке/таймауте/API-ошибке.
    """
    if not oauth_token or not counter_id:
        raise NotConfiguredError("YANDEX_OAUTH_TOKEN or METRIKA_COUNTER_ID not set")

    goal_ids = goal_ids or {}
    if not goal_ids:
        raise NotConfiguredError("METRIKA_GOAL_IDS_JSON not set or empty")

    metrics_param, ordered_keys = _build_metrics_param(goal_ids)
    date1, date2 = _period_to_dates(period_hours)

    params = {
        "ids": counter_id,
        "metrics": metrics_param,
        "date1": date1,
        "date2": date2,
        "accuracy": "full",  # неагрегированная точность, чтобы sampled было реже True
    }
    headers = {"Authorization": f"OAuth {oauth_token}"}

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(REPORTS_API_URL, headers=headers, params=params)
    except httpx.TimeoutException as exc:
        raise MetrikaConnectorError(f"Timeout calling Metrika API: {exc}") from exc
    except httpx.HTTPError as exc:
        raise MetrikaConnectorError(f"HTTP error calling Metrika API: {exc}") from exc

    try:
        body = response.json()
    except ValueError:
        body = None

    if response.status_code != 200:
        error_message = _parse_api_error(response.status_code, body)
        raise MetrikaConnectorError(error_message)

    if body is None:
        raise MetrikaConnectorError("Metrika API returned 200 but body is not valid JSON")

    return _parse_success_response(body, ordered_keys, goal_ids, response.status_code)


def _parse_success_response(body: dict, ordered_keys: list, goal_ids: dict, status_code: int) -> dict:
    """
    Парсит успешный (200) ответ Reports API. "totals" -- массив сумм по
    каждой запрошенной метрике, в ТОМ ЖЕ порядке, что и metrics в запросе:
    [visits, users, goal_1_reaches, goal_2_reaches, ...].

    Отсутствие "totals" в ответе обрабатываем явно, чтобы не упасть с
    IndexError -- в этом случае считаем все метрики нулями (это не
    ошибка, а пустые данные).
    """
    totals = body.get("totals")

    sample_size = body.get("sample_size")
    sample_space = body.get("sample_space")
    sample_share = (sample_size / sample_space) if (sample_size and sample_space) else None

    diagnostics = MetrikaDiagnostics(
        status_code=status_code,
        data_lag=body.get("data_lag"),
        sampled=body.get("sampled"),
        sample_size=sample_size,
        sample_share=sample_share,
        requested_goal_ids=goal_ids,
        total_rows=body.get("total_rows"),
    )

    if not totals or not isinstance(totals, list) or len(totals) == 0:
        logger.info("Metrika returned empty totals -- treating as zero data, not an error")
        values = [0] * (2 + len(ordered_keys))
    else:
        row = totals[0] if isinstance(totals[0], list) else totals
        values = [v if v is not None else 0 for v in row]
        expected_len = 2 + len(ordered_keys)
        if len(values) < expected_len:
            logger.warning(
                "Metrika response has fewer columns (%d) than requested (%d) -- padding with zeros",
                len(values), expected_len,
            )
            values = values + [0] * (expected_len - len(values))

    traffic = values[0]
    goal_values = values[2:2 + len(ordered_keys)]

    normalized = {"traffic": traffic}
    for key, value in zip(ordered_keys, goal_values):
        normalized[key] = value

    normalized["_diagnostics"] = diagnostics.__dict__
    normalized["_users"] = values[1]

    if diagnostics.sampled:
        logger.warning(
            "Metrika response is sampled (sample_size=%s, sample_share=%s) -- numbers are estimates",
            diagnostics.sample_size, diagnostics.sample_share,
        )

    return normalized


# ---------------------------------------------------------------------------
# Debug-функция для проверки конфигурации без полного цикла scheduler.py
# ---------------------------------------------------------------------------


async def test_metrika_connection(oauth_token, counter_id, goal_ids=None) -> dict:
    """
    Минимальная проверка: токен валиден, counter_id существует, хотя бы
    один запрос проходит успешно. Возвращает dict с результатом, не
    бросает исключения наружу -- предназначена для вызова из Telegram-
    команды или CLI.
    """
    if not oauth_token or not counter_id:
        return {"ok": False, "error": "YANDEX_OAUTH_TOKEN or METRIKA_COUNTER_ID not set", "stage": "config"}

    if not goal_ids:
        return {"ok": False, "error": "METRIKA_GOAL_IDS_JSON not set or empty", "stage": "config"}

    try:
        result = await fetch_metrics(
            oauth_token=oauth_token,
            counter_id=counter_id,
            period_hours=24,
            goal_mapping={},
            goal_ids=goal_ids,
        )
    except NotConfiguredError as exc:
        return {"ok": False, "error": str(exc), "stage": "config"}
    except MetrikaConnectorError as exc:
        return {"ok": False, "error": str(exc), "stage": "api_call"}

    diagnostics = result.get("_diagnostics", {})
    return {
        "ok": True,
        "traffic": result.get("traffic"),
        "users": result.get("_users"),
        "goals_found": {k: result.get(k) for k in goal_ids.keys()},
        "sampled": diagnostics.get("sampled"),
        "data_lag": diagnostics.get("data_lag"),
    }
