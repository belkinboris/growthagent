"""
Telegram-бот.

Главный принцип: бот НЕ анализирует данные самостоятельно. Он только:
- вызывает scheduler.run_cycle_once() (для /run и автоматического цикла);
- форматирует CycleResult в текст по фиксированному шаблону;
- обрабатывает нажатия кнопок, меняя статус Alert в БД.

Никаких действий в рекламе/продукте/лендинге/оплатах -- кнопки в v1 только
меняют состояние алерта (acknowledged/snoozed), не трогают внешние системы.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from sqlmodel import select

from app.analyzer import AlertCandidate
from app.config import get_settings, MIN_SIGNUP_CONVERSION_WARN_PERCENT, DEFAULT_QUERY_CLUSTERS
from app.db import get_session
from app.models import Alert, AlertStatus, Integration, IntegrationStatus, Project
from app.rules import NormalizedMetrics
from app.scheduler import run_cycle_once
from app.service import AlertChange, AlertChangeType, CycleResult

logger = logging.getLogger("growth_agent.telegram")


CONFIDENCE_RU = {
    "low": "данных мало, вывод осторожный",
    "medium": "есть первый сигнал, но выборка небольшая",
    "high": "проблема подтверждается, данных достаточно",
}

SNOOZE_HOURS = 24


# ---------------------------------------------------------------------------
# Человекочитаемое объяснение payload по rule_id ("Объясни подробнее").
# Raw JSON НЕ показывается обычному пользователю -- слишком технически.
# Каждое правило имеет свой смысл payload, поэтому это per-rule функции,
# а не общий шаблон "ключ: значение".
# ---------------------------------------------------------------------------


def _explain_spend_no_signups(payload: dict) -> str:
    return (
        f"Почему я так решил: потрачено {payload.get('spend', 0):.0f} ₽, "
        f"кликов было {payload.get('clicks', 0)}, регистраций — 0.\n\n"
        "Вероятная зона проблемы: реклама → лендинг → форма регистрации.\n\n"
        "Что проверить:\n"
        "1. Откуда именно идёт трафик — соответствует ли он целевой аудитории.\n"
        "2. Открывается ли лендинг корректно с мобильного устройства.\n"
        "3. Не сломана ли форма регистрации технически."
    )


def _explain_clicks_no_signups(payload: dict) -> str:
    return (
        f"Почему я так решил: было {payload.get('clicks', 0)} кликов, "
        "ни одной регистрации.\n\n"
        "Вероятная зона проблемы: лендинг или мобильный путь пользователя.\n\n"
        "Что проверить:\n"
        "1. Открыть лендинг с телефона, как реальный пользователь.\n"
        "2. Понятен ли первый экран и есть ли явная кнопка действия.\n"
        "3. Не зависает ли страница на медленном интернете."
    )


def _explain_signups_no_activation_1(payload: dict) -> str:
    return (
        f"Почему я так решил: зарегистрировалось {payload.get('signup', 0)} человек, "
        "но ни один не дошёл до первого шага после регистрации.\n\n"
        "Вероятная зона проблемы: онбординг сразу после регистрации.\n\n"
        "Что проверить:\n"
        "1. Понятно ли, что делать сразу после регистрации.\n"
        "2. Нет ли технической ошибки на этом шаге.\n"
        "3. Не теряется ли пользователь между регистрацией и следующим действием."
    )


def _explain_activation_1_no_activation_2(payload: dict) -> str:
    return (
        f"Почему я так решил: {payload.get('activation_1', 0)} пользователей прошли первый шаг, "
        "но никто не дошёл до второго.\n\n"
        "Вероятная зона проблемы: переход от первого шага ко второму.\n\n"
        "Что проверить:\n"
        "1. Понятен ли пользователю следующий шаг.\n"
        "2. Нет ли задержки или сложности на этом этапе."
    )


def _explain_payments_started_no_success(payload: dict) -> str:
    return (
        f"Почему я так решил: начато {payload.get('payment_started', 0)} оплат, "
        "успешных — 0.\n\n"
        "Вероятная зона проблемы: форма оплаты или техническая ошибка платёжной системы.\n\n"
        "Что проверить:\n"
        "1. Пройти оплату самостоятельно тестовым платежом.\n"
        "2. Проверить логи YooKassa на ошибки."
    )


def _explain_pending_payments(payload: dict) -> str:
    return (
        f"Почему я так решил: {payload.get('pending_payments', 0)} платежей в подвешенном статусе.\n\n"
        "Это может быть нормальной задержкой обработки или зависшим webhook.\n\n"
        "Что проверить:\n"
        "1. Статус этих платежей в кабинете YooKassa.\n"
        "2. Не повторять платёж до выяснения причины."
    )


def _explain_metrics_discrepancy(payload: dict) -> str:
    return (
        f"Почему я так решил: продукт показывает {payload.get('signup', 0)} регистраций, "
        "а цель в Яндекс.Метрике — 0.\n\n"
        "Вероятная причина: не настроена цель или счётчик не стоит на странице успеха.\n\n"
        "Что проверить:\n"
        "1. Настройку цели register_success в Метрике.\n"
        "2. Установлен ли счётчик на странице после регистрации."
    )


def _explain_low_signup_conversion(payload: dict) -> str:
    return (
        f"Почему я так решил: за период было {payload.get('clicks', 0)} кликов из Директа, "
        f"регистраций в продукте — {payload.get('signup', 0)}, соотношение — "
        f"{payload.get('conversion_percent', 0)}%.\n\n"
        f"Это ниже осторожного порога {MIN_SIGNUP_CONVERSION_WARN_PERCENT}%. "
        "Атрибуция регистраций к Директу не подтверждена -- цифры приведены за один и тот же "
        "период, не как прямая причинно-следственная связь.\n\n"
        "Вероятная зона проблемы: реклама → лендинг → регистрация.\n\n"
        "Что проверить:\n"
        "1. Соответствие объявления первому экрану.\n"
        "2. Понятность оффера.\n"
        "3. Форму регистрации."
    )


def _explain_integration_down(payload: dict) -> str:
    error = payload.get("error") or "источник не отвечает"
    return (
        f"Почему я так решил: {error}.\n\n"
        "Это техническая проблема с подключением, не с бизнесом.\n\n"
        "Что проверить:\n"
        "1. Действителен ли токен доступа.\n"
        "2. Доступен ли сервис сейчас."
    )


_EXPLAIN_BY_RULE_ID = {
    "spend_no_signups": _explain_spend_no_signups,
    "clicks_no_signups": _explain_clicks_no_signups,
    "signups_no_activation_1": _explain_signups_no_activation_1,
    "activation_1_no_activation_2": _explain_activation_1_no_activation_2,
    "payments_started_no_success": _explain_payments_started_no_success,
    "pending_payments": _explain_pending_payments,
    "metrics_discrepancy": _explain_metrics_discrepancy,
    "low_signup_conversion": _explain_low_signup_conversion,
    "integration_down": _explain_integration_down,
}


def format_alert_details(alert: Alert) -> str:
    """
    Человекочитаемое объяснение алерта для кнопки "Объясни подробнее".
    Использует rule_id из fingerprint (формат project_id/rule_id/period_key/
    affected_step), чтобы выбрать правильный explainer. Если rule_id не
    распознан (например, появилось новое правило, для которого explainer
    ещё не написан) -- честно говорит "детали не настроены" вместо падения
    или показа raw JSON.
    """
    parts = alert.fingerprint.split("/")
    rule_id = parts[1] if len(parts) >= 2 else None

    explainer = _EXPLAIN_BY_RULE_ID.get(rule_id)
    if explainer is None:
        return (
            f"Подробное объяснение для «{alert.title}» пока не настроено.\n"
            f"Технические детали можно посмотреть через /debug_alert {alert.id} "
            "(только для администратора)."
        )

    return explainer(alert.payload_json)


# ---------------------------------------------------------------------------
# Форматирование сообщений (один шаблон для всех случаев -- ручной /run,
# автоматический цикл, с LLM и без него -- LLM пока не подключён, поэтому
# hypothesis/check_action/do_not_action берутся прямо из AlertCandidate,
# которые уже сформулированы шаблонами правил в rules.py)
# ---------------------------------------------------------------------------


def _format_metrics_line(metrics: NormalizedMetrics) -> str:
    parts = []
    if metrics.spend is not None or metrics.clicks is not None:
        spend_str = f"{metrics.spend:.0f} ₽" if metrics.spend is not None else "—"
        clicks_str = str(metrics.clicks) if metrics.clicks is not None else "—"
        ctr_str = f"{metrics.ctr:.1f}%" if metrics.ctr is not None else "—"
        parts.append(f"Реклама: {spend_str} / {clicks_str} кликов / CTR {ctr_str}")
    signup_str = metrics.signup if metrics.signup is not None else "—"
    act1_str = metrics.activation_1 if metrics.activation_1 is not None else "—"
    act2_str = metrics.activation_2 if metrics.activation_2 is not None else "—"
    pay_str = metrics.payment_success if metrics.payment_success is not None else "—"
    parts.append(f"Продукт: {signup_str} регистраций / {act1_str} / {act2_str} / {pay_str} оплат")
    return "\n".join(parts)


def format_alert_block(candidate: AlertCandidate, project_name: str, is_primary: bool = True) -> str:
    confidence_ru = CONFIDENCE_RU.get(candidate.confidence.value, candidate.confidence.value)

    if is_primary:
        lines = [
            "Growth Agent — watch-only",
            f"Проект: {project_name}",
            "",
            "Главный сигнал:",
            f"{candidate.title}, {confidence_ru}",
            "",
            "Где вероятно проблема:",
            candidate.hypothesis,
            "",
            "Что проверить:",
            candidate.check_action,
            "",
            "Что НЕ делать:",
            candidate.do_not_action,
        ]
    else:
        lines = [
            f"Также есть ранний сигнал: {candidate.title.lower()} ({confidence_ru}).",
            candidate.hypothesis,
        ]
    return "\n".join(lines)


def format_cycle_message(result: CycleResult, project_name: str) -> str:
    """
    Главная функция форматирования. Используется и для автоматических
    уведомлений, и для ответа на /run. Если новых бизнес-алертов нет, но
    есть integration_down -- показывает его. Если вообще ничего notifiable --
    показывает "всё спокойно" с текущими метриками (используется в /run,
    который должен показывать результат даже без новых алертов).

    Деп diagnostics (granular-находки) НЕ включаются в этот текст -- по
    дизайну двухуровневого вывода они доступны через кнопку "Показать
    детали"/"Проверить глубже" (см. build_alert_keyboard), а здесь только
    короткая пометка, что глубокая проверка была сделана и что нашла.
    """
    blocks = []

    new_or_escalated_integration = [
        c for c in result.integration_down_changes
        if c.change_type in (AlertChangeType.new, AlertChangeType.escalated)
    ]
    if new_or_escalated_integration:
        c = new_or_escalated_integration[0]
        blocks.append(
            "Growth Agent — внимание\n"
            f"Проект: {project_name}\n\n"
            f"{c.alert.title}\n"
            f"{c.alert.message}"
        )

    if result.primary_candidate is not None:
        blocks.append(format_alert_block(result.primary_candidate, project_name, is_primary=True))
        for sec in result.secondary:
            blocks.append(format_alert_block(sec, project_name, is_primary=False))

        deep_summary = _format_deep_diagnostics_teaser(result.deep_diagnostics)
        if deep_summary:
            blocks.append(deep_summary)
    elif not blocks:
        blocks.append(
            f"Growth Agent — watch-only\nПроект: {project_name}\n\n"
            "Главный сигнал: пока всё спокойно, явных проблем не найдено."
        )

    metrics_7d = result.metrics_by_window.get("7d")
    if metrics_7d:
        blocks.append(f"Метрики (7д):\n{_format_metrics_line(metrics_7d)}")

    return "\n\n".join(blocks)


def _format_deep_diagnostics_teaser(deep_diagnostics: dict | None) -> str | None:
    """
    Короткая пометка про deep diagnostics для основного сообщения --
    НЕ полная детализация (та идёт по кнопке, см. format_deep_diagnostics_details).
    Возвращает None, если diagnostics не запускался вовсе -- тогда в
    основном сообщении про него вообще не упоминается, чтобы не плодить
    лишние строки для тех, у кого Direct не настроен.
    """
    if deep_diagnostics is None:
        return None

    if deep_diagnostics.get("insufficient_data"):
        clicks = deep_diagnostics.get("total_clicks", 0)
        return (
            f"Глубокая проверка рекламы: данных пока мало ({clicks} кликов за период). "
            "Можно запустить вручную, но вывод будет предварительным."
        )

    main_finding = deep_diagnostics.get("main_finding")
    if main_finding:
        return f"Проверил группы объявлений и поисковые запросы: {main_finding['title'].lower()}. Детали — по кнопке ниже."

    return "Проверил группы объявлений и поисковые запросы: явных проблем не нашёл."


def format_deep_diagnostics_details(deep_diagnostics: dict, project_name: str) -> str:
    """
    Детальная диагностика по кнопке "Показать детали"/"Проверить глубже".
    Формат по задаче: главная находка, основные запросы, вероятная
    причина, что сделать, уровень уверенности.

    attribution_status формулируется явно текстом, не цифрой -- агент
    никогда не должен писать "клики дали регистрации" как причинно-
    следственную связь, если атрибуция не подтверждена (almost всегда
    not_available в v1, см. scheduler.run_deep_diagnostics_for_project).
    """
    attribution_ru = {
        "confirmed": "подтверждена",
        "partial": "частичная",
        "not_available": "не подтверждена",
    }

    lines = [
        "Growth Agent — рекламная диагностика",
        f"Проект: {project_name}",
        "",
    ]

    if deep_diagnostics.get("insufficient_data"):
        clicks = deep_diagnostics.get("total_clicks", 0)
        lines.append(
            f"Данных пока мало для глубокой диагностики: {clicks} кликов за 7 дней. "
            "Вывод будет предварительным, если запустить проверку сейчас."
        )
        return "\n".join(lines)

    main_finding = deep_diagnostics.get("main_finding")
    if main_finding is None:
        lines.append("Проверил группы объявлений и поисковые запросы — явных проблем не нашёл.")
        attribution_status = deep_diagnostics.get("attribution_status", "not_available")
        lines.append(f"\nАтрибуция регистраций к Директу: {attribution_ru.get(attribution_status, attribution_status)}.")
        return "\n".join(lines)

    lines.append(f"Главная находка:\n{main_finding['detail']}")

    top_queries = main_finding.get("payload", {}).get("top_queries")
    if top_queries:
        lines.append("\nОсновные запросы:")
        lines.extend(f"— {q}" for q in top_queries)

    if main_finding.get("recommended_action"):
        lines.append(f"\nЧто сделать:\n{main_finding['recommended_action']}")

    lines.append("\nЛендинг и рекламу одновременно менять не стоит — сначала найти проблемный сегмент.")

    attribution_status = deep_diagnostics.get("attribution_status", "not_available")
    lines.append(f"\nАтрибуция регистраций к Директу: {attribution_ru.get(attribution_status, attribution_status)}.")
    lines.append(f"Уровень уверенности: {main_finding.get('confidence', 'medium')}.")

    good_findings = deep_diagnostics.get("good_findings", [])
    if good_findings:
        lines.append("\nЕсть и хорошие сигналы:")
        for gf in good_findings[:2]:
            lines.append(f"— {gf['detail']}")

    return "\n".join(lines)


def format_negative_keywords_suggestion(deep_diagnostics: dict, query_clusters: Optional[dict] = None) -> str:
    """
    "Подготовить минус-фразы" -- по задаче, это MVP-заглушка: формирует
    список ПРЕДЛОЖЕННЫХ кандидатов в минус-фразы из top_queries найденного
    irrelevant-кластера, не применяет их автоматически в Директе.

    Защита от слишком широких минус-фраз (см. замечание архитектора):
    - предлагаются МНОГОСЛОВНЫЕ фразы (полные запросы или их значимые
      part), не отдельные слова -- одиночное слово "текст" или "генерация"
      может убить запросы вроде "генерация постов для Telegram", которые
      релевантны продукту;
    - явная проверка: ни один кандидат не должен пересекаться с include-
      термином good-кластеров (white-list) -- если слово "telegram" есть
      и в плохом, и в хорошем кластере, оно никогда не попадёт в минус-фразы;
    - стоп-слова (предлоги, частицы) исключаются, как и слова короче 4
      символов -- они либо бесполезны как минус-фразы, либо слишком общие.
    """
    main_finding = deep_diagnostics.get("main_finding")
    if main_finding is None or main_finding.get("finding_type") != "irrelevant_query_cluster":
        return "Нет подходящей находки для подготовки минус-фраз — нужен сигнал об нерелевантном кластере запросов."

    top_queries = main_finding.get("payload", {}).get("top_queries", [])
    if not top_queries:
        return "Запросы для минус-фраз не найдены в этой находке."

    query_clusters = query_clusters or DEFAULT_QUERY_CLUSTERS

    # White-list: все include-термины всех good-кластеров. Ни один
    # кандидат в минус-фразу не должен содержать ничего из этого списка
    # (даже как подстроку), иначе минус-фраза рискует обрезать релевантный
    # трафик вместе с нерелевантным.
    good_terms = set()
    for cluster_def in query_clusters.get("good", {}).values():
        good_terms |= {t.lower() for t in cluster_def.get("include", [])}

    _STOP_WORDS = {"для", "как", "что", "это", "или", "если", "при", "без", "под", "над"}

    def _is_safe_candidate(phrase: str) -> bool:
        phrase_lower = phrase.lower()
        if any(good_term in phrase_lower for good_term in good_terms):
            return False  # пересекается с релевантным intent -- не предлагаем
        if phrase_lower in _STOP_WORDS:
            return False
        if len(phrase_lower) < 4:
            return False
        return True

    # Кандидаты -- ПОЛНЫЕ запросы (не отдельные слова) плюс отдельные
    # значимые слова, которые сами по себе безопасны (прошли white-list).
    # Полные запросы как минус-фразы безопаснее единичных слов почти всегда,
    # поэтому идут первыми и это основной рекомендуемый вариант.
    full_query_candidates = [q for q in top_queries if _is_safe_candidate(q)]

    single_word_candidates = []
    seen_words = set()
    for query in top_queries:
        for word in query.lower().split():
            word = word.strip(".,!?")
            if word in seen_words:
                continue
            if _is_safe_candidate(word):
                seen_words.add(word)
                single_word_candidates.append(word)

    lines = ["Кандидаты в минус-фразы (черновик для ручной проверки):", ""]

    if full_query_candidates:
        lines.append("Рекомендуется (целые фразы, безопаснее):")
        lines.extend(f"-{q}" for q in full_query_candidates)
        lines.append("")

    if single_word_candidates:
        lines.append("Можно рассмотреть отдельно (проверьте, не слишком ли широко):")
        lines.extend(f"-{w}" for w in single_word_candidates)
        lines.append("")

    if not full_query_candidates and not single_word_candidates:
        lines.append("Не нашлось безопасных кандидатов — все слова из найденных запросов пересекаются с релевантными.")
        lines.append("")

    lines.append(
        "Это черновик, требующий проверки человеком. Минус-фразы НЕ применены "
        "автоматически — добавьте вручную в интерфейсе Директа, если согласны. "
        "Слова, совпадающие с релевантными запросами продукта, уже исключены из списка."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Кнопки
# ---------------------------------------------------------------------------


def _get_deep_diagnostics_for_keyboard(session, project_id: int) -> dict | None:
    """
    Возвращает result_json последнего свежего deep diagnostics кэша для
    проекта, если он есть -- используется при построении клавиатуры, чтобы
    решить, показывать "Показать детали" (уже есть что показать) или
    "Проверить глубже" (force refresh). Кэш привязан к project_id +
    period_key, не к конкретному alert_id -- в v1 один активный проект,
    поэтому "последний свежий кэш проекта" эквивалентно "кэш для текущего
    primary alert" на практике, разделять их не нужно.
    """
    from app.service import get_cached_diagnostics
    cached = get_cached_diagnostics(session, project_id, "7d")
    return cached.result_json if cached else None


def build_alert_keyboard(alert_id: int, has_deep_diagnostics: bool = False, deep_diagnostics_available: bool = False) -> InlineKeyboardMarkup:
    """
    has_deep_diagnostics -- diagnostics уже запускался в этом цикле
    (автоматически или из кэша), есть что показать по кнопке "Показать
    детали". deep_diagnostics_available -- Direct настроен и в принципе
    может быть запущен по требованию, даже если в этом цикле он не
    запускался (например, alert был не той категории, что триггерит auto-run) --
    тогда показываем "Проверить глубже" как force refresh.

    Обе кнопки не показываются одновременно: если диагностика уже есть --
    "Показать детали"; если нет, но в принципе доступна -- "Проверить глубже".
    """
    rows = [
        [
            InlineKeyboardButton("Понял", callback_data=f"ack:{alert_id}"),
            InlineKeyboardButton("Отложить", callback_data=f"snooze:{alert_id}"),
        ],
        [
            InlineKeyboardButton("Объясни подробнее", callback_data=f"explain:{alert_id}"),
            InlineKeyboardButton("Создай задачу", callback_data=f"task:{alert_id}"),
        ],
    ]

    if has_deep_diagnostics:
        rows.append([InlineKeyboardButton("Показать детали", callback_data=f"show_details:{alert_id}")])
    elif deep_diagnostics_available:
        rows.append([InlineKeyboardButton("Проверить глубже", callback_data=f"deep_check:{alert_id}")])

    return InlineKeyboardMarkup(rows)


def build_negative_keywords_keyboard(alert_id: int, deep_diagnostics: dict) -> InlineKeyboardMarkup | None:
    """
    Кнопки для детальной диагностики (формат "B" из задачи): "Создать
    задачу", "Подготовить минус-фразы", "Отклонить". "Подготовить минус-
    фразы" показывается только если главная находка -- irrelevant_query_cluster,
    иначе кнопка бессмысленна (нет запросов, из которых готовить минус-фразы).
    """
    main_finding = deep_diagnostics.get("main_finding")

    rows = [[InlineKeyboardButton("Создать задачу", callback_data=f"task:{alert_id}")]]

    if main_finding and main_finding.get("finding_type") == "irrelevant_query_cluster":
        rows[0].append(InlineKeyboardButton("Подготовить минус-фразы", callback_data=f"prepare_negative_keywords:{alert_id}"))

    rows.append([InlineKeyboardButton("Отклонить", callback_data=f"ack:{alert_id}")])

    return InlineKeyboardMarkup(rows)


async def on_button_press(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    try:
        action, alert_id_str = query.data.split(":", 1)
        alert_id = int(alert_id_str)
    except (ValueError, AttributeError):
        await query.edit_message_text("Не удалось обработать кнопку.")
        return

    with get_session() as session:
        alert = session.get(Alert, alert_id)
        if alert is None:
            await query.edit_message_text("Алерт не найден (возможно, уже обработан).")
            return

        if action == "ack":
            alert.status = AlertStatus.acknowledged
            session.add(alert)
            session.commit()
            await query.edit_message_text(f"{query.message.text}\n\n✓ Принято к сведению.")

        elif action == "snooze":
            alert.status = AlertStatus.snoozed
            alert.snooze_until = datetime.now(timezone.utc) + timedelta(hours=SNOOZE_HOURS)
            session.add(alert)
            session.commit()
            await query.edit_message_text(f"{query.message.text}\n\n⏸ Отложено на {SNOOZE_HOURS} часов.")

        elif action == "explain":
            details = format_alert_details(alert)
            await query.message.reply_text(details)

        elif action == "task":
            # В v1 нет отдельного таск-трекера -- фиксируем как acknowledged
            # с явной отметкой, что это осознанная задача, не просто "увидел".
            alert.status = AlertStatus.acknowledged
            session.add(alert)
            session.commit()
            await query.edit_message_text(f"{query.message.text}\n\n📌 Зафиксировано как задача.")

        elif action == "show_details":
            # Кэш уже есть (иначе кнопка была бы "Проверить глубже", не
            # "Показать детали") -- читаем его, не запускаем заново.
            deep_diagnostics = _get_deep_diagnostics_for_keyboard(session, alert.project_id)
            if deep_diagnostics is None:
                await query.message.reply_text(
                    "Детали не найдены (кэш устарел между нажатиями). Попробуйте /run заново."
                )
            else:
                project = session.get(Project, alert.project_id)
                project_name = project.name if project else "Проект"
                details_text = format_deep_diagnostics_details(deep_diagnostics, project_name)
                keyboard = build_negative_keywords_keyboard(alert.id, deep_diagnostics)
                await query.message.reply_text(details_text, reply_markup=keyboard)

        elif action == "deep_check":
            # Force refresh -- пользователь явно попросил, минуя кэш и
            # триггер-условие. Может занять до 30+ секунд (granular-отчёты
            # Директа), предупреждаем заранее.
            await query.message.reply_text("Проверяю глубже: смотрю группы объявлений и поисковые запросы...")
            from app.scheduler import force_refresh_deep_diagnostics
            refresh_result = await force_refresh_deep_diagnostics(alert.project_id)

            if not refresh_result["ok"]:
                await query.message.reply_text(
                    f"Не удалось выполнить глубокую проверку: {refresh_result['error']}\n\n"
                    "Можно проверить позже — light-диагностика продолжает работать как обычно."
                )
            else:
                project = session.get(Project, alert.project_id)
                project_name = project.name if project else "Проект"
                details_text = format_deep_diagnostics_details(refresh_result["result"], project_name)
                keyboard = build_negative_keywords_keyboard(alert.id, refresh_result["result"])
                await query.message.reply_text(details_text, reply_markup=keyboard)

        elif action == "prepare_negative_keywords":
            deep_diagnostics = _get_deep_diagnostics_for_keyboard(session, alert.project_id)
            if deep_diagnostics is None:
                await query.message.reply_text("Нет данных для подготовки минус-фраз — запустите проверку глубже сначала.")
            else:
                from app.diagnostics import get_query_clusters
                project = session.get(Project, alert.project_id)
                # Используем тот же словарь кластеров, что и при самой
                # диагностике (per-project settings с fallback на дефолт) --
                # иначе white-list защита в минус-фразах может разойтись с
                # тем, что реально считалось "хорошим" при поиске находки.
                project_query_clusters = get_query_clusters(project.settings_json if project else {})
                suggestion_text = format_negative_keywords_suggestion(deep_diagnostics, project_query_clusters)
                await query.message.reply_text(suggestion_text)

        else:
            await query.edit_message_text("Неизвестное действие.")


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------


def _get_active_project(session) -> Project | None:
    return session.exec(select(Project).where(Project.is_active == True)).first()


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from app.config import BUILD_MARKER
    await update.message.reply_text(
        "Growth Agent — watch-only.\n"
        f"Версия: {BUILD_MARKER}\n\n"
        "Команды:\n"
        "/status — состояние проекта\n"
        "/run — запустить проверку вручную\n"
        "/alerts — последние алерты\n"
        "/funnel — воронка\n"
        "/mode — текущий режим\n"
        "/settings — основные настройки\n"
        "/test_metrika — проверить подключение к Яндекс.Метрике\n"
        "/test_direct — проверить подключение к Яндекс.Директу\n"
        "/deep_direct — глубокая диагностика Директа (группы, запросы) независимо от текущего alert"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with get_session() as session:
        project = _get_active_project(session)
        if project is None:
            await update.message.reply_text("Активный проект не найден.")
            return

        integrations = session.exec(
            select(Integration).where(Integration.project_id == project.id)
        ).all()

        status_lines = [f"Проект: {project.name}", f"Режим: {project.settings_json.get('mode', 'watch_only')}", ""]
        status_lines.append("Интеграции:")
        status_emoji = {
            IntegrationStatus.ok: "🟢",
            IntegrationStatus.not_configured: "⚪️",
            IntegrationStatus.error: "🔴",
            IntegrationStatus.stale: "🟡",
        }
        for integ in integrations:
            emoji = status_emoji.get(integ.status, "⚪️")
            status_lines.append(f"{emoji} {integ.type.value}: {integ.status.value}")

        open_alerts_count = session.exec(
            select(Alert).where(
                Alert.project_id == project.id,
                Alert.status.in_([AlertStatus.open, AlertStatus.sent, AlertStatus.escalated]),
            )
        ).all()
        status_lines.append("")
        status_lines.append(f"Открытых алертов: {len(open_alerts_count)}")

        await update.message.reply_text("\n".join(status_lines))


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Запускаю проверку...")
    try:
        result = await run_cycle_once()
    except Exception as exc:
        logger.exception("Manual /run failed")
        await update.message.reply_text(f"Не удалось выполнить проверку: {exc}")
        return

    with get_session() as session:
        project = _get_active_project(session)
        project_name = project.name if project else "Проект"

    text = format_cycle_message(result, project_name)

    # /run показывает результат ВСЕГДА, даже если has_notifiable_changes
    # False -- это явный запрос пользователя посмотреть текущее состояние,
    # а не автоматическое уведомление, для которого молчание оправдано.
    if result.primary_candidate is not None:
        # Нужен alert_id для кнопок -- берём его из БД по fingerprint primary candidate
        with get_session() as session:
            alert = session.exec(
                select(Alert).where(Alert.fingerprint == result.primary_candidate.fingerprint)
            ).first()
            if alert:
                settings = get_settings()
                direct_available = bool(settings.effective_direct_oauth_token and settings.direct_client_login)
                keyboard = build_alert_keyboard(
                    alert.id,
                    has_deep_diagnostics=result.deep_diagnostics is not None,
                    deep_diagnostics_available=direct_available,
                )
            else:
                keyboard = None
        await update.message.reply_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text)


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with get_session() as session:
        project = _get_active_project(session)
        if project is None:
            await update.message.reply_text("Активный проект не найден.")
            return

        alerts = session.exec(
            select(Alert)
            .where(Alert.project_id == project.id)
            .order_by(Alert.last_seen_at.desc())
            .limit(10)
        ).all()

        if not alerts:
            await update.message.reply_text("Алертов пока нет.")
            return

        status_emoji = {
            AlertStatus.open: "🔵", AlertStatus.sent: "🔵", AlertStatus.acknowledged: "✅",
            AlertStatus.resolved: "⚪️", AlertStatus.escalated: "🔴", AlertStatus.snoozed: "⏸",
        }
        lines = []
        for a in alerts:
            emoji = status_emoji.get(a.status, "⚪️")
            lines.append(f"{emoji} [{a.severity.value}] {a.title} ({a.status.value}, x{a.occurrence_count})")

        await update.message.reply_text("\n".join(lines))


async def cmd_funnel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        result = await run_cycle_once()
    except Exception as exc:
        await update.message.reply_text(f"Не удалось получить данные воронки: {exc}")
        return

    metrics_7d = result.metrics_by_window.get("7d")
    if metrics_7d is None:
        await update.message.reply_text("Данных по воронке нет.")
        return

    lines = ["Воронка (7 дней):"]
    if metrics_7d.clicks is not None:
        lines.append(f"Клики: {metrics_7d.clicks}")
    lines.append(f"Регистрации: {metrics_7d.signup if metrics_7d.signup is not None else '—'}")
    lines.append(f"Активация 1: {metrics_7d.activation_1 if metrics_7d.activation_1 is not None else '—'}")
    lines.append(f"Активация 2: {metrics_7d.activation_2 if metrics_7d.activation_2 is not None else '—'}")
    lines.append(f"Оплаты начаты: {metrics_7d.payment_started if metrics_7d.payment_started is not None else '—'}")
    lines.append(f"Оплаты успешны: {metrics_7d.payment_success if metrics_7d.payment_success is not None else '—'}")

    await update.message.reply_text("\n".join(lines))


async def cmd_test_metrika(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Проверка подключения к Яндекс.Метрике без полного цикла -- использует
    test_metrika_connection() из connectors/metrika.py. Полезно при первой
    настройке live-токенов, чтобы не ждать полного /run для диагностики.

    Запрашивает данные за текущие календарные сутки (UTC), не за скользящее
    окно "последние 24 часа" -- Reports API Метрики работает по датам
    (DateFrom/DateTo), не по точным временным меткам. Если сейчас 23:00 UTC,
    отчёт покроет только последний час суток, не предыдущие 24 часа.
    """
    from app.connectors.metrika import test_metrika_connection

    settings = get_settings()
    await update.message.reply_text("Проверяю подключение к Яндекс.Метрике...")

    result = await test_metrika_connection(
        oauth_token=settings.yandex_oauth_token,
        counter_id=settings.metrika_counter_id,
        goal_ids=settings.metrika_goal_ids,
    )

    if result["ok"]:
        goals_text = "\n".join(f"  {k}: {v}" for k, v in result["goals_found"].items())
        await update.message.reply_text(
            f"Метрика подключена.\n\n"
            f"Визиты (сегодня): {result['traffic']}\n"
            f"Пользователи: {result['users']}\n"
            f"Sampled: {result['sampled']}\n"
            f"Data lag: {result['data_lag']} сек\n\n"
            f"Достижения целей:\n{goals_text}"
        )
    else:
        await update.message.reply_text(
            f"Не удалось подключиться к Метрике.\n"
            f"Этап: {result['stage']}\n"
            f"Ошибка: {result['error']}"
        )


async def cmd_deep_direct(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Принудительный запуск Direct deep diagnostics НЕЗАВИСИМО от primary
    alert. Нужна, потому что обычная кнопка "Проверить глубже" привязана
    к конкретному alert_id и появляется только если этот alert относится к
    триггерящей категории (DEEP_DIAGNOSTICS_TRIGGER_CATEGORIES) -- если
    сейчас главный сигнал продуктовый (например, "Регистрации без
    активации"), кнопки не будет вообще, хотя Direct может быть подключён
    и его стоит проверить отдельно для теста/диагностики.

    Read-only, как и весь Direct-анализ: только granular-отчёты (ad group,
    search query) и поиск находок, никаких изменений в кампаниях.
    """
    settings = get_settings()

    if not (settings.effective_direct_oauth_token and settings.direct_client_login):
        await update.message.reply_text(
            "Директ не настроен (нет OAuth-токена или DIRECT_CLIENT_LOGIN) -- "
            "глубокая диагностика недоступна."
        )
        return

    await update.message.reply_text(
        "Запускаю глубокую диагностику Директа (группы объявлений, поисковые запросы)...\n"
        "Может занять до 30 секунд."
    )

    from app.scheduler import force_refresh_deep_diagnostics
    refresh_result = await force_refresh_deep_diagnostics()

    if not refresh_result["ok"]:
        await update.message.reply_text(f"Не удалось выполнить глубокую проверку: {refresh_result['error']}")
        return

    with get_session() as session:
        project = _get_active_project(session)
        project_name = project.name if project else "Проект"

    details_text = format_deep_diagnostics_details(refresh_result["result"], project_name)

    # Кнопка "Подготовить минус-фразы" имеет смысл и здесь, если находка --
    # irrelevant_query_cluster, поэтому используем тот же конструктор
    # клавиатуры, что и для обычной детальной диагностики. alert_id здесь
    # не привязан к реальному Alert -- ставим 0 как заглушку, потому что
    # "Создать задачу"/"Отклонить" в контексте ручной диагностики без
    # привязки к конкретному алерту неприменимы так же буквально; кнопка
    # минус-фраз использует project-контекст из кэша, не alert_id напрямую.
    if refresh_result["result"].get("main_finding", {}).get("finding_type") == "irrelevant_query_cluster":
        from app.diagnostics import get_query_clusters
        with get_session() as session:
            project = _get_active_project(session)
            project_query_clusters = get_query_clusters(project.settings_json if project else {})
        suggestion_text = format_negative_keywords_suggestion(refresh_result["result"], project_query_clusters)
        await update.message.reply_text(details_text)
        await update.message.reply_text(suggestion_text)
    else:
        await update.message.reply_text(details_text)


async def cmd_test_direct(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Проверка подключения к Яндекс.Директу без полного цикла -- использует
    test_direct_connection() из connectors/direct.py.
    """
    from app.connectors.direct import test_direct_connection

    settings = get_settings()
    await update.message.reply_text("Проверяю подключение к Яндекс.Директу (может занять до 30 секунд)...")

    result = await test_direct_connection(
        oauth_token=settings.effective_direct_oauth_token,
        client_login=settings.direct_client_login,
        campaign_ids=settings.direct_campaign_ids_list,
        sandbox=settings.direct_sandbox,
    )

    if result["ok"]:
        await update.message.reply_text(
            f"Директ подключен.\n\n"
            f"Расход (сегодня): {result['spend']} ₽\n"
            f"Клики: {result['clicks']}\n"
            f"Показы: {result['impressions']}\n"
            f"Кампаний в отчёте: {result['campaigns_count']}"
        )
    else:
        await update.message.reply_text(
            f"Не удалось подключиться к Директу.\n"
            f"Этап: {result['stage']}\n"
            f"Ошибка: {result['error']}"
        )


async def cmd_debug_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Техническая команда -- показывает raw payload_json алерта. Не для
    обычного использования, для дебага. Не подключена к кнопкам.
    """
    if not context.args:
        await update.message.reply_text("Использование: /debug_alert <id>")
        return
    try:
        alert_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return

    with get_session() as session:
        alert = session.get(Alert, alert_id)
        if alert is None:
            await update.message.reply_text(f"Алерт {alert_id} не найден.")
            return

        lines = [
            f"Alert #{alert.id}",
            f"fingerprint: {alert.fingerprint}",
            f"category: {alert.category.value}",
            f"severity: {alert.severity.value}",
            f"confidence: {alert.confidence.value}",
            f"status: {alert.status.value}",
            f"occurrence_count: {alert.occurrence_count}",
            f"escalation_level: {alert.escalation_level}",
            "",
            "payload_json:",
        ]
        lines.extend(f"  {k}: {v}" for k, v in alert.payload_json.items())
        await update.message.reply_text("\n".join(lines))


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with get_session() as session:
        project = _get_active_project(session)
        if project is None:
            await update.message.reply_text("Активный проект не найден.")
            return
        mode = project.settings_json.get("mode", "watch_only")
        await update.message.reply_text(
            f"Текущий режим: {mode}\n\n"
            "Доступные режимы (в v1 активен только watch_only):\n"
            "• watch_only — только смотреть (активен)\n"
            "• recommend_only — предлагать (скоро)\n"
            "• approval_required — делать с подтверждением (скоро)\n"
            "• autopilot_limited — автопилот в лимитах (скоро)"
        )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    with get_session() as session:
        project = _get_active_project(session)
        if project is None:
            await update.message.reply_text("Активный проект не найден.")
            return

        lines = [
            f"Проект: {project.name}",
            f"Тип: {project.type}",
            f"Connector: {project.connector_name}",
            f"Base URL: {project.base_url or '—'}",
            f"Интервал проверки: {settings.watch_interval_seconds // 3600} ч",
        ]
        await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Отправка автоматических уведомлений (вызывается из scheduler job)
# ---------------------------------------------------------------------------


async def send_cycle_notification(app: Application, result: CycleResult, project_name: str) -> None:
    """
    Отправляет уведомление в Telegram ТОЛЬКО если result.has_notifiable_changes
    True. Вызывается из job в scheduler.py после run_cycle_once(). Помечает
    отправленные алерты статусом sent (если они были open), чтобы отличать
    "создан, но ещё не отправлен" от "отправлен".
    """
    if not result.has_notifiable_changes:
        return

    settings = get_settings()
    if not settings.admin_chat_ids_list:
        logger.warning("No admin chat IDs configured, cannot send notification")
        return

    text = format_cycle_message(result, project_name)

    keyboard = None
    if result.primary_candidate is not None:
        with get_session() as session:
            alert = session.exec(
                select(Alert).where(Alert.fingerprint == result.primary_candidate.fingerprint)
            ).first()
            if alert:
                direct_available = bool(settings.effective_direct_oauth_token and settings.direct_client_login)
                keyboard = build_alert_keyboard(
                    alert.id,
                    has_deep_diagnostics=result.deep_diagnostics is not None,
                    deep_diagnostics_available=direct_available,
                )
                if alert.status == AlertStatus.open:
                    alert.status = AlertStatus.sent
                    alert.sent_at = datetime.now(timezone.utc)
                    session.add(alert)
                    session.commit()

    for chat_id in settings.admin_chat_ids_list:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
        except Exception:
            logger.exception("Failed to send Telegram notification to chat %s", chat_id)


# ---------------------------------------------------------------------------
# Сборка приложения
# ---------------------------------------------------------------------------


def build_application() -> Application:
    settings = get_settings()
    if not settings.bot_token:
        raise ValueError("BOT_TOKEN not set")

    app = Application.builder().token(settings.bot_token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("funnel", cmd_funnel))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("debug_alert", cmd_debug_alert))
    app.add_handler(CommandHandler("test_metrika", cmd_test_metrika))
    app.add_handler(CommandHandler("test_direct", cmd_test_direct))
    app.add_handler(CommandHandler("deep_direct", cmd_deep_direct))
    app.add_handler(CallbackQueryHandler(on_button_press))

    return app
