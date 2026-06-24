"""
Owner Decision Layer for /run.

This module converts already collected/cached metrics into a business-owner
report. It deliberately does not call external APIs, does not read/write DB and
is safe to use inside Telegram formatting paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.rules import MIN_PAYMENT_ATTEMPTS_FOR_PAYMENT_ALERT, NormalizedMetrics


SOURCE_LABELS = {
    "product": "Метрики продукта",
    "metrika": "Яндекс.Метрика",
    "direct": "Яндекс.Директ",
    "yookassa": "ЮKassa / оплаты",
}

BROAD_SINGLE_NEGATIVES = {
    "генерация",
    "сгенерировать",
    "генерировать",
    "текст",
    "текста",
    "пост",
    "поста",
    "посты",
    "онлайн",
    "ии",
    "нейросеть",
    "нейросетью",
}

RELEVANT_QUERY_MARKERS = {
    "telegram",
    "телеграм",
    "tg",
    "канал",
    "канала",
    "канале",
    "автопост",
    "автопостинг",
    "постинг",
    "контент план",
    "ведение канала",
}


@dataclass
class StageDecision:
    stage: str
    main_conclusion: str
    main_action: str
    supporting_checks: list[str]
    do_not_touch: list[str]
    confidence: dict[str, str]


def _as_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def format_dt(dt: datetime | str | None) -> str:
    if dt is None:
        return "неизвестное время"
    if isinstance(dt, str):
        return dt
    aware = _as_aware(dt)
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def format_snapshot_age(snapshot_created_at: datetime | None, now: datetime | None = None) -> str:
    """Human-readable snapshot age for /status. Does not show period_key as age."""
    if snapshot_created_at is None:
        return "возраст неизвестен"
    now = now or datetime.now(timezone.utc)
    snap = _as_aware(snapshot_created_at)
    delta_seconds = max(0, int((now - snap).total_seconds()))
    if delta_seconds < 60:
        return "обновлено только что"
    minutes = delta_seconds // 60
    if minutes < 60:
        return f"обновлено {minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"обновлено {hours} ч назад"
    days = hours // 24
    return f"обновлено {days} дн назад"


def _n(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _pct(numerator: int | float, denominator: int | float) -> str:
    if not denominator:
        return "—"
    return f"{(float(numerator) / float(denominator) * 100):.1f}%"


def determine_stage(metrics: NormalizedMetrics | None) -> StageDecision | None:
    if metrics is None:
        return None

    clicks = _n(metrics.clicks)
    signups = _n(metrics.signup)
    activation_1 = _n(metrics.activation_1)
    activation_2 = _n(metrics.activation_2)
    payment_started = _n(metrics.payment_started)
    payment_success = _n(metrics.payment_success)
    spend = _f(metrics.spend)

    if clicks == 0 and signups == 0:
        return StageDecision(
            stage="трафик и регистрации пока не запущены или не видны в данных",
            main_conclusion="Пока нет достаточного потока данных, чтобы судить о спросе и воронке.",
            main_action="Проверить, что рекламный трафик, лендинг и продуктовые события действительно попадают в аналитику.",
            supporting_checks=[
                "убедиться, что Директ отдаёт клики за выбранный период",
                "проверить, что продуктовый источник отдаёт регистрации и активации",
            ],
            do_not_touch=["не делать выводы о продукте или рекламе без базового трафика"],
            confidence={"acquisition": "low", "payment_problem": "low", "pricing_change": "low"},
        )

    if clicks > 0 and signups == 0:
        return StageDecision(
            stage="трафик есть, регистраций пока нет",
            main_conclusion=(
                f"Есть {clicks} кликов, но регистраций в продукте не видно. Вероятная зона проверки — "
                "связка объявление → лендинг → регистрация или трекинг регистраций."
            ),
            main_action="Пройти путь пользователя с мобильного: объявление → лендинг → регистрация, и проверить событие регистрации.",
            supporting_checks=[
                "сравнить рекламный оффер с первым экраном лендинга",
                "проверить скорость открытия веб-версии и форму регистрации",
                "проверить, не теряется ли UTM/landing_session при переходе",
            ],
            do_not_touch=["не увеличивать бюджет", "не менять цены", "не делать редизайн до проверки базового пути"],
            confidence={"acquisition": "low", "traffic_to_signup_problem": "medium", "payment_problem": "low"},
        )

    if signups > 0 and activation_1 == 0 and activation_2 == 0:
        return StageDecision(
            stage="регистрации пошли, активации пока нет",
            main_conclusion=(
                f"Есть {signups} регистраций, но не видно создания каналов или генераций. "
                "Главная зона проверки — первый экран после регистрации и онбординг."
            ),
            main_action="Проверить путь сразу после регистрации: видит ли пользователь понятный следующий шаг и может ли создать канал/первый пост.",
            supporting_checks=[
                "проверить redirect после регистрации",
                "проверить CTA создания первого канала/поста",
                "посмотреть ошибки фронта/бэка на первом действии после регистрации",
            ],
            do_not_touch=["не чистить рекламу как главную причину", "не менять цены", "не менять лендинг и онбординг одновременно"],
            confidence={"acquisition": "medium", "activation_problem": "medium", "payment_problem": "low"},
        )

    if signups > 0 and (activation_1 > 0 or activation_2 > 0) and payment_success == 0:
        cpa_text = ""
        if spend > 0 and signups > 0:
            cpa_text = f" Расход на регистрацию примерно {spend / signups:.0f} ₽."
        payment_note = ""
        if 0 < payment_started < MIN_PAYMENT_ATTEMPTS_FOR_PAYMENT_ALERT:
            payment_note = (
                f" Есть {payment_started} начатая оплата, но это ранний сигнал, не P1: "
                f"для проблемы платёжного шага нужно минимум {MIN_PAYMENT_ATTEMPTS_FOR_PAYMENT_ALERT} попытки без успеха."
            )
        return StageDecision(
            stage="регистрации и активации пошли, успешных оплат пока нет",
            main_conclusion=(
                f"Привлечение начало работать: есть {signups} регистраций, "
                f"{activation_1} созданных каналов и {activation_2} генераций постов.{cpa_text}{payment_note}"
            ),
            main_action=(
                "Проверить путь от активации к оплате: видит ли пользователь тарифы, понятно ли зачем платить, "
                "нет ли технического разрыва на тарифном или платёжном шаге."
            ),
            supporting_checks=[
                "посмотреть пользователей, которые зарегистрировались, но не создали канал/не сгенерировали пост",
                "посмотреть пользователей, которые создали канал или сгенерировали пост, но не открыли тарифы/оплату",
                "вручную пройти путь оплаты с нового аккаунта и проверить, где появляется paywall/тарифы",
            ],
            do_not_touch=[
                "не менять резко рекламу",
                "не менять резко лендинг",
                "не менять ставки, бюджет, цены и тарифы",
                "не чистить рекламу по единичным низкозатратным запросам",
                "не делать редизайн лендинга без доказанного узкого места",
            ],
            confidence={"acquisition_started": "medium/high", "payment_broken": "low", "pricing_change": "low"},
        )

    if payment_success > 0:
        return StageDecision(
            stage="есть первые успешные оплаты",
            main_conclusion=(
                f"Появились успешные оплаты: {payment_success}. Следующая зона анализа — экономика: CPA, "
                "конверсия в оплату, выручка и окупаемость каналов."
            ),
            main_action="Сравнить стоимость привлечения с выручкой и понять, какие сегменты можно масштабировать без ухудшения качества.",
            supporting_checks=[
                "посчитать CPA до оплаты и примерный payback",
                "посмотреть, какие источники/кампании дали платежи, если атрибуция доступна",
                "отдельно проверить удержание и повторное использование продукта",
            ],
            do_not_touch=["не масштабировать бюджет без проверки payback", "не менять цены без данных по конверсии и отказам"],
            confidence={"acquisition_started": "high", "payment_exists": "medium/high", "pricing_change": "low/medium"},
        )

    return None


def _format_funnel_diagnosis(metrics: NormalizedMetrics) -> str:
    clicks = _n(metrics.clicks)
    signups = _n(metrics.signup)
    activation_1 = _n(metrics.activation_1)
    activation_2 = _n(metrics.activation_2)
    payment_started = _n(metrics.payment_started)
    payment_success = _n(metrics.payment_success)

    lines = ["Воронка:"]
    lines.append(f"— клики → регистрации: {clicks} → {signups} ({_pct(signups, clicks)}).")
    if signups > 0:
        lines.append(f"— регистрации → создан канал: {signups} → {activation_1} ({_pct(activation_1, signups)}).")
        lines.append(f"— регистрации → генерации постов: {signups} → {activation_2} событий ({_pct(activation_2, signups)} как событий к регистрациям).")
        lines.append(f"— регистрации → начатая оплата: {signups} → {payment_started} ({_pct(payment_started, signups)}).")
    else:
        lines.append("— регистрации → активация/оплата: нет регистраций для расчёта.")

    if payment_started > 0:
        lines.append(f"— начатая оплата → успешная оплата: {payment_started} → {payment_success} ({_pct(payment_success, payment_started)}).")
    else:
        lines.append("— начатая оплата → успешная оплата: попыток оплаты пока нет.")

    if payment_started and payment_started < MIN_PAYMENT_ATTEMPTS_FOR_PAYMENT_ALERT and payment_success == 0:
        lines.append("Оценка: платёжный шаг нужно проверить руками, но данных пока мало для вывода, что он сломан.")
    elif payment_started >= MIN_PAYMENT_ATTEMPTS_FOR_PAYMENT_ALERT and payment_success == 0:
        lines.append("Оценка: вероятная зона проблемы — платёжный шаг, потому что попыток уже достаточно для сигнала.")
    elif signups > 0 and (activation_1 > 0 or activation_2 > 0):
        lines.append("Оценка: ранняя воронка живая; главный вопрос — переход к оплате и качество активированных пользователей.")

    return "\n".join(lines)


def _format_deltas(previous_metrics: dict | None, metrics: NormalizedMetrics) -> str:
    if not previous_metrics:
        return "Что изменилось с прошлого замера:\nДинамику пока не показываю: нет сопоставимого предыдущего замера."

    current = {
        "регистрации": _n(metrics.signup),
        "созданные каналы": _n(metrics.activation_1),
        "генерации постов": _n(metrics.activation_2),
        "начатые оплаты": _n(metrics.payment_started),
        "успешные оплаты": _n(metrics.payment_success),
    }
    previous = {
        "регистрации": _n(previous_metrics.get("signup")),
        "созданные каналы": _n(previous_metrics.get("activation_1")),
        "генерации постов": _n(previous_metrics.get("activation_2")),
        "начатые оплаты": _n(previous_metrics.get("payment_started")),
        "успешные оплаты": _n(previous_metrics.get("payment_success")),
    }
    lines = ["Что изменилось с прошлого замера:"]
    for label, value in current.items():
        delta = value - previous[label]
        sign = "+" if delta >= 0 else ""
        lines.append(f"— {label}: {sign}{delta}.")

    current_spend = _f(metrics.spend)
    current_signups = _n(metrics.signup)
    prev_spend = _f(previous_metrics.get("spend"))
    prev_signups = _n(previous_metrics.get("signup"))
    if current_spend and current_signups and prev_spend and prev_signups:
        cpa = current_spend / current_signups
        prev_cpa = prev_spend / prev_signups
        lines.append(f"— CPA: {prev_cpa:.0f} ₽ → {cpa:.0f} ₽.")
    return "\n".join(lines)


def _status_text(status_info: dict | None, fallback_status: str | None = None) -> str:
    status_info = status_info or {}
    status = status_info.get("status") or fallback_status
    if status == "fresh":
        return "свежие данные текущего запуска"
    if status == "stale":
        cached_at = status_info.get("snapshot_created_at")
        return f"кэш от {format_dt(cached_at)}"
    if status == "unavailable":
        error = status_info.get("error")
        return f"недоступно или не настроено{f' ({error})' if error else ''}"
    return "статус не определён"


def format_source_freshness(source_statuses: dict | None) -> str:
    source_statuses = source_statuses or {}
    lines = ["Свежесть данных:"]
    for source, label in SOURCE_LABELS.items():
        lines.append(f"— {label}: {_status_text(source_statuses.get(source))}.")
    return "\n".join(lines)


def _query_is_safe_phrase(query: str) -> bool:
    q = (query or "").strip().lower()
    if not q:
        return False
    if q in BROAD_SINGLE_NEGATIVES:
        return False
    if len(q.split()) < 2:
        return False
    if any(marker in q for marker in RELEVANT_QUERY_MARKERS):
        return False
    return True


def safe_phrase_negative_candidates(queries: list[str], limit: int = 5) -> list[str]:
    result: list[str] = []
    seen = set()
    for query in queries:
        q = (query or "").strip()
        q_lower = q.lower()
        if q_lower in seen:
            continue
        if not _query_is_safe_phrase(q):
            continue
        seen.add(q_lower)
        result.append(q)
        if len(result) >= limit:
            break
    return result


def _finding_payload(finding: dict) -> dict:
    return finding.get("payload") or {}


def _format_direct_decision_layer(deep_diagnostics: dict | None) -> str:
    lines = ["Запросы и Директ:"]
    if not deep_diagnostics:
        lines.append("— кэш глубокой диагностики запросов не приложен к этому /run; не делаю выводы по отдельным запросам.")
        lines.append("— сейчас правило: не чистить рекламу по единичным низкозатратным запросам; для ручной проверки используйте /deep_direct.")
        return "\n".join(lines)

    if deep_diagnostics.get("insufficient_data"):
        lines.append("— данных по поисковым запросам пока мало для уверенной чистки или масштабирования.")
        return "\n".join(lines)

    good = deep_diagnostics.get("good_findings") or []
    findings = deep_diagnostics.get("findings") or []
    known_risks = deep_diagnostics.get("known_risks") or []

    scale = good[:2]
    clean = [f for f in findings if f.get("finding_type") == "irrelevant_query_cluster" and f.get("severity") in ("P1", "P2")]
    observe = [f for f in findings if f.get("finding_type") == "irrelevant_query_cluster" and f.get("severity") == "info"]

    if scale:
        lines.append("— что можно расширять: релевантные кластеры с Telegram/SMM-интентом.")
        for f in scale:
            payload = _finding_payload(f)
            lines.append(f"  • {f.get('title')}: {payload.get('clicks', 0)} кликов, {payload.get('cost', 0)} ₽.")
    else:
        lines.append("— что масштабировать: явного кластера для расширения в кэше нет.")

    if clean:
        lines.append("— что чистить: только повторяющиеся/весомые нерелевантные кластеры, не одиночный шум.")
        for f in clean[:2]:
            payload = _finding_payload(f)
            candidates = safe_phrase_negative_candidates(payload.get("top_queries") or [])
            suffix = f" Безопасные фразовые кандидаты: {', '.join(candidates)}." if candidates else " Безопасных фразовых кандидатов без ручной проверки нет."
            lines.append(
                f"  • {f.get('title')}: {payload.get('clicks', 0)} кликов, {payload.get('cost', 0)} ₽, "
                f"доля расхода {int(_f(payload.get('cost_share')) * 100)}%.{suffix}"
            )
    else:
        lines.append("— что чистить: срочной чистки по кэшу нет.")

    if observe:
        top = observe[0]
        payload = _finding_payload(top)
        lines.append(
            f"— что наблюдать: {top.get('title')} — {payload.get('clicks', 0)} кликов, {payload.get('cost', 0)} ₽; "
            "бизнес-вес низкий, не делать главным выводом."
        )

    if known_risks:
        lines.append(f"— известные риски: {len(known_risks)} старых сигнала в кэше, не поднимаю их как главный вывод без роста веса.")

    lines.append("— что не минусовать: одиночные широкие слова вроде “генерация”, “текст”, “поста”, “онлайн”, а также запросы с Telegram/канал-интентом.")
    lines.append("— важное ограничение: сквозной атрибуции запрос → регистрация/оплата пока нет, поэтому минус-фразы только после ручной проверки.")
    return "\n".join(lines)


def _format_confidence(confidence: dict[str, str]) -> str:
    if not confidence:
        return "Уверенность:\n— выводы предварительные: данных пока мало."
    labels = {
        "acquisition_started": "что привлечение начало работать",
        "payment_broken": "что платёжный шаг сломан",
        "pricing_change": "что нужно менять цены/тарифы",
        "traffic_to_signup_problem": "что проблема в переходе к регистрации",
        "activation_problem": "что проблема в активации",
        "payment_exists": "что оплаты уже появились",
        "acquisition": "что привлечение работает",
    }
    lines = ["Уверенность:"]
    for key, value in confidence.items():
        lines.append(f"— {labels.get(key, key)}: {value}.")
    return "\n".join(lines)


def _format_metrics_line(metrics: NormalizedMetrics) -> str:
    spend = _f(metrics.spend)
    clicks = _n(metrics.clicks)
    ctr = metrics.ctr
    ctr_text = f" / CTR {float(ctr):.1f}%" if ctr is not None else ""
    return "\n".join([
        f"Реклама: {spend:.0f} ₽ / {clicks} кликов{ctr_text}",
        (
            f"Продукт: {_n(metrics.signup)} регистраций / {_n(metrics.activation_1)} создали канал / "
            f"{_n(metrics.activation_2)} генераций постов / {_n(metrics.payment_success)} успешных оплат"
        ),
    ])


def build_owner_report(
    project_name: str,
    metrics: NormalizedMetrics | None,
    *,
    source_statuses: dict | None = None,
    previous_metrics: dict | None = None,
    deep_diagnostics: dict | None = None,
    period_label: str = "7д",
    preface: str | None = None,
) -> str | None:
    decision = determine_stage(metrics)
    if metrics is None or decision is None:
        return None

    blocks: list[str] = []
    if preface:
        blocks.append(preface)

    blocks.append("\n".join([
        "Аналитик Воронки — проверка бизнеса",
        f"Проект: {project_name}",
        "",
        "Стадия:",
        decision.stage.capitalize() + ".",
        "",
        "Главный вывод:",
        decision.main_conclusion,
    ]))

    blocks.append(_format_funnel_diagnosis(metrics))

    action_lines = ["Что сделать сейчас:", "1. " + decision.main_action]
    for i, check in enumerate(decision.supporting_checks[:3], start=2):
        action_lines.append(f"{i}. {check}.")
    blocks.append("\n".join(action_lines))

    blocks.append("Что не трогать:\n" + "\n".join(f"— {item}." for item in decision.do_not_touch))
    blocks.append(_format_direct_decision_layer(deep_diagnostics))
    blocks.append(_format_deltas(previous_metrics, metrics))
    blocks.append(_format_confidence(decision.confidence))
    blocks.append(f"Метрики ({period_label}):\n{_format_metrics_line(metrics)}")
    blocks.append(format_source_freshness(source_statuses))

    return "\n\n".join(blocks)
