"""
Connector для Яндекс.Метрики.

В этой версии -- минимальная реализация: если токен/счётчик не настроены,
функция явно говорит об этом через NotConfiguredError (отличается от
MetrikaConnectorError -- сетевой/API ошибки). scheduler.py обрабатывает
NotConfiguredError как "источник не подключён", а не как integration_down --
это разные ситуации (см. CONTRACT.md: not_configured не равно error).

Реальный вызов Яндекс.Метрика Reporting API будет добавлен здесь же,
интерфейс fetch_metrics() не должен измениться для вызывающего кода.
"""

from datetime import datetime, timezone
from typing import Optional

import httpx


class MetrikaConnectorError(Exception):
    pass


class NotConfiguredError(Exception):
    """Источник не настроен (нет токена/counter_id) -- не ошибка, а отсутствие интеграции."""
    pass


METRIKA_API_BASE = "https://api-metrika.yandex.net/stat/v1/data"


async def fetch_metrics(
    oauth_token: Optional[str],
    counter_id: Optional[str],
    period_hours: int,
    goal_mapping: dict,
    timeout_seconds: float = 15.0,
) -> dict:
    """
    Возвращает dict с нормализованными ключами воронки, заполненными ИЗ
    целей Метрики (используется для metrics_discrepancy -- сравнения с
    данными продукта), плюс traffic-метрики (визиты).

    Бросает NotConfiguredError, если токен/counter_id отсутствуют.
    Бросает MetrikaConnectorError при сетевой ошибке/таймауте/невалидном ответе.
    """
    if not oauth_token or not counter_id:
        raise NotConfiguredError("YANDEX_OAUTH_TOKEN or METRIKA_COUNTER_ID not set")

    # TODO: реальный вызов Яндекс.Метрика Reporting API.
    # Текущая реализация -- интерфейсный stub, чтобы scheduler.py и тесты
    # могли работать end-to-end до подключения настоящего API-ключа.
    raise MetrikaConnectorError(
        "Metrika API client not implemented yet -- this is a stub for scheduler.py integration"
    )
