"""
Connector для YooKassa (read-only). Stub до подключения реального API.
"""

from typing import Optional


class YooKassaConnectorError(Exception):
    pass


class NotConfiguredError(Exception):
    pass


async def fetch_metrics(
    shop_id: Optional[str],
    secret_key: Optional[str],
    period_hours: int,
    timeout_seconds: float = 15.0,
) -> dict:
    if not shop_id or not secret_key:
        raise NotConfiguredError("YOOKASSA_SHOP_ID or YOOKASSA_SECRET_KEY not set")

    # TODO: реальный вызов YooKassa API (список платежей за период).
    raise YooKassaConnectorError(
        "YooKassa API client not implemented yet -- this is a stub for scheduler.py integration"
    )
