"""
Connector для Яндекс.Директа.

Аналогично metrika.py -- интерфейсный stub. NotConfiguredError если
client_login/campaign_ids не заданы, DirectConnectorError при реальных
сбоях API. Реальный вызов Яндекс.Директ API (campaigns/get, reports)
добавляется позже без изменения сигнатуры fetch_metrics().
"""

from typing import Optional


class DirectConnectorError(Exception):
    pass


class NotConfiguredError(Exception):
    pass


async def fetch_metrics(
    oauth_token: Optional[str],
    client_login: Optional[str],
    campaign_ids: list[str],
    period_hours: int,
    timeout_seconds: float = 15.0,
) -> dict:
    """
    Возвращает dict с traffic-метриками: spend, clicks, impressions, ctr.
    Не возвращает as_of явно -- Директ Reports API обычно агрегирует по
    дням, поэтому as_of вычисляется в scheduler.py как конец последнего
    полного дня в отчёте, не здесь.
    """
    if not oauth_token or not client_login or not campaign_ids:
        raise NotConfiguredError("YANDEX_OAUTH_TOKEN, DIRECT_CLIENT_LOGIN or DIRECT_CAMPAIGN_IDS not set")

    # TODO: реальный вызов Яндекс.Директ Reports API.
    raise DirectConnectorError(
        "Direct API client not implemented yet -- this is a stub for scheduler.py integration"
    )
