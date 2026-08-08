"""
Настройки кампаний Яндекс.Директа: под какую цель и за какую цену
работает стратегия (задача R11).

Зачем. Владелец АвтоПоста поставил в стратегии цену конверсии 300 ₽ и
получил 65 регистраций с двумя оплатами. Директ отработал безупречно:
ему сказали «покупай регистрации по 300 ₽» — он и покупал. Убыток
возник не в кабинете, а в постановке задачи: цель оптимизации была на
шаге, который денег не приносит.

Пока платформа читала только статистику расхода, этот диагноз был
недоступен в принципе — цифры расхода одинаковы и при верной, и при
неверной цели. Метод `campaigns.get` показывает саму стратегию: её тип,
цель (`GoalId`) и целевую цену. Это read-only, ничего не меняется.

Отдельный файл, а не строчка в `direct.py`, по той же причине, что и
`direct_write.py`: `direct.py` — про отчёты Reports Service (TSV,
очередь, retry), а здесь обычный JSON-метод API кампаний с другим
URL и другим форматом ответа.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger("growth_agent.connectors.direct_campaigns")

CAMPAIGNS_API_URL = "https://api.direct.yandex.com/json/v5/campaigns"
SANDBOX_API_URL = "https://api-sandbox.direct.yandex.com/json/v5/campaigns"


class DirectCampaignsError(Exception):
    pass


class NotConfiguredError(Exception):
    pass


# Стратегии, которые оптимизируются под конкретную цель Метрики. Только у
# них имеет смысл спрашивать «а та ли это цель» -- у ручных ставок и у
# стратегий на клики цели нет вовсе, и предупреждение было бы ложным.
GOAL_DRIVEN_STRATEGIES = frozenset({
    "AVERAGE_CPA",
    "AVERAGE_CPA_MULTIPLE_GOALS",
    "PAY_FOR_CONVERSION",
    "PAY_FOR_CONVERSION_MULTIPLE_GOALS",
    "AVERAGE_ROI",
    "AVERAGE_CRR",
    "PAY_FOR_CONVERSION_CRR",
    "WB_MAXIMUM_CONVERSION_RATE",
})


def _extract_strategy(campaign: dict) -> dict:
    """
    Достаёт стратегию из кампании. Директ кладёт её в разные ветки в
    зависимости от типа кампании (TextCampaign, UnifiedCampaign и т.д.),
    поэтому перебираем известные контейнеры, а не жёстко один путь: при
    появлении нового типа кампании функция вернёт «стратегию не видно»,
    а не упадёт и не соврёт.

    Внутри стратегии цена цели тоже лежит по-разному: у AVERAGE_CPA это
    AverageCpa, у PAY_FOR_CONVERSION -- Cpa. Собираем оба.
    """
    for container_key in ("TextCampaign", "UnifiedCampaign", "DynamicTextCampaign",
                          "SmartCampaign", "MobileAppCampaign"):
        container = campaign.get(container_key)
        if not isinstance(container, dict):
            continue
        bidding = container.get("BiddingStrategy") or {}
        search = bidding.get("Search") or {}
        network = bidding.get("Network") or {}
        # Цель задаётся на поиске; сеть обычно наследует. Берём поиск, а
        # если там ручная стратегия -- смотрим сеть.
        for part in (search, network):
            strategy_type = part.get("BiddingStrategyType")
            if not strategy_type:
                continue
            payload = {}
            for key in ("AverageCpa", "PayForConversion", "AverageRoi", "AverageCrr",
                        "WbMaximumConversionRate"):
                if isinstance(part.get(key), dict):
                    payload = part[key]
                    break
            goal_id = payload.get("GoalId")
            target_price = payload.get("AverageCpa") or payload.get("Cpa")
            if strategy_type in GOAL_DRIVEN_STRATEGIES or goal_id:
                return {
                    "strategy_type": strategy_type,
                    "goal_id": str(goal_id) if goal_id is not None else None,
                    # Цены в API Директа приходят в микро-единицах валюты,
                    # как и Cost в отчётах (см. direct.py) -- иначе 300 ₽
                    # показались бы владельцу как 300 000 000.
                    "target_price": (float(target_price) / 1_000_000
                                     if target_price is not None else None),
                    "optimizes_for_goal": strategy_type in GOAL_DRIVEN_STRATEGIES,
                }
        return {"strategy_type": search.get("BiddingStrategyType")
                or network.get("BiddingStrategyType"),
                "goal_id": None, "target_price": None, "optimizes_for_goal": False}
    return {"strategy_type": None, "goal_id": None, "target_price": None,
            "optimizes_for_goal": False}


async def fetch_campaign_strategies(
    oauth_token: Optional[str],
    client_login: Optional[str],
    campaign_ids: Optional[list] = None,
    sandbox: bool = False,
    timeout_seconds: float = 20.0,
) -> list:
    """
    Возвращает список кампаний: [{"campaign_id", "name", "strategy_type",
    "goal_id", "target_price", "optimizes_for_goal"}, ...].

    Только чтение. Бросает NotConfiguredError, если нет токена/логина;
    DirectCampaignsError при сетевой ошибке или ошибке API.
    """
    if not oauth_token or not client_login:
        raise NotConfiguredError("DIRECT_OAUTH_TOKEN or DIRECT_CLIENT_LOGIN not set")

    selection: dict = {}
    if campaign_ids:
        selection["Ids"] = [int(c) for c in campaign_ids]
    else:
        # Без фильтра Директ требует явно указать, какие кампании нужны.
        # Берём все, кроме архивных: архивная кампания денег не тратит, а
        # в выводе создавала бы шум.
        selection["States"] = ["ON", "OFF", "SUSPENDED", "ENDED"]

    body = {
        "method": "get",
        "params": {
            "SelectionCriteria": selection,
            "FieldNames": ["Id", "Name", "State", "Status"],
            "TextCampaignFieldNames": ["BiddingStrategy"],
        },
    }
    headers = {
        "Authorization": f"Bearer {oauth_token}",
        "Accept-Language": "ru",
        "Client-Login": client_login,
    }

    url = SANDBOX_API_URL if sandbox else CAMPAIGNS_API_URL
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise DirectCampaignsError(f"Не удалось запросить кампании Директа: {exc}") from exc

    if response.status_code != 200:
        raise DirectCampaignsError(f"HTTP {response.status_code} от API кампаний Директа")

    try:
        payload = response.json()
    except ValueError as exc:
        raise DirectCampaignsError("API кампаний Директа вернул не JSON") from exc

    if "error" in payload:
        error = payload["error"]
        raise DirectCampaignsError(
            f"{error.get('error_string', 'ошибка')}: {error.get('error_detail', '')}".strip(": ")
        )

    campaigns = (payload.get("result") or {}).get("Campaigns") or []
    out = []
    for campaign in campaigns:
        out.append({
            "campaign_id": str(campaign.get("Id", "")),
            "name": campaign.get("Name", ""),
            "state": campaign.get("State", ""),
            **_extract_strategy(campaign),
        })
    return out
