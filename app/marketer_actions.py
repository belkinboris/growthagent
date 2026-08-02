"""
Действия Маркетолога по целям Метрики (задача F6).

Единственный сценарий пока: "оплата видна в продукте, но не видна в
Метрике" (правило `payments_invisible_in_metrika` в rules.py). Маркетолог
всегда ПРЕДЛАГАЕТ починку -- независимо от уровня автономии, владелец сам
попросил именно так ("хотя бы предлагать"). Реально ЗАПИСАТЬ цель в
Метрику агент пытается только на уровне автономии 3, и только если у
проекта настроен и токен на запись (`metrika_management_token`), и заранее
заданное владельцем условие цели (`metrika_payment_success_goal_condition`)
-- без условия агенту неоткуда взять URL/событие успешной оплаты, а
придумывать его значило бы записать в чужой рекламный кабинет наугад,
что прямо запрещено принципом "честность данных".

Вызывается из scheduler.py сразу после того, как цикл сбора создал/обновил
алерт -- единственное место, где известны и метрики, и алерт, и настройки
проекта разом.
"""
from __future__ import annotations

import logging

from sqlmodel import select

from app.connectors import metrika_write
from app.models import AgentAction, AgentActionStatus, Alert, AlertCategory, utcnow

logger = logging.getLogger("growth_agent.marketer_actions")

PAYMENT_GOAL_NAME = "payment_success (авто-восстановлено Маркетологом)"


def _has_open_action_for_alert(session, alert_id: int) -> bool:
    existing = session.exec(
        select(AgentAction).where(
            AgentAction.related_alert_id == alert_id,
            AgentAction.status.in_([
                AgentActionStatus.proposed.value,
                AgentActionStatus.applied.value,
            ]),
        )
    ).first()
    return existing is not None


async def handle_payment_visibility_alert(session, project, alert: Alert, level: int) -> AgentAction | None:
    """Создаёт (или, на уровне 3, ещё и применяет) предложение Маркетолога
    по алерту `payments_invisible_in_metrika`. Не делает ничего, если по
    этому алерту уже есть открытое предложение -- иначе на каждый цикл
    сбора плодилась бы новая карточка на то же самое."""
    if alert.category != AlertCategory.payments_invisible_in_metrika:
        return None
    if _has_open_action_for_alert(session, alert.id):
        return None

    payment_success = (alert.payload_json or {}).get("payment_success", 0)
    reasoning = (
        f"Продукт зафиксировал {payment_success} успешных оплат, а цель "
        "payment_success в Метрике не сработала ни разу. Похоже, цель "
        "отвязана от события/кнопки оплаты. Предлагаю пересоздать цель "
        "payment_success в Метрике."
    )

    action = AgentAction(
        project_id=project.id, agent="marketer", domain="metrika_goal",
        action="recreate_goal", reasoning=reasoning,
        payload_json={"before": {"payment_success_reaches": 0}, "after": None},
        status=AgentActionStatus.proposed.value,
        autonomy_level_at_time=level,
        related_alert_id=alert.id,
    )

    if level >= 3:
        from app.config import get_settings

        condition = (project.settings_json or {}).get(
            "metrika_payment_success_goal_condition"
        ) or get_settings().metrika_payment_success_goal_condition
        if not metrika_write.is_configured(project) or not condition:
            action.status = AgentActionStatus.blocked_not_configured.value
            missing = []
            if not metrika_write.is_configured(project):
                missing.append("токен записи Метрики (METRIKA_MANAGEMENT_TOKEN в окружении "
                                "или metrika_management_token у проекта)")
            if not condition:
                missing.append("условие цели «оплата успешна» "
                                "(METRIKA_PAYMENT_SUCCESS_GOAL_CONDITION_JSON в окружении "
                                "или metrika_payment_success_goal_condition у проекта)")
            action.reasoning += (
                " Уровень автономии 3 включён, но записать цель сам не могу: "
                "не настроено " + ", ".join(missing) + "."
            )
        else:
            try:
                goal = await metrika_write.create_goal(
                    project, name=PAYMENT_GOAL_NAME, goal_type=condition.get("type", "url"),
                    conditions=condition.get("conditions", []),
                )
                action.status = AgentActionStatus.applied.value
                action.payload_json = {"before": {"payment_success_reaches": 0}, "after": goal}
                action.applied_at = utcnow()
                action.reasoning += " Применил сам: пересоздал цель payment_success в Метрике."
            except metrika_write.MetrikaWriteError as exc:
                action.status = AgentActionStatus.blocked_not_configured.value
                action.reasoning += f" Уровень автономии 3 включён, но запись не удалась: {exc}"
                logger.warning("marketer_actions: create_goal failed for project %s: %s", project.id, exc)

    session.add(action)
    session.commit()
    session.refresh(action)
    return action
