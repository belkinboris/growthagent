"""
Действия Маркетолога: цели Метрики и минус-фразы Директа (задачи F6, R4).

Два сценария:

1. **Цель payment_success не срабатывает** (правило
   `payments_invisible_in_metrika`): оплата видна в продукте, но не видна
   в Метрике. Маркетолог предлагает пересоздать цель, а на уровне
   автономии 3 создаёт её сам -- если владелец заранее задал и токен
   записи, и условие цели. Без условия агенту неоткуда взять URL/событие
   успешной оплаты, а придумывать его значило бы писать в чужой кабинет
   наугад.

2. **Мусорные поисковые запросы** (`classify_search_queries` пометил их
   `safe_negative`): деньги уходят на запросы, которые заведомо не про
   продукт. На уровне 2 и 3 Маркетолог добавляет их в минус-фразы группы
   сам -- это обратимая правка с малой ценой ошибки, ровно то, что
   владелец описал как «мелкое, которое гендир решает сам». На уровне 1 --
   только предложение.

Общее правило для обоих: агент НИКОГДА не действует без явно настроенной
записи и всегда оставляет след в `AgentAction` -- что сделал, почему и
что было до. Не смог -- честный статус «не настроено», а не тихий no-op.

Вызывается из scheduler.py сразу после того, как цикл сбора создал/обновил
алерт -- единственное место, где известны и метрики, и алерт, и настройки
проекта разом.
"""
from __future__ import annotations

import logging

from sqlmodel import select

from app.connectors import direct_write, metrika_write
from app.models import AgentAction, AgentActionStatus, Alert, AlertCategory, utcnow

logger = logging.getLogger("growth_agent.marketer_actions")

PAYMENT_GOAL_NAME = "payment_success (авто-восстановлено Маркетологом)"

# Минимальный уровень автономии, с которого Маркетолог сам добавляет
# минус-фразы. Два, а не три: правка обратима (фразу можно убрать),
# дёшева по цене ошибки и не трогает ни ставки, ни бюджеты.
MIN_LEVEL_FOR_NEGATIVE_KEYWORDS = 2

# За один проход добавляем ограниченное число фраз: если классификатор
# вдруг ошибётся оптом, человек увидит это на десятке строк, а не на сотне
# вырезанных запросов.
MAX_NEGATIVES_PER_RUN = 10


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


def _open_negative_phrases(session, project_id: int) -> set[str]:
    """Фразы, по которым уже есть предложение или применение -- чтобы не
    предлагать одно и то же каждый цикл сбора."""
    rows = session.exec(
        select(AgentAction).where(
            AgentAction.project_id == project_id,
            AgentAction.domain == "direct_negative_keywords",
        )
    ).all()
    seen: set[str] = set()
    for row in rows:
        for p in ((row.payload_json or {}).get("after") or {}).get("phrases", []):
            seen.add(str(p).strip().lower())
    return seen


async def handle_safe_negatives(session, project, intelligence: dict | None, level: int) -> list[AgentAction]:
    """
    Мусорные поисковые запросы -> минус-фразы в Директе.

    `intelligence` -- результат `classify_search_queries` из кэша
    (`direct_intelligence_24h`). Берём только `safe_negatives`: у них уже
    пройдены все защиты классификатора (безусловный мусор по семантике,
    защищённые термины, пороги по кликам и расходу).

    Уровень 1 -- только предложение. Уровень 2 и выше -- Маркетолог
    добавляет фразы сам, потому что правка обратима и дёшева по цене
    ошибки. Группируем по группе объявлений: `adgroups.update` пишет
    список целиком, и один вызов на группу вместо одного на фразу и
    безопаснее, и экономит баллы API.
    """
    from app.config import get_settings

    if not intelligence:
        return []
    safe = intelligence.get("safe_negatives") or []
    if not safe:
        return []

    already = _open_negative_phrases(session, project.id)

    # Группируем по ad_group_id: без него записывать некуда -- такие фразы
    # можем только предложить человеку.
    by_group: dict[str | None, list[dict]] = {}
    for item in safe:
        phrase = (item.get("query") or "").strip()
        if not phrase or phrase.lower() in already:
            continue
        by_group.setdefault(item.get("ad_group_id") or None, []).append(item)

    settings = get_settings()
    actions: list[AgentAction] = []

    for ad_group_id, items in by_group.items():
        items = items[:MAX_NEGATIVES_PER_RUN]
        phrases = [i["query"].strip() for i in items]
        reasons = {i["query"].strip(): (i.get("reason") or "мусорный запрос") for i in items}
        wasted = sum(float(i.get("cost") or 0) for i in items)
        group_label = items[0].get("ad_group_name") or (f"группа {ad_group_id}" if ad_group_id else "неизвестная группа")

        reasoning = (
            f"{len(phrases)} поисковых запросов не про продукт "
            f"(потрачено {wasted:.0f} ₽) в группе «{group_label}». "
            "Предлагаю добавить их в минус-фразы: "
            + ", ".join(f"«{p}» — {reasons[p]}" for p in phrases[:5])
            + ("…" if len(phrases) > 5 else "")
        )

        action = AgentAction(
            project_id=project.id, agent="marketer", domain="direct_negative_keywords",
            action="add_negative_keywords", reasoning=reasoning,
            payload_json={
                "before": {"ad_group_id": ad_group_id, "ad_group_name": group_label},
                "after": {"phrases": phrases, "wasted_rub": round(wasted)},
            },
            status=AgentActionStatus.proposed.value,
            autonomy_level_at_time=level,
        )

        if level >= MIN_LEVEL_FOR_NEGATIVE_KEYWORDS:
            if not ad_group_id:
                action.status = AgentActionStatus.blocked_not_configured.value
                action.reasoning += (
                    " Применить сам не могу: Директ не вернул номер группы объявлений "
                    "для этих запросов — добавьте фразы руками."
                )
            elif not direct_write.is_configured(settings):
                action.status = AgentActionStatus.blocked_not_configured.value
                action.reasoning += (
                    " Применить сам не могу: не настроен токен записи в Директ "
                    "(DIRECT_WRITE_OAUTH_TOKEN или YANDEX_OAUTH_TOKEN)."
                )
            else:
                try:
                    res = await direct_write.add_negative_keywords(settings, ad_group_id, phrases)
                    if res.applied:
                        action.status = AgentActionStatus.applied.value
                        action.applied_at = utcnow()
                        action.payload_json["after"]["applied"] = res.applied
                        action.reasoning += f" Применил сам: добавил {len(res.applied)} фраз в минус-слова."
                    if res.skipped:
                        # «Применили 3 из 5» -- штатный ответ Директа, и
                        # показать надо именно это, а не «готово».
                        action.payload_json["after"]["skipped"] = res.as_dict()["skipped"]
                        action.reasoning += (
                            f" Директ отклонил {len(res.skipped)}: "
                            + "; ".join(f"«{i}» — {r}" for i, r in res.skipped[:3])
                        )
                    if not res.applied and not res.skipped:
                        action.status = AgentActionStatus.applied.value
                        action.applied_at = utcnow()
                        action.reasoning += " Ничего добавлять не потребовалось: фразы уже стоят."
                except direct_write.DirectWriteError as exc:
                    action.status = AgentActionStatus.blocked_not_configured.value
                    action.reasoning += f" Запись в Директ не удалась: {exc}"
                    logger.warning("marketer_actions: negatives failed (project=%s): %s", project.id, exc)

        session.add(action)
        actions.append(action)

    if actions:
        session.commit()
        for a in actions:
            session.refresh(a)
    return actions
