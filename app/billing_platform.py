"""
Оплата тарифа платформы через ЮKassa (задача E1).

Не путать с `app/connectors/yookassa.py` — тот читает данные об оплатах
АНАЛИЗИРУЕМОГО продукта (сколько заплатили клиенты АвтоПоста), это чужие
деньги, только чтение. Здесь — оплата САМОЙ платформы: владелец аналитика
платит нам за тариф. Разные магазины ЮKassa, разные ключи
(`PLATFORM_YOOKASSA_*`, не `YOOKASSA_*`) — перепутать их означало бы
слать счета клиента продавцу вместо себя.

Поток тот же, что в АвтоПосте (см. billing.py там): создаём платёж,
отдаём confirmation_url, ждём вебхук, перед начислением тарифа повторно
спрашиваем статус у самой ЮKassa (вебхук можно подделать, ответ API — нет).
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx

logger = logging.getLogger(__name__)

YOOKASSA_PAYMENTS_URL = "https://api.yookassa.ru/v3/payments"


class YooKassaError(RuntimeError):
    pass


def is_configured(settings) -> bool:
    return bool(
        getattr(settings, "platform_yookassa_shop_id", None)
        and getattr(settings, "platform_yookassa_secret_key", None)
    )


def _auth(settings) -> tuple[str, str]:
    return (settings.platform_yookassa_shop_id, settings.platform_yookassa_secret_key)


def _amount(value: float) -> dict[str, str]:
    return {"value": f"{float(value):.2f}", "currency": "RUB"}


async def create_checkout(settings, *, user_id: int, plan: str, price_rub: float, description: str) -> dict[str, Any]:
    """Создаёт платёж в ЮKassa, возвращает сырой ответ (нужен confirmation_url и id)."""
    if not is_configured(settings):
        raise YooKassaError("Оплата тарифов не настроена: задайте PLATFORM_YOOKASSA_SHOP_ID и PLATFORM_YOOKASSA_SECRET_KEY")

    payload = {
        "amount": _amount(price_rub),
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": getattr(settings, "platform_yookassa_return_url", None) or "https://analitik.projectsozdatel.ru/growth/",
        },
        "description": description[:128],
        "metadata": {"user_id": str(user_id), "plan": plan},
    }
    headers = {"Idempotence-Key": str(uuid.uuid4()), "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await client.post(YOOKASSA_PAYMENTS_URL, auth=_auth(settings), headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise YooKassaError(f"Не удалось обратиться к ЮKassa: {exc}") from exc

    if resp.status_code >= 400:
        logger.warning("platform billing: create_checkout error %s: %s", resp.status_code, resp.text[:300])
        raise YooKassaError(_extract_error(resp))

    data = resp.json()
    if not (data.get("confirmation") or {}).get("confirmation_url"):
        raise YooKassaError("ЮKassa не вернула ссылку на оплату")
    return data


async def get_payment(settings, payment_id: str) -> dict[str, Any]:
    """Актуальный статус платежа -- источник правды перед начислением тарифа,
    вебхуку самому по себе доверять нельзя (его можно подделать)."""
    if not is_configured(settings):
        raise YooKassaError("Оплата тарифов не настроена")
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await client.get(f"{YOOKASSA_PAYMENTS_URL}/{payment_id}", auth=_auth(settings))
        except httpx.HTTPError as exc:
            raise YooKassaError(f"Не удалось проверить платёж в ЮKassa: {exc}") from exc
    if resp.status_code >= 400:
        raise YooKassaError(_extract_error(resp))
    return resp.json()


def _extract_error(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except Exception:
        return f"Ошибка ЮKassa: HTTP {resp.status_code}"
    msg = data.get("description") or data.get("code")
    return f"Ошибка ЮKassa: {msg}" if msg else f"Ошибка ЮKassa: HTTP {resp.status_code}"
