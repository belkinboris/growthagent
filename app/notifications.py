"""
Live notifications о ключевых шагах пути пользователя в TruePost.

Архитектурное ограничение (важно для понимания v0-реализации):
TruePost отдаёт Growth Agent только АГРЕГАТЫ через
/api/internal/payment-path-diagnostics (registrations, channels_created,
first_post_feedback_good/bad, pricing_viewed, payment_started/success...).
Individual ProductEvent (конкретный user_id, конкретное действие, точное
время) сейчас НЕ передаётся.

Поэтому в v0 уведомления строятся на ДЕЛЬТАХ агрегатов между двумя
последовательными /run-циклами: "pricing_viewed выросло с 3 до 4 -- значит
кто-то открыл тарифы один раз, отправим один обобщённый сигнал". Это не
даёт per-user детализацию (источник, путь конкретного пользователя), но
даёт частоту и направление сигнала без необходимости менять TruePost API.

Когда TruePost добавит per-event endpoint (см. задачу: future task --
first_post_shown/first_post_ready, в более широком виде -- individual
event feed), этот модуль можно расширить до true per-user notifications
с реальным source/путём, как в примерах A-E из задачи. Текущая реализация
сделана так, чтобы переход был плавным: event_key и формат уведомления
уже рассчитаны на то, что user_id может появиться.

Дедупликация: NotificationLog хранит event_key. Для агрегатных дельт v0
event_key строится детерминированно из (event_type, project_id, текущее
значение счётчика) -- так что даже если /run запустится дважды подряд
без изменений, повторное уведомление не уйдёт.

Anti-spam guardrail: если регистраций за последний час > DIGEST_THRESHOLD,
отдельные уведомления о регистрациях объединяются в дайджест.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Anti-spam guardrail
# ---------------------------------------------------------------------------

# Если новых регистраций за последний цикл сравнения <= этого порога --
# уведомляем по каждой персонально (точнее, по каждому шагу-дельте).
# Если больше -- группируем в дайджест.
DIGEST_THRESHOLD_PER_RUN = 20


@dataclass
class StepDelta:
    """Одна обнаруженная дельта между двумя снимками агрегатов."""
    event_type: str          # "user_registered" | "channel_created" | ...
    delta: int                # на сколько выросло значение
    current_value: int        # текущее (новое) значение счётчика
    previous_value: int       # предыдущее значение


@dataclass
class NotificationBatch:
    """Результат сравнения снимков: список дельт + решение спамить или нет."""
    deltas: list[StepDelta] = field(default_factory=list)
    is_digest: bool = False
    digest_text: Optional[str] = None


# ---------------------------------------------------------------------------
# Вычисление дельт
# ---------------------------------------------------------------------------

# Маппинг: поле в payment_path dict -> event_type
_TRACKED_FIELDS: dict[str, str] = {
    "registrations": "user_registered",
    "channels_created": "channel_created",
    "first_post_feedback_good": "first_post_feedback_good",
    "first_post_feedback_bad": "first_post_feedback_bad",
    "pricing_viewed": "pricing_viewed",
    "payment_started": "payment_started",
    "payment_success": "payment_success",
    "payment_failed": "payment_failed",
}


def compute_deltas(previous: dict | None, current: dict | None) -> list[StepDelta]:
    """
    Сравнивает два снимка payment_path agregates, возвращает список дельт
    (только положительные изменения -- нас интересует прирост, не падение).

    Если previous is None (первый запуск) -- дельты не считаются, чтобы не
    выгрузить разом всю историю как "новые" события.
    """
    if previous is None or current is None:
        return []

    deltas: list[StepDelta] = []
    for field_name, event_type in _TRACKED_FIELDS.items():
        prev_val = previous.get(field_name)
        cur_val = current.get(field_name)
        if prev_val is None or cur_val is None:
            continue
        try:
            prev_int = int(prev_val)
            cur_int = int(cur_val)
        except (TypeError, ValueError):
            continue
        diff = cur_int - prev_int
        if diff > 0:
            deltas.append(StepDelta(
                event_type=event_type,
                delta=diff,
                current_value=cur_int,
                previous_value=prev_int,
            ))
    return deltas


def build_event_key(project_id: int, event_type: str, current_value: int) -> str:
    """
    Детерминированный ключ для дедупликации в v0 (агрегатная дельта,
    не individual event id).

    Формат: "<event_type>:<project_id>:<current_value>"
    Пример: "user_registered:1:31" -- "на проекте 1 счётчик регистраций
    достиг 31". Если /run обнаружит тот же current_value повторно (счётчик
    не изменился), event_key будет идентичен -- запись в NotificationLog
    предотвратит повторную отправку.
    """
    return f"{event_type}:{project_id}:{current_value}"


# ---------------------------------------------------------------------------
# Группировка в batch с учётом anti-spam guardrail
# ---------------------------------------------------------------------------

def build_notification_batch(deltas: list[StepDelta]) -> NotificationBatch:
    """
    Решает: слать каждую дельту персонально или объединить в дайджест.

    Guardrail: если суммарный прирост user_registered за этот цикл сравнения
    больше DIGEST_THRESHOLD_PER_RUN -- весь batch уходит дайджестом, чтобы
    не заспамить владельца. Это намеренно консервативно (весь batch, не
    только registrations) -- если регистраций много, остальные шаги тоже
    скорее всего пришли пачкой и их разумнее показать одной сводкой.
    """
    if not deltas:
        return NotificationBatch(deltas=[], is_digest=False)

    registered_delta = next((d for d in deltas if d.event_type == "user_registered"), None)
    registered_count = registered_delta.delta if registered_delta else 0

    if registered_count > DIGEST_THRESHOLD_PER_RUN:
        digest_text = _format_digest(deltas)
        return NotificationBatch(deltas=deltas, is_digest=True, digest_text=digest_text)

    return NotificationBatch(deltas=deltas, is_digest=False)


def _format_digest(deltas: list[StepDelta]) -> str:
    """Объединённый дайджест вместо потока отдельных уведомлений."""
    by_type = {d.event_type: d.delta for d in deltas}
    parts: list[str] = []
    if by_type.get("user_registered"):
        parts.append(f"{by_type['user_registered']} регистраций")
    if by_type.get("channel_created"):
        parts.append(f"{by_type['channel_created']} каналов")
    fb_good = by_type.get("first_post_feedback_good", 0)
    fb_bad = by_type.get("first_post_feedback_bad", 0)
    if fb_good or fb_bad:
        parts.append(f"{fb_good + fb_bad} отзывов о первом посте")
    if by_type.get("pricing_viewed"):
        parts.append(f"{by_type['pricing_viewed']} открытий тарифов")
    if by_type.get("payment_started"):
        parts.append(f"{by_type['payment_started']} попыток оплаты")
    if by_type.get("payment_success"):
        parts.append(f"{by_type['payment_success']} успешных оплат")

    summary = ", ".join(parts) if parts else "новых событий"
    return f"Активность пользователей — TruePost\n\nЗа последний цикл: {summary}."


# ---------------------------------------------------------------------------
# Форматирование отдельных уведомлений (когда не дайджест)
# ---------------------------------------------------------------------------

def format_notification(delta: StepDelta, source_label: str = "неизвестный источник") -> str:
    """
    Форматирует уведомление об одной дельте.

    v0-ограничение: т.к. это агрегатная дельта, а не конкретный пользователь,
    путь пользователя ("регистрация ✓, канал ещё нет...") в v0 неизвестен.
    Уведомление честно говорит о приросте счётчика и его влиянии на
    текущую проверку, без выдумывания пути конкретного человека.

    source_label передаётся опционально -- из source_breakdown, если он
    доступен на момент дельты. Если breakdown недоступен, используется
    общая формулировка без указания источника.
    """
    if delta.event_type == "user_registered":
        return (
            f"Новый пользователь — TruePost\n\n"
            f"Зарегистрировался {delta.delta} {'человек' if delta.delta == 1 else 'человек(а)'}.\n\n"
            f"Влияние:\n"
            f"+{delta.delta} к текущей проверке «Путь после первого поста». "
            f"Смотрим, дойдёт ли пользователь до канала, первого поста и тарифов."
        )

    if delta.event_type == "channel_created":
        return (
            f"Путь пользователя обновился — TruePost\n\n"
            f"Создан канал ({delta.delta}).\n\n"
            f"Влияние:\n"
            f"Ранний шаг пройден. Теперь важно понять, оценит ли пользователь "
            f"первый пост и дойдёт ли до тарифов."
        )

    if delta.event_type == "first_post_feedback_good":
        return (
            f"Первый пост оценили — TruePost\n\n"
            f"Оценка: пост подходит ({delta.delta}).\n\n"
            f"Влияние:\n"
            f"Это снижает вероятность, что главный блокер — качество первого поста. "
            f"Если тарифы всё равно не откроют, усилится идея «очередь постов на неделю»."
        )

    if delta.event_type == "first_post_feedback_bad":
        return (
            f"Первый пост оценили — TruePost\n\n"
            f"Оценка: пост не подходит ({delta.delta}).\n\n"
            f"Влияние:\n"
            f"Возможно, дело в качестве первого поста, а не только в тарифном экране. "
            f"Смотрим причины «не подходит» в /experiments."
        )

    if delta.event_type == "pricing_viewed":
        return (
            f"Коммерческий сигнал — TruePost\n\n"
            f"Открытие тарифов ({delta.delta}).\n\n"
            f"Влияние:\n"
            f"Пользователь дошёл до коммерческого шага. Следим, нажмёт ли оплату."
        )

    if delta.event_type == "payment_started":
        return (
            f"Важный сигнал — TruePost\n\n"
            f"Начата оплата ({delta.delta}).\n\n"
            f"Влияние:\n"
            f"Появился первый платёжный интерес. Если оплату не завершит, "
            f"нужно смотреть платёжный путь."
        )

    if delta.event_type == "payment_success":
        return (
            f"Оплата! — TruePost\n\n"
            f"Успешная оплата ({delta.delta}).\n\n"
            f"Влияние:\n"
            f"Воронка довела пользователя до оплаты. Это главный сигнал — "
            f"проверка можно считать пройденной."
        )

    if delta.event_type == "payment_failed":
        return (
            f"Внимание — TruePost\n\n"
            f"Неуспешная попытка оплаты ({delta.delta}).\n\n"
            f"Влияние:\n"
            f"Стоит проверить платёжный шлюз — /pay."
        )

    return f"Событие TruePost: {delta.event_type} (+{delta.delta})."
