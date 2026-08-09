"""
Обратимость действий агента (задача A1).

Зачем это первое, а не последнее. Владелец хочет, чтобы аналитик сам
менял настройки, проверял результат и «утверждал или откатывал». Первые
три шага были: агент умеет писать в Метрику и Директ, Growth Loop умеет
выносить вердикт. Четвёртого не было вовсе — вердикт ПИСАЛ словами
«откатить изменение», и на этом всё заканчивалось. `payload_json`
исправно хранил `before`, и этот `before` не читал никто и никогда.

Пока откат не гарантирован, автономия — храповик в одну сторону: агент
может только добавлять изменения, и чем дольше он работает, тем больше
накапливается правок, о которых никто не помнит, зачем они. Поэтому
обратимость — не удобство, а условие, при котором остальную автономию
вообще можно включать.

Устройство. Реестр: домен действия -> функция отмены. Отменяются ТОЛЬКО
те действия, которые агент сам записал в журнал (`AgentAction`): чужие
правки — решения владельца, и агент не вправе их трогать.

Отдельно здесь живёт классификация действий по цене ошибки. Она нужна
не для красоты: «добавить минус-фразу» и «поднять бюджет» — принципиально
разные поступки, и одинаковая ручка автономии для них была бы обманом.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("growth_agent.reversible")


@dataclass(frozen=True)
class Reversibility:
    """
    Насколько дорого ошибиться этим действием и насколько легко вернуть.

    `cost_of_error` -- сколько стоит неверное решение: "низкая" (правка
    видна и отменяется мгновенно), "средняя" (влияет на показы/данные),
    "высокая" (тратит деньги или меняет то, что видит клиент).
    `auto_revertible` -- можно ли откатить программно, без человека.
    """

    cost_of_error: str
    auto_revertible: bool
    explanation: str


REVERSIBILITY: dict[str, Reversibility] = {
    "direct_negative_keywords": Reversibility(
        cost_of_error="низкая", auto_revertible=True,
        explanation="Минус-фраза убирается обратно за секунду, деньги она "
                    "только экономит — ошибиться здесь дёшево.",
    ),
    "metrika_goal": Reversibility(
        cost_of_error="средняя", auto_revertible=True,
        explanation="Цель можно удалить, но собранная по ней статистика "
                    "за время работы не восстановится.",
    ),
    "direct_bid": Reversibility(
        cost_of_error="высокая", auto_revertible=True,
        explanation="Ставка тратит деньги немедленно. Вернуть прежнее "
                    "значение можно, потраченное — нет.",
    ),
    "product": Reversibility(
        cost_of_error="высокая", auto_revertible=False,
        explanation="Правки в самом продукте вносит человек — у платформы "
                    "нет доступа на запись в чужой код.",
    ),
    "pricing": Reversibility(
        cost_of_error="высокая", auto_revertible=False,
        explanation="Цену видят клиенты. Откат не отменяет того, что её "
                    "уже увидели.",
    ),
}


def reversibility_of(domain: str) -> Reversibility:
    """Неизвестный домен считаем необратимым и дорогим: осторожность по
    умолчанию безопаснее, чем оптимизм по умолчанию."""
    return REVERSIBILITY.get(domain, Reversibility(
        cost_of_error="высокая", auto_revertible=False,
        explanation="Неизвестный тип действия — автоматически откатывать не берёмся.",
    ))


@dataclass
class RevertResult:
    ok: bool
    message: str            # человеку, по-русски
    details: dict | None = None


# Реестр: домен -> как отменить. Функция получает (payload, settings,
# project) и возвращает RevertResult.
_UNDOERS: dict[str, Callable[..., Awaitable[RevertResult]]] = {}


def undoer(domain: str):
    def wrap(fn):
        _UNDOERS[domain] = fn
        return fn
    return wrap


@undoer("direct_negative_keywords")
async def _undo_negative_keywords(payload: dict, settings, project) -> RevertResult:
    from app.connectors import direct_write

    after = (payload or {}).get("after") or {}
    before = (payload or {}).get("before") or {}
    ad_group_id = before.get("ad_group_id")
    # Убираем именно то, что реально применилось, а не то, что предлагали:
    # часть фраз Директ мог отклонить, и «откатывать» их нечего.
    phrases = after.get("applied") or after.get("phrases") or []

    if not ad_group_id:
        return RevertResult(False, "Не знаю, из какой группы убирать фразы — откат вручную.")
    if not phrases:
        return RevertResult(False, "В журнале не записано ни одной применённой фразы.")
    if not direct_write.is_configured(settings):
        return RevertResult(False, "Не настроен токен записи в Директ — откат вручную.")

    try:
        res = await direct_write.remove_negative_keywords(settings, str(ad_group_id), list(phrases))
    except Exception as exc:  # noqa: BLE001 -- наружу отдаём текст, не трассировку
        return RevertResult(False, f"Директ не принял откат: {exc}")

    if res.applied:
        return RevertResult(True, f"Убрал {len(res.applied)} минус-фраз обратно.",
                            {"removed": res.applied})
    if res.warnings:
        return RevertResult(True, "; ".join(res.warnings))
    return RevertResult(False, "Откат не применился, Директ ничего не изменил.")


@undoer("metrika_goal")
async def _undo_metrika_goal(payload: dict, settings, project) -> RevertResult:
    from app.connectors import metrika_write

    after = (payload or {}).get("after") or {}
    goal_id = (after or {}).get("id") or (after or {}).get("goal_id")
    if not goal_id:
        return RevertResult(False, "В журнале нет номера созданной цели — удалите её вручную.")
    if not metrika_write.is_configured(project):
        return RevertResult(False, "Не настроен токен записи в Метрику — откат вручную.")
    try:
        await metrika_write.delete_goal(project, int(goal_id))
    except Exception as exc:  # noqa: BLE001
        return RevertResult(False, f"Метрика не приняла удаление цели: {exc}")
    return RevertResult(True, f"Удалил цель №{goal_id}, созданную агентом.")


async def revert_action(session, action, settings, project) -> RevertResult:
    """
    Отменяет одно действие агента и записывает это в его же журнал.

    Идемпотентность важнее аккуратности формулировок: повторный откат не
    должен пытаться отменить уже отменённое (иначе автоматический откат
    после проигравшей проверки, запущенный дважды, наделает бед).
    """
    from app.models import AgentActionStatus, utcnow

    if action.status == AgentActionStatus.reverted.value:
        return RevertResult(True, "Уже откачено раньше.")
    if action.status != AgentActionStatus.applied.value:
        return RevertResult(False, "Откатывать нечего: действие не применялось.")

    rev = reversibility_of(action.domain)
    if not rev.auto_revertible:
        return RevertResult(False, rev.explanation)

    undo = _UNDOERS.get(action.domain)
    if undo is None:
        return RevertResult(False, "Для этого типа действия отката пока нет.")

    result = await undo(action.payload_json or {}, settings, project)
    if result.ok:
        action.status = AgentActionStatus.reverted.value
        action.reasoning = f"{action.reasoning} Откат: {result.message}"
        # Время отката -- в payload_json, а не отдельной колонкой: таблица
        # уже живёт на проде, а create_all() новых колонок не добавляет.
        payload = dict(action.payload_json or {})
        payload["revert"] = {"at": utcnow().isoformat(), "message": result.message}
        action.payload_json = payload
        session.add(action)
        session.commit()
    return result


def revertible_actions(session, project_id: int, recommendation_id: int) -> list:
    """
    Что агент сделал ради этой рекомендации и может отменить.

    Связь идёт через `related_recommendation_id`: именно она отвечает на
    вопрос «какие правки относятся к этой проверке» — без неё откат после
    проигравшего эксперимента отменял бы всё подряд.
    """
    from sqlmodel import select

    from app.models import AgentAction, AgentActionStatus

    return list(session.exec(
        select(AgentAction)
        .where(AgentAction.project_id == project_id)
        .where(AgentAction.related_recommendation_id == recommendation_id)
        .where(AgentAction.status == AgentActionStatus.applied)
    ).all())
