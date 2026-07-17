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


# ---------------------------------------------------------------------------
# Per-user journeys (v1) -- более точные уведомления по конкретному
# (анонимному) пользователю, заменяют delta-уведомления когда journeys
# endpoint доступен. Используется только user_key (анонимный идентификатор),
# никакого PII.
# ---------------------------------------------------------------------------

# Источники -- те же метки, что в traffic_sources.py KNOWN_SOURCES,
# продублировано здесь чтобы notifications.py не тянул лишний модуль-зависимость
# для одной мелкой функции. При расхождении меток -- traffic_sources.py
# источник правды для /funnel, тут только для текста уведомлений.
_SOURCE_LABELS = {
    "yandex_direct": "Яндекс.Директ",
    "yandex": "Яндекс.Директ",
    "direct": "Яндекс.Директ",
    "ya_direct": "Яндекс.Директ",
    "telegram_ads": "Telegram Ads",
    "tgads": "Telegram Ads",
    "telegram": "Telegram",
    "organic": "Органика",
    "referral": "Реферал",
}


def _source_label(journey: dict) -> str:
    raw = (journey.get("source") or journey.get("utm_source") or "").strip().lower()
    return _SOURCE_LABELS.get(raw, "неизвестного источника")


# Через сколько минут после pricing_viewed без payment_started считаем,
# что пользователь "застрял" на тарифном экране.
STUCK_TARIFF_SCREEN_MINUTES = 45


def build_journey_event_key(user_key: str, step: str, timestamp: str | None, extra: str | None = None) -> str:
    """
    event_key для per-user journey событий.
    Формат: journey:<user_key>:<step>:<timestamp>[:<extra>]

    timestamp обязателен для стабильности -- если TruePost вернёт тот же
    timestamp повторно (событие не изменилось), ключ будет идентичен и
    NotificationLog предотвратит дубль. Если значение поменяется (новый
    feedback после повторной оценки), ключ изменится и уведомление уйдёт
    заново -- это осознанное поведение, не баг.
    """
    base = f"journey:{user_key}:{step}:{timestamp or 'none'}"
    if extra:
        base += f":{extra}"
    return base


def _journey_path_lines(journey: dict) -> list[str]:
    """Формирует блок 'Путь:' для уведомления — используем только осознанные шаги."""
    lines = ["регистрация ✓"]
    lines.append("канал создан ✓" if journey.get("channel_created_at") else "канал не создан")

    feedback = journey.get("first_post_feedback")
    if feedback == "good" or feedback is True:
        lines.append("первый пост: подходит")
    elif feedback == "bad" or feedback is False:
        lines.append("первый пост: не подходит")
    else:
        lines.append("первый пост: нет отзыва")

    lines.append("тарифы открыты ✓" if journey.get("pricing_viewed_at") else "тарифы не открывал")

    if journey.get("payment_success_at"):
        lines.append("оплата завершена ✓")
    elif journey.get("payment_started_at"):
        lines.append("оплату начал")
    else:
        lines.append("оплату пока не начал")

    return lines


def format_journey_pricing_viewed(journey: dict) -> str:
    """Уведомление: пользователь открыл тарифы (per-user, с путём)."""
    user_key = journey.get("user_key", "unknown")
    source = _source_label(journey)
    path = "\n".join(_journey_path_lines(journey))
    return (
        f"Коммерческий сигнал — TruePost\n\n"
        f"Пользователь {user_key} из {source} дошёл до тарифов.\n\n"
        f"Путь:\n{path}\n\n"
        f"Где сейчас:\nэкран тарифов\n\n"
        f"Влияние:\n"
        f"путь до коммерческого шага сработал. Следим, начнёт ли оплату."
    )


def format_journey_stuck_tariff_screen(journey: dict, minutes_stuck: int) -> str:
    """Уведомление: пользователь застрял на тарифном экране 45+ минут."""
    user_key = journey.get("user_key", "unknown")
    return (
        f"Пользователь застрял — TruePost\n\n"
        f"Пользователь {user_key} открыл тарифы {minutes_stuck}+ минут назад, "
        f"но оплату не начал.\n\n"
        f"Сигнал:\n"
        f"путь до тарифов сработал, но тарифный экран не довёл до начала оплаты."
    )


def format_journey_payment_started(journey: dict) -> str:
    """Уведомление: пользователь начал оплату (per-user)."""
    user_key = journey.get("user_key", "unknown")
    return (
        f"Важный сигнал — TruePost\n\n"
        f"Пользователь {user_key} начал оплату.\n\n"
        f"Влияние:\n"
        f"появился реальный платёжный интерес. Если оплата не завершится, "
        f"проверяем платёжный путь."
    )


def format_journey_payment_success(journey: dict) -> str:
    """Уведомление: пользователь успешно оплатил (per-user)."""
    user_key = journey.get("user_key", "unknown")
    return (
        f"Оплата — TruePost\n\n"
        f"Пользователь {user_key} оплатил.\n\n"
        f"Влияние:\n"
        f"получен коммерческий результат. Нужно смотреть источник, путь и повторяемость."
    )


def format_journey_payment_failed(journey: dict) -> str:
    """Уведомление: попытка оплаты завершилась ошибкой (per-user)."""
    user_key = journey.get("user_key", "unknown")
    return (
        f"Внимание — TruePost\n\n"
        f"У пользователя {user_key} не получилось оплатить.\n\n"
        f"Влияние:\n"
        f"стоит проверить платёжный шлюз — /pay."
    )


def _short_path_summary(journey: dict) -> str:
    """
    Короткое резюме пути одной строкой для /today и /experiments.
    Пример: "u_febdae54: Telegram Ads → канал → тарифы → ждём оплату"
    """
    user_key = journey.get("user_key", "unknown")
    source = _source_label(journey)
    steps = [source]
    if journey.get("channel_created_at"):
        steps.append("канал")
    feedback = journey.get("first_post_feedback")
    if feedback == "good" or feedback is True:
        steps.append("отзыв хороший")
    elif feedback == "bad" or feedback is False:
        steps.append("отзыв плохой")
    if journey.get("pricing_viewed_at"):
        steps.append("тарифы")
    if journey.get("payment_success_at"):
        steps.append("оплачено")
    elif journey.get("payment_started_at"):
        steps.append("оплата начата")
    elif journey.get("pricing_viewed_at"):
        steps.append("ждём оплату")
    return f"{user_key}: " + " → ".join(steps)


def pick_recent_commercial_journey(journeys: list[dict]) -> dict | None:
    """
    Выбирает самый свежий journey, дошедший хотя бы до открытия тарифов
    (для блока 'Последний коммерческий путь' в /today).
    """
    candidates = [j for j in journeys if j.get("pricing_viewed_at")]
    if not candidates:
        return None
    # Сортируем по pricing_viewed_at по убыванию (самый свежий первый)
    candidates.sort(key=lambda j: j.get("pricing_viewed_at") or "", reverse=True)
    return candidates[0]


def pick_recent_stuck_journey(journeys: list[dict]) -> tuple[dict, int] | None:
    """
    Выбирает самый свежий "застрявший" journey (есть minutes_since_last_step
    и stuck_at == 'tariff_screen' или похожее), для блока в /today.
    Возвращает (journey, minutes) или None.
    """
    candidates = [
        j for j in journeys
        if j.get("pricing_viewed_at") and not j.get("payment_started_at")
        and (j.get("minutes_since_last_step") or 0) >= STUCK_TARIFF_SCREEN_MINUTES
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda j: j.get("minutes_since_last_step") or 0, reverse=True)
    top = candidates[0]
    return top, int(top.get("minutes_since_last_step") or 0)


def format_recent_journeys_block(journeys: list[dict], max_lines: int = 5) -> str:
    """
    Блок 'Последние пользовательские пути' для /experiments.
    Берёт самые продвинутые (по числу пройденных шагов) journeys, не больше
    max_lines штук.
    """
    if not journeys:
        return ""

    def _progress_score(j: dict) -> int:
        score = 0
        for field in ["channel_created_at", "first_post_feedback_at",
                       "pricing_viewed_at", "payment_started_at", "payment_success_at"]:
            if j.get(field):
                score += 1
        return score

    sorted_journeys = sorted(journeys, key=_progress_score, reverse=True)
    top = sorted_journeys[:max_lines]
    lines = ["\nПоследние пользовательские пути:"]
    for j in top:
        lines.append(f"— {_short_path_summary(j)}")
    return "\n".join(lines)


def build_journey_notifications(
    journeys: list[dict],
    already_notified_keys: set[str],
) -> list[tuple[str, str]]:
    """
    Главная точка входа: проходит по journeys, определяет какие события
    требуют уведомления, фильтрует уже отправленные (already_notified_keys --
    множество event_key, которые уже есть в NotificationLog, передаётся
    извне после батч-проверки в БД).

    Возвращает список (event_key, notification_text) -- НЕ отправляет и
    НЕ пишет в NotificationLog сам, это делает вызывающий код (scheduler),
    чтобы дедупликация и сайд-эффекты были явными и тестируемыми отдельно.

    Порядок проверки на одного пользователя: pricing_viewed -> stuck ->
    payment_started -> payment_success -> payment_failed. Один journey может
    дать несколько уведомлений за один проход (например pricing_viewed и
    payment_started одновременно, если оба произошли между опросами) --
    это нормально, у каждого свой event_key.
    """
    results: list[tuple[str, str]] = []

    for journey in journeys:
        user_key = journey.get("user_key")
        if not user_key:
            continue

        if journey.get("pricing_viewed_at"):
            key = build_journey_event_key(user_key, "pricing_viewed", journey.get("pricing_viewed_at"))
            if key not in already_notified_keys:
                results.append((key, format_journey_pricing_viewed(journey)))

        stuck = pick_recent_stuck_journey([journey])
        if stuck:
            _, minutes = stuck
            key = build_journey_event_key(
                user_key, "stuck_tariff_screen", journey.get("pricing_viewed_at"),
            )
            if key not in already_notified_keys:
                results.append((key, format_journey_stuck_tariff_screen(journey, minutes)))

        if journey.get("payment_started_at"):
            key = build_journey_event_key(user_key, "payment_started", journey.get("payment_started_at"))
            if key not in already_notified_keys:
                results.append((key, format_journey_payment_started(journey)))

        if journey.get("payment_success_at"):
            key = build_journey_event_key(user_key, "payment_success", journey.get("payment_success_at"))
            if key not in already_notified_keys:
                results.append((key, format_journey_payment_success(journey)))

        if journey.get("payment_failed_at"):
            key = build_journey_event_key(user_key, "payment_failed", journey.get("payment_failed_at"))
            if key not in already_notified_keys:
                results.append((key, format_journey_payment_failed(journey)))

    return results


# ---------------------------------------------------------------------------
# Founder Live Feed (v2) -- дискретные события из /api/internal/user-events,
# с режимами smart/founder и digest anti-spam.
# ---------------------------------------------------------------------------

# Событие считается "важным" (smart mode) если оно в этом множестве.
# Обычная регистрация/создание канала/good feedback НЕ входят в smart --
# они шумные при малом трафике их можно пропускать, если владелец не хочет
# видеть вообще всё.
SMART_MODE_EVENT_TYPES = frozenset([
    "first_post_feedback_bad",
    "pricing_viewed",
    "payment_started",
    "payment_success",
    "payment_failed",
    "stuck_tariff_screen",  # синтетическое событие, генерируется отдельно
])

# founder mode -- показывает вообще все определённые типы событий, включая
# user_registered / channel_created / good feedback.
FOUNDER_MODE_EVENT_TYPES = frozenset([
    "user_registered", "channel_created",
    "first_post_feedback_good", "first_post_feedback_bad",
    "pricing_viewed", "payment_cta_clicked",
    "payment_started", "payment_success", "payment_failed",
    "stuck_tariff_screen",
])

FOUNDER_FEED_DIGEST_THRESHOLD = 10


def should_notify_event(event_type: str, mode: str) -> bool:
    """True если событие этого типа должно уведомляться в данном режиме."""
    if mode == "off":
        return False
    if mode == "founder":
        return event_type in FOUNDER_MODE_EVENT_TYPES
    # smart (по умолчанию)
    return event_type in SMART_MODE_EVENT_TYPES


def build_user_event_key(event_id: str) -> str:
    """event_key для discrete user-event. Формат: user_event:<event_id>"""
    return f"user_event:{event_id}"


def build_stuck_event_key(user_key: str, stuck_at: str, base_timestamp: str | None) -> str:
    """
    event_key для синтетического stuck-события.
    Формат: stuck:<user_key>:<stuck_at>:<base_event_timestamp>
    """
    return f"stuck:{user_key}:{stuck_at}:{base_timestamp or 'none'}"


def _feed_source_label(event: dict) -> str:
    raw = (event.get("source") or event.get("utm_source") or "").strip().lower()
    return _SOURCE_LABELS.get(raw, "неизвестного источника")


def _feed_path_lines(snapshot: dict) -> list[str]:
    """Короткий путь из journey_snapshot для Founder Live Feed сообщений."""
    steps = []
    if snapshot.get("registered"):
        steps.append("регистрация ✓")
    if snapshot.get("channel_created"):
        steps.append("канал ✓")
    if snapshot.get("pricing_viewed"):
        steps.append("тарифы ✓")
    return " → ".join(steps) if steps else "регистрация ✓"


def format_feed_user_registered(event: dict) -> str:
    user_key = event.get("user_key", "unknown")
    source = _feed_source_label(event)
    return (
        f"Новая конверсия — TruePost\n\n"
        f"{user_key} из {source} зарегистрировался.\n\n"
        f"Путь:\nрегистрация ✓\n\n"
        f"Ждём:\nсоздаст ли канал.\n\n"
        f"Доска: /board"
    )


def format_feed_channel_created(event: dict) -> str:
    user_key = event.get("user_key", "unknown")
    return (
        f"Путь обновился — TruePost\n\n"
        f"{user_key} создал канал.\n\n"
        f"Путь:\nрегистрация ✓ → канал ✓\n\n"
        f"Ждём:\nоценит ли первый пост и дойдёт ли до тарифов.\n\n"
        f"Доска: /board"
    )


def format_feed_first_post_feedback_bad(event: dict) -> str:
    user_key = event.get("user_key", "unknown")
    reason = (event.get("journey_snapshot") or {}).get("first_post_feedback_reason") or "не указана"
    return (
        f"Первый пост не подошёл — TruePost\n\n"
        f"{user_key} оценил первый пост: не подошёл.\n\n"
        f"Причина:\n{reason}\n\n"
        f"Что это значит:\n"
        f"этот путь пока упирается в первый результат, не в оплату.\n\n"
        f"Доска: /board"
    )


def format_feed_first_post_feedback_good(event: dict) -> str:
    user_key = event.get("user_key", "unknown")
    return (
        f"Первый пост подошёл — TruePost\n\n"
        f"{user_key} оценил первый пост: подходит.\n\n"
        f"Что это значит:\n"
        f"качество первого результата по этому пути не выглядит блокером. "
        f"Смотрим, дойдёт ли до тарифов.\n\n"
        f"Доска: /board"
    )


def format_feed_pricing_viewed(event: dict) -> str:
    user_key = event.get("user_key", "unknown")
    snapshot = event.get("journey_snapshot") or {}
    path = _feed_path_lines(snapshot)
    return (
        f"Коммерческий сигнал — TruePost\n\n"
        f"{user_key} открыл тарифы.\n\n"
        f"Путь:\n{path}\n\n"
        f"Ждём:\nнажмёт ли оплату.\n\n"
        f"Доска: /board"
    )


def format_feed_payment_started(event: dict) -> str:
    user_key = event.get("user_key", "unknown")
    return (
        f"Важный сигнал — TruePost\n\n"
        f"{user_key} начал оплату.\n\n"
        f"Что это значит:\n"
        f"появился реальный платёжный интерес. "
        f"Если не завершит — проверяем платёжный путь.\n\n"
        f"Доска: /board"
    )


def format_feed_payment_success(event: dict) -> str:
    user_key = event.get("user_key", "unknown")
    return (
        f"Оплата — TruePost\n\n"
        f"{user_key} оплатил.\n\n"
        f"Что это значит:\n"
        f"получен коммерческий proof. Нужно смотреть источник и повторяемость.\n\n"
        f"Доска: /board"
    )


def format_feed_payment_failed(event: dict) -> str:
    user_key = event.get("user_key", "unknown")
    return (
        f"Внимание — TruePost\n\n"
        f"У {user_key} не получилось оплатить.\n\n"
        f"Что это значит:\n"
        f"стоит проверить платёжный шлюз — /pay.\n\n"
        f"Доска: /board"
    )


def format_feed_stuck_tariff_screen(user_key: str, minutes: int) -> str:
    return (
        f"Пользователь застрял — TruePost\n\n"
        f"{user_key} открыл тарифы {minutes}+ минут назад, но оплату не начал.\n\n"
        f"Что это значит:\n"
        f"если таких будет 5+, проверяем тарифный экран.\n\n"
        f"Доска: /board"
    )


_FEED_FORMATTERS = {
    "user_registered": format_feed_user_registered,
    "channel_created": format_feed_channel_created,
    "first_post_feedback_bad": format_feed_first_post_feedback_bad,
    "first_post_feedback_good": format_feed_first_post_feedback_good,
    "pricing_viewed": format_feed_pricing_viewed,
    "payment_started": format_feed_payment_started,
    "payment_success": format_feed_payment_success,
    "payment_failed": format_feed_payment_failed,
}


def format_feed_event(event: dict) -> str | None:
    """Форматирует одно discrete-событие. None если тип не поддерживается (не должно случаться)."""
    formatter = _FEED_FORMATTERS.get(event.get("event_type"))
    if formatter is None:
        return None
    return formatter(event)


def format_feed_digest(events: list[dict]) -> str:
    """Дайджест вместо потока отдельных сообщений (>FOUNDER_FEED_DIGEST_THRESHOLD за цикл)."""
    by_type: dict[str, int] = {}
    for e in events:
        et = e.get("event_type", "unknown")
        by_type[et] = by_type.get(et, 0) + 1

    parts = []
    labels = {
        "user_registered": "регистраций", "channel_created": "каналов",
        "first_post_feedback_good": "хороших отзывов", "first_post_feedback_bad": "плохих отзывов",
        "pricing_viewed": "открытий тарифов", "payment_started": "попыток оплаты",
        "payment_success": "успешных оплат", "payment_failed": "неуспешных оплат",
    }
    for event_type, count in by_type.items():
        label = labels.get(event_type, event_type)
        parts.append(f"{count} {label}")

    summary = ", ".join(parts) if parts else "новых событий"
    return f"Активность пользователей — TruePost\n\nЗа последний цикл: {summary}.\n\nДоска: /board"


def detect_stuck_events(journeys: list[dict]) -> list[tuple[str, str, int]]:
    """
    Определяет застрявшие пути среди journeys (snapshot list, не discrete events).
    Возвращает список (user_key, pricing_viewed_at, minutes) для тех, кто
    >=STUCK_TARIFF_SCREEN_MINUTES на тарифном экране без payment_started.
    """
    results = []
    for j in journeys:
        if j.get("pricing_viewed_at") and not j.get("payment_started_at"):
            minutes = int(j.get("minutes_since_last_step") or 0)
            if minutes >= STUCK_TARIFF_SCREEN_MINUTES:
                results.append((j.get("user_key", "unknown"), j.get("pricing_viewed_at"), minutes))
    return results
