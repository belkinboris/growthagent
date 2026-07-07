"""
Commercial Reporting Layer v2 — Growth Agent / Аналитик Воронки.

Этот модуль отвечает за весь owner-facing текст в командах /run, /ads,
/funnel, /pay, /deep_direct.

Правила:
  — Никакого технического жаргона: нет legacy, fallback, watch, winners,
    protected, payment flow, per-query attribution, GoalId, Direct Intelligence,
    cache, live collection, SEARCH_QUERY_PERFORMANCE_REPORT, backend, UTC,
    pricing_viewed, payment_cta_clicked, payment_started, payment_success.
  — Время — в бизнес-таймзоне (МСК).
  — Числа — без лишних знаков, понятно с первого взгляда.
  — Эмодзи для навигации и ключевых блоков.

Технические данные (кэши, ORM, DiagnosticsResult и т.д.) принимаются как
аргументы, но в текст не просачиваются.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.rules import NormalizedMetrics
    from app.query_classifier import DirectIntelligenceResult, QueryLabel


# ---------------------------------------------------------------------------
# Время в МСК
# ---------------------------------------------------------------------------

_MSK = timezone(timedelta(hours=3))

def _now_msk() -> datetime:
    return datetime.now(_MSK)

def _fmt_dt_msk(dt: datetime | None) -> str:
    """Возвращает дату-время в русском формате МСК."""
    if dt is None:
        return "неизвестно"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    msk = dt.astimezone(_MSK)
    months = [
        "", "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    ]
    return f"{msk.day} {months[msk.month]} {msk.year} года, {msk.strftime('%H:%M')} МСК"

def _data_age_minutes(dt: datetime | None) -> Optional[float]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60


# ---------------------------------------------------------------------------
# Хелперы форматирования чисел
# ---------------------------------------------------------------------------

def _n(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0

def _plural(n: int, form1: str, form2: str, form5: str) -> str:
    """Склонение по числительному: 1 регистрация, 2 регистрации, 5 регистраций."""
    n = abs(n) % 100
    if 10 <= n <= 20:
        return form5
    n = n % 10
    if n == 1:
        return form1
    if 2 <= n <= 4:
        return form2
    return form5

def _pct(a, b) -> str:
    if not b:
        return "—"
    return f"{int(a / b * 100)}%"


def progress_bar(current: int, target: int, width: int = 10) -> str:
    """
    Текстовая шкала прогресса, plain text (без markdown).

    progress_bar(0, 30)  -> "[░░░░░░░░░░] 0%"
    progress_bar(15, 30) -> "[█████░░░░░] 50%"
    progress_bar(30, 30) -> "[██████████] 100%"
    progress_bar(40, 30) -> "[██████████] 100%" (не превышает 100%, даже если current > target)

    target <= 0 трактуется как "цель не задана" — возвращает пустую шкалу 0%
    без деления на ноль.
    """
    current = max(0, int(current or 0))
    target = int(target or 0)
    if target <= 0:
        filled = 0
        percent = 0
    else:
        ratio = min(current / target, 1.0)
        filled = round(ratio * width)
        percent = int(ratio * 100)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {percent}%"


# ---------------------------------------------------------------------------
# /run — главный владельческий отчёт
# ---------------------------------------------------------------------------

def build_run_report(
    project_name: str,
    metrics: "NormalizedMetrics | None",
    *,
    payment_path: dict | None = None,
    direct_intelligence: "DirectIntelligenceResult | None" = None,
    snapshot_dt: datetime | None = None,
    is_fallback: bool = False,
) -> str:
    """
    Главный owner-level отчёт. Показывается по команде /run.
    Никакого технического языка.
    """
    now_str = _fmt_dt_msk(_now_msk())
    lines: list[str] = []

    # Заголовок
    lines.append(f"📊 Аналитик Воронки — {project_name}")
    lines.append(f"Данные: {now_str}")
    lines.append("Период: последние 7 дней")

    # Плашка если используем сохранённый замер
    if is_fallback and snapshot_dt is not None:
        age = _data_age_minutes(snapshot_dt)
        if age is not None and age < 180:  # < 3 часов — данные свежие
            lines.append(f"\nИспользован последний сохранённый замер. Технические детали: /status")
        else:
            age_hours = int(age / 60) if age else 0
            lines.append(f"\n⚠️ Данные могут быть устаревшими: последний замер {age_hours} ч. назад.")

    if metrics is None:
        lines.append("\nДанных пока нет. Запустите /run ещё раз через несколько минут.")
        return "\n".join(lines)

    signup = _n(metrics.signup)
    activation_1 = _n(metrics.activation_1)
    activation_2 = _n(metrics.activation_2)
    payment_started = _n(metrics.payment_started)
    payment_success = _n(metrics.payment_success)
    clicks = _n(metrics.clicks)
    spend = float(metrics.spend or 0)

    # Данные по тарифам из payment_path.
    # pricing_viewed_raw=None означает "событие не настроено/нет данных".
    # pricing_viewed_raw=int означает реальное значение (0 = настроено, но никто не открывал).
    pricing_viewed_raw = payment_path.get("pricing_viewed") if payment_path else None
    pricing_viewed_tracked = pricing_viewed_raw is not None  # событие отслеживается
    pricing_viewed = _n(pricing_viewed_raw) if pricing_viewed_raw is not None else 0
    pp_payment_started = _n(payment_path.get("payment_started")) if payment_path else payment_started
    pp_payment_success = _n(payment_path.get("payment_success")) if payment_path else payment_success

    MIN_PRICING_FOR_CONCLUSION = 5

    # ── Главный вывод ────────────────────────────────────────────────────
    lines.append("\n🎯 Главный вывод:")
    if signup == 0:
        lines.append(
            "Регистраций пока нет. Реклама только запущена или ещё не приводит пользователей."
        )
    elif activation_1 == 0:
        lines.append(
            f"Реклама даёт регистрации ({signup}), но никто не создал канал. "
            "Главный вопрос — что происходит сразу после регистрации."
        )
    elif not pricing_viewed_tracked and pp_payment_started == 0:
        lines.append(
            "Реклама начала приводить пользователей, и они активно создают каналы. "
            "Пока неизвестно, доходят ли они до тарифов — это событие не отслеживается."
        )
    elif pricing_viewed_tracked and pricing_viewed < MIN_PRICING_FOR_CONCLUSION and pp_payment_started == 0:
        lines.append(
            "Реклама начала приводить пользователей, и они активно создают каналы. "
            "Главная проблема сейчас ниже по воронке: пользователи почти не доходят до тарифов и оплаты."
        )
    elif pricing_viewed >= MIN_PRICING_FOR_CONCLUSION and pp_payment_started == 0:
        lines.append(
            "Реклама работает, активация хорошая. "
            "Пользователи видят тарифы, но пока не пытаются оплатить. "
            "Вероятная зона — тарифный экран: ценность, цена, момент предложения."
        )
    elif pp_payment_started > 0 and pp_payment_success == 0:
        lines.append(
            f"Реклама работает, пользователи доходят до оплаты. "
            f"Есть попытки оплаты ({pp_payment_started}), но пока без успеха. "
            "Стоит проверить платёжный шлюз."
        )
    else:
        lines.append(
            "Есть регистрации и оплаты. Реклама и воронка работают. "
            "Следующий фокус — экономика: стоимость привлечения и окупаемость."
        )

    # ── Что сейчас не так ────────────────────────────────────────────────
    lines.append("\n❗ Что сейчас не так:")
    issues: list[str] = []

    if signup > 0 and activation_1 < signup * 0.5:
        issues.append(
            f"Только {activation_1} из {signup} зарегистрировавшихся создали канал — "
            "высокий отвал после регистрации."
        )

    if activation_1 > 0:
        if not pricing_viewed_tracked:
            issues.append(
                "Пользователи создают каналы. "
                "Пока неизвестно, доходят ли они до тарифов — просмотр тарифов не отслеживается."
            )
        elif pricing_viewed < MIN_PRICING_FOR_CONCLUSION:
            issues.append(
                "Пользователи создают каналы, но почти не открывают тарифы."
            )
        elif pricing_viewed >= MIN_PRICING_FOR_CONCLUSION and pp_payment_started == 0:
            issues.append(
                f"Пользователи открывали тарифы {pricing_viewed} раз, но ни разу не начали оплату. "
                "Возможно, не хватает явной причины платить прямо сейчас."
            )

    if pp_payment_started == 0 and pp_payment_success == 0:
        issues.append(
            "Попыток оплаты нет — платёжный шлюз пока не главный подозреваемый."
        )
    elif pp_payment_started > 0 and pp_payment_success == 0:
        from app.query_classifier import ActionType  # noqa — избегаем circular
        issues.append(
            f"Есть {pp_payment_started} попыток оплаты без успеха — стоит проверить платёжный шлюз."
        )

    if spend > 0 and signup > 0:
        cpa = spend / signup
        if cpa > 500:
            issues.append(f"Стоимость регистрации высокая: {cpa:.0f} ₽.")
    elif spend > 500 and signup == 0:
        issues.append(f"Потрачено {spend:.0f} ₽, регистраций пока нет.")

    # Safe negatives из рекламы
    safe_negs: list[str] = []
    if direct_intelligence and direct_intelligence.safe_negatives:
        safe_negs = [q.query for q in direct_intelligence.safe_negatives[:3]]

    if safe_negs:
        issues.append(
            f"В рекламе есть явно нерелевантные запросы: {', '.join(f'«{q}»' for q in safe_negs)}."
        )

    if not issues:
        issues.append("Явных критических проблем не видно.")

    for issue in issues:
        lines.append(f"— {issue}")

    # ── Что сделать сегодня ───────────────────────────────────────────────
    lines.append("\n✅ Что сделать сегодня:")

    # Product
    if activation_1 > 0 and not pricing_viewed_tracked:
        product_action = (
            "Настроить отслеживание просмотра тарифов, чтобы понять, "
            "доходят ли пользователи до экрана оплаты."
        )
    elif activation_1 > 0 and pricing_viewed_tracked and pricing_viewed < MIN_PRICING_FOR_CONCLUSION:
        product_action = (
            "Пройти путь от создания канала до тарифов самому. "
            "Проверить, где пользователь видит предложение оплатить и понимает ли, зачем платить сейчас."
        )
    elif pricing_viewed >= MIN_PRICING_FOR_CONCLUSION and pp_payment_started == 0:
        product_action = (
            "Проверить тарифный экран: что видит пользователь, понятна ли ценность, "
            "есть ли причина платить прямо сейчас."
        )
    else:
        product_action = "Следить за воронкой, собирать данные по пути к оплате."
    lines.append(f"— 🛠 Продукт: {product_action}")

    # Ads
    if safe_negs:
        ads_action = (
            f"Рассмотреть минус-фразы для очевидного мусора: {', '.join(f'«{q}»' for q in safe_negs[:2])}. "
            "Ставки и бюджет не трогать резко."
        )
    else:
        ads_action = "Не менять ставки и бюджет резко. Очевидного рекламного мусора сейчас не видно."
    lines.append(f"— 📢 Реклама: {ads_action}")

    # Payment
    if pp_payment_started == 0:
        payment_action = "YooKassa пока не чинить — попыток оплаты не было."
    elif pp_payment_started > 0 and pp_payment_success == 0:
        payment_action = "Проверить платёжный шлюз: есть попытки оплаты без успеха."
    else:
        payment_action = "Платёжная воронка работает. Следить за качеством."
    lines.append(f"— 💳 Оплата: {payment_action}")

    # Data
    lines.append("— 📈 Данные: продолжать собирать события по тарифам и оплате.")

    # ── Что не трогать ───────────────────────────────────────────────────
    lines.append("\n🚫 Что не трогать:")
    do_not_touch = ["цены", "тарифы", "бесплатная квота", "лендинг", "рекламный бюджет", "ставки"]
    for item in do_not_touch:
        lines.append(f"— {item}")

    # ── Ключевые числа ───────────────────────────────────────────────────
    lines.append("\n📊 Ключевые числа:")
    if clicks:
        lines.append(f"— {clicks} кликов из рекламы")
    reg_word = _plural(signup, "регистрация", "регистрации", "регистраций")
    lines.append(f"— {signup} {reg_word}")
    lines.append(f"— {activation_1} создали канал")
    if payment_path and pricing_viewed_tracked:
        pv_word = _plural(pricing_viewed, "открытие тарифов", "открытия тарифов", "открытий тарифов")
        lines.append(f"— {pricing_viewed} {pv_word}")
    lines.append(f"— {pp_payment_started} попыток оплаты")
    lines.append(f"— {pp_payment_success} успешных оплат")
    if spend:
        lines.append(f"— {spend:.0f} ₽ потрачено на рекламу")

    # ── Уверенность ──────────────────────────────────────────────────────
    lines.append("\n🔍 Уверенность:")
    if signup > 5:
        lines.append("— реклама даёт регистрации: средняя/высокая")
    else:
        lines.append("— реклама даёт регистрации: пока мало данных")

    if pp_payment_started >= 3 and pp_payment_success == 0:
        lines.append("— проблема в платёжном шлюзе: средняя (несколько попыток без успеха)")
    else:
        lines.append("— проблема в платёжном шлюзе: низкая — попыток оплаты не было")

    lines.append("— нужно менять цены прямо сейчас: низкая")

    if activation_1 > 0 and pricing_viewed_tracked and pricing_viewed < MIN_PRICING_FOR_CONCLUSION:
        lines.append("— нужно проверить путь к тарифам: высокая")
    elif activation_1 > 0 and not pricing_viewed_tracked:
        lines.append("— нужно настроить отслеживание тарифов: высокая")

    # ── Итог ─────────────────────────────────────────────────────────────
    lines.append("\n💡 Итог:")
    if activation_1 > 0 and pricing_viewed_tracked and pricing_viewed < MIN_PRICING_FOR_CONCLUSION:
        lines.append(
            "Следующая задача не «чинить оплату» и не «чистить рекламу» — "
            "а проверить путь от полученной ценности к тарифам."
        )
    elif activation_1 > 0 and not pricing_viewed_tracked:
        lines.append(
            "Следующая задача — настроить отслеживание просмотра тарифов, "
            "чтобы понять, где пользователи уходят из воронки."
        )
    elif pricing_viewed >= MIN_PRICING_FOR_CONCLUSION and pp_payment_started == 0:
        lines.append("Следующая задача — улучшить тарифный экран: ценность, цена, момент предложения.")
    elif pp_payment_started > 0 and pp_payment_success == 0:
        lines.append("Следующая задача — разобраться с платёжным шлюзом.")
    else:
        lines.append("Продолжать собирать данные и наблюдать за воронкой.")

    # ── Подробности ───────────────────────────────────────────────────────
    lines.append("\n📋 Подробности:")
    lines.append("— реклама: /ads")
    lines.append("— воронка: /funnel")
    lines.append("— оплата: /pay")
    lines.append("— технический статус: /status")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /ads — рекламный вывод
# ---------------------------------------------------------------------------

def build_ads_report(
    project_name: str,
    *,
    direct_intelligence: "DirectIntelligenceResult | None" = None,
    metrics: "NormalizedMetrics | None" = None,
    snapshot_dt: datetime | None = None,
) -> str:
    """Рекламный отчёт без технического жаргона."""
    now_str = _fmt_dt_msk(_now_msk())
    lines: list[str] = []

    lines.append(f"📢 Реклама — {project_name}")
    lines.append(f"Данные: {now_str}")
    lines.append("Период: 7 дней")

    spend = float(metrics.spend or 0) if metrics else 0.0
    clicks = _n(metrics.clicks) if metrics else 0
    signup = _n(metrics.signup) if metrics else 0
    cpa = spend / signup if signup > 0 else None

    # Общий вывод
    lines.append("\nВывод:")
    if direct_intelligence is None:
        lines.append(
            "Данные по поисковым запросам пока не собраны. "
            "Запустите /deep_direct для обновления."
        )
    else:
        has_negatives = bool(direct_intelligence.safe_negatives)
        if cpa and cpa < 300:
            quality = "Реклама даёт регистрации по хорошей цене"
        elif cpa and cpa < 600:
            quality = "Реклама даёт регистрации по приемлемой цене"
        elif signup > 0:
            quality = "Реклама даёт регистрации"
        else:
            quality = "Регистраций пока нет"

        if has_negatives:
            lines.append(
                f"{quality}. "
                "Среди поисковых запросов есть явно нерелевантные — их можно добавить в минус-фразы."
            )
        else:
            lines.append(
                f"{quality}. "
                "Поисковые запросы в основном близки к продукту. "
                "Сильного мусора с достаточным сигналом сейчас не видно."
            )

    if spend or clicks:
        cpa_str = f" / цена регистрации: {cpa:.0f} ₽" if cpa else ""
        lines.append(f"\nЦифры: {spend:.0f} ₽ / {clicks} кликов{cpa_str}")

    # Запросы
    if direct_intelligence is not None:
        # Что оставить — только DO_NOT_TOUCH запросы БЕЗ garbage_category
        # (garbage_category означает что запрос мусорный, пусть и с защищённым термином)
        good_queries = [
            q for q in direct_intelligence.do_not_touch
            if not q.garbage_category  # исключаем обход ограничений, шапки и т.п.
        ][:5]
        if good_queries:
            lines.append("\n✅ Что оставить:")
            for q in good_queries:
                lines.append(f"— «{q.query}»")
        else:
            lines.append("\n✅ Что оставить:")
            lines.append("— запросы про генерацию постов и ведение каналов в Telegram")
            lines.append("— запросы про ИИ для Telegram")
            lines.append("— запросы про автопостинг")

        # Что проверить
        watch_top = [w for w in direct_intelligence.watch if w.cost > 0][:5]
        if watch_top:
            lines.append("\n🔍 Что проверить:")
            for q in watch_top:
                lines.append(f"— «{q.query}» ({q.clicks} кл., {q.cost:.0f} ₽)")

        # Что исключить
        if direct_intelligence.safe_negatives:
            lines.append("\n🗑 Что можно исключить:")
            for q in direct_intelligence.safe_negatives[:5]:
                lines.append(f"— «{q.query}»")
            if len(direct_intelligence.safe_negatives) > 5:
                lines.append(
                    f"  ... и ещё {len(direct_intelligence.safe_negatives) - 5} запросов. "
                    "Добавлять в минус-фразы только точные формулировки, не широкие слова."
                )
        else:
            lines.append("\n🗑 Что можно исключить:")
            lines.append("Очевидных минус-фраз с достаточным сигналом не найдено.")

        # Оговорка без технического языка
        lines.append(
            "\nТочную связь каждого запроса с регистрацией сейчас определить нельзя — "
            "выводы по запросам осторожные."
        )

        lines.append(
            f"\nПроанализировано {direct_intelligence.total_queries_analyzed} запросов "
            f"за период."
        )

    lines.append("\nДоска: /board")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /funnel — продуктовая воронка
# ---------------------------------------------------------------------------

def _format_new_product_signals(payment_path: dict | None, skip_feedback_summary: bool = False) -> str:
    """
    Форматирует блок новых ProductEvent сигналов: onboarding choice,
    first post feedback, breakdown генераций.

    Правила:
    - Если все новые поля None/0 или payment_path=None — компактная фраза
      "Новые сигналы ещё не накопились после деплоя."
    - Без технических названий событий (нет onboarding_choice_selected и т.д.)
    - Причины "не подошёл" показываем только если first_post_feedback_bad > 0
    - Breakdown verified/unverified — только если хотя бы одно поле не None

    skip_feedback_summary: если True, не дублирует "Первый пост подошёл/не подошёл" —
    используется когда /funnel уже показал эти числа в "Шаги воронки".
    Причины "не подошёл" показываются в любом случае, если есть.
    """
    if payment_path is None:
        return ""

    # Читаем новые поля — все опциональные
    choice_counts = payment_path.get("onboarding_choice_counts")   # dict | None
    fb_good = payment_path.get("first_post_feedback_good")          # int | None
    fb_bad = payment_path.get("first_post_feedback_bad")            # int | None
    fb_reasons = payment_path.get("first_post_feedback_reasons")    # dict | None
    gen_verified = payment_path.get("post_generations_verified")    # int | None
    gen_unverified = payment_path.get("post_generations_unverified") # int | None
    queue_shown = payment_path.get("queue_offer_shown")              # int | None
    queue_clicked = payment_path.get("queue_offer_clicked")          # int | None

    # Определяем есть ли хоть что-то новое
    has_choice = bool(choice_counts)
    has_feedback = fb_good is not None or fb_bad is not None
    has_gen_breakdown = gen_verified is not None or gen_unverified is not None
    has_queue = queue_shown is not None or queue_clicked is not None

    if not has_choice and not has_feedback and not has_gen_breakdown and not has_queue:
        return "\nНовые сигналы ещё не накопились после деплоя."

    lines = ["\nНовые сигналы:"]

    # Onboarding choice
    if has_choice and isinstance(choice_counts, dict):
        generate = _n(choice_counts.get("generate_post") or choice_counts.get("first_post"))
        analyze = _n(choice_counts.get("analyze_channel") or choice_counts.get("channel_analysis"))
        skip = _n(choice_counts.get("skip") or choice_counts.get("skipped"))
        if generate or analyze or skip:
            lines.append(f"— Сгенерировать первый пост: {generate}")
            lines.append(f"— Проанализировать канал: {analyze}")
            lines.append(f"— Пропустить онбординг: {skip}")

    # First post feedback
    if has_feedback:
        if not skip_feedback_summary:
            if fb_good is not None:
                lines.append(f"— Первый пост подошёл: {_n(fb_good)}")
            if fb_bad is not None:
                lines.append(f"— Первый пост не подошёл: {_n(fb_bad)}")

        # Причины — только если есть отрицательные отзывы
        if fb_bad and _n(fb_bad) > 0 and fb_reasons and isinstance(fb_reasons, dict):
            # Человекочитаемые названия причин
            reason_labels = {

                "too_generic":      "Слишком общий",
                "wrong_style":      "Не тот стиль",
                "wrong_topic":      "Не про тему",
                "too_dry":          "Слишком сухо",
                "too_promotional":  "Слишком рекламно",
                "other":            "Другое",
                # Возможные альтернативные ключи от AutoPost
                "not_my_style":     "Не тот стиль",
                "off_topic":        "Не про тему",
                "too_salesy":       "Слишком рекламно",
                "too_formal":       "Слишком сухо",
            }
            shown: set[str] = set()  # дедупликация если несколько ключей → одна метка
            reasons_lines: list[str] = []
            for key, count in fb_reasons.items():
                if _n(count) == 0:
                    continue
                label = reason_labels.get(key)
                if label and label not in shown:
                    shown.add(label)
                    reasons_lines.append(f"  — {label}: {_n(count)}")
            if reasons_lines:
                lines.append("  Причины:")
                lines.extend(reasons_lines)

    # Breakdown верифицированные/неверифицированные каналы
    # Мост к тарифам: показы и клики блока «Собрать очередь на неделю».
    # good-отзывов может быть больше показов (кэш старого фронта, регенерации),
    # поэтому показываем сырые счётчики без вычисления доли от good.
    if has_queue:
        shown_n = _n(queue_shown)
        clicked_n = _n(queue_clicked)
        lines.append(f"— Мост «очередь на неделю»: показан {shown_n}, кликнули {clicked_n}")
        if shown_n == 0 and _n(fb_good) > 0:
            lines.append("  (good-отзывы есть, показов моста нет — проверить кэш фронтенда)")

    if has_gen_breakdown:
        if gen_verified is not None:
            lines.append(f"— Генерации у подключённых каналов: {_n(gen_verified)}")
        if gen_unverified is not None:
            lines.append(f"— Генерации у неподключённых каналов: {_n(gen_unverified)}")

    if len(lines) == 1:
        # Заголовок есть, но данных не добавилось — что-то пошло не так с форматом
        return "\nНовые сигналы ещё не накопились после деплоя."

    return "\n".join(lines)


def build_funnel_report(
    project_name: str,
    metrics: "NormalizedMetrics | None",
    *,
    payment_path: dict | None = None,
    snapshot_dt: datetime | None = None,
    prev_metrics: "NormalizedMetrics | None" = None,
) -> str:
    """Продуктовая воронка без технического языка."""
    now_str = _fmt_dt_msk(snapshot_dt) if snapshot_dt else _fmt_dt_msk(_now_msk())
    lines: list[str] = []

    lines.append(f"🔽 Воронка продукта — {project_name}")
    lines.append(f"Данные: {now_str}")
    lines.append("Период: 7 дней")

    if metrics is None:
        lines.append("\nДанных пока нет.")
        return "\n".join(lines)

    clicks = _n(metrics.clicks)
    signup = _n(metrics.signup)
    activation_1 = _n(metrics.activation_1)
    activation_2 = _n(metrics.activation_2)
    payment_started = _n(metrics.payment_started)
    payment_success = _n(metrics.payment_success)

    pricing_viewed_raw = payment_path.get("pricing_viewed") if payment_path else None
    pricing_viewed = _n(pricing_viewed_raw) if pricing_viewed_raw is not None else None
    pp_started = _n(payment_path.get("payment_started")) if payment_path else payment_started
    pp_success = _n(payment_path.get("payment_success")) if payment_path else payment_success

    MIN_PRICING_FOR_CONCLUSION = 5

    lines.append("\nШаги воронки:")
    if clicks:
        lines.append(f"— {clicks} человек пришли из рекламы")
    if clicks:
        lines.append(f"— {signup} зарегистрировались ({_pct(signup, clicks)} из кликов)")
    else:
        lines.append(f"— {signup} зарегистрировались")
    lines.append(f"— {activation_1} создали канал ({_pct(activation_1, signup)} из регистраций)")

    # Первый пост: показываем feedback (good+bad), если он есть, как осознанный сигнал.
    # Raw post_generations НЕ используется здесь — это техническая метрика,
    # смешивающая ручные действия пользователя и автогенерацию системой.
    fb_good = _n(payment_path.get("first_post_feedback_good")) if payment_path else 0
    fb_bad = _n(payment_path.get("first_post_feedback_bad")) if payment_path else 0
    has_feedback_data = bool(payment_path) and (
        payment_path.get("first_post_feedback_good") is not None
        or payment_path.get("first_post_feedback_bad") is not None
    )
    if has_feedback_data and (fb_good + fb_bad) > 0:
        total_fb = fb_good + fb_bad
        times_word = _plural(total_fb, "раз", "раза", "раз")
        lines.append(f"— первый пост получили и оценили: {total_fb} {times_word} "
                     f"({fb_good} понравился, {fb_bad} не понравился)")
    else:
        lines.append("— Данные по первому посту собираются через отзыв пользователя после деплоя.")

    if pricing_viewed is not None:
        pv_word = _plural(pricing_viewed, "раз открыли тарифы", "раза открыли тарифы", "раз открыли тарифы")
        lines.append(f"— {pricing_viewed} {pv_word}")
    else:
        lines.append("— просмотры тарифов: не отслеживаются")
    lines.append(f"— {pp_started} раз начали оплату")
    lines.append(f"— {pp_success} раз успешно оплатили")

    # Вывод
    lines.append("\nВывод:")
    if signup == 0:
        lines.append("Регистраций пока нет. Смотреть на качество трафика.")
    elif activation_1 == 0:
        lines.append(
            "Пользователи регистрируются, но никто не создал канал. "
            "Что-то происходит сразу после регистрации — стоит пройти этот путь вручную."
        )
    elif pricing_viewed is None and pp_started == 0:
        lines.append(
            f"Ранняя активация хорошая: люди регистрируются и создают каналы. "
            "Переход к тарифам пока нельзя оценить: просмотр тарифов не отслеживается."
        )
    elif pricing_viewed is not None and pricing_viewed < MIN_PRICING_FOR_CONCLUSION:
        lines.append(
            f"Ранняя активация сильная ({_pct(activation_1, signup)} создали канал). "
            "Главный провал — между созданием канала и открытием тарифов. "
            "Непонятно, когда и где пользователь видит предложение оплатить."
        )
    elif pricing_viewed is not None and pricing_viewed >= MIN_PRICING_FOR_CONCLUSION and pp_started == 0:
        lines.append(
            f"Активация хорошая, тарифы открывали {pricing_viewed} раз. "
            "Но до попытки оплаты никто не дошёл — проверить тарифный экран."
        )
    elif pp_started > 0 and pp_success == 0:
        lines.append(
            f"Есть попытки оплаты ({pp_started}), но пока без успеха. "
            "Стоит проверить платёжный шлюз."
        )
    elif pp_success > 0:
        lines.append(
            f"Ранняя воронка работает: люди регистрируются, создают каналы и оплачивают. "
            "Главный вопрос — масштаб и экономика привлечения."
        )
    else:
        lines.append(
            "Ранняя воронка работает: люди регистрируются и создают каналы. "
            "Главный вопрос — доходят ли они до тарифов и оплаты."
        )

    # Динамика
    if prev_metrics is not None:
        deltas: list[str] = []
        pairs = [
            (signup, _n(prev_metrics.signup), "регистраций"),
            (activation_1, _n(prev_metrics.activation_1), "каналов"),
            (pp_success, _n(prev_metrics.payment_success), "оплат"),
        ]
        for cur, prv, label in pairs:
            diff = cur - prv
            if diff:
                sign = "+" if diff > 0 else ""
                deltas.append(f"{label} {sign}{diff}")
        if deltas:
            lines.append(f"\nДинамика: {', '.join(deltas)}.")
        else:
            lines.append("\nДинамика: без изменений.")

    # ── Новые сигналы (onboarding choice + причины + breakdown) ─────────
    # skip_feedback_summary=True: первый пост good/bad уже показан выше
    # в "Шаги воронки" — не дублируем.
    new_signals_block = _format_new_product_signals(payment_path, skip_feedback_summary=True)
    if new_signals_block:
        lines.append(new_signals_block)

    # Технический raw post_generations НЕ показывается в owner-facing /funnel.
    # Метрика смешивает ручные действия и автогенерацию системой и не помогает
    # принять бизнес-решение. Доступна только в /debug.

    lines.append("\nДоска: /board")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /pay — путь к оплате
# ---------------------------------------------------------------------------

def build_pay_report(
    project_name: str,
    *,
    payment_path: dict | None = None,
    metrics: "NormalizedMetrics | None" = None,
    snapshot_dt: datetime | None = None,
) -> str:
    """Путь к оплате без технического языка."""
    now_str = _fmt_dt_msk(snapshot_dt) if snapshot_dt else _fmt_dt_msk(_now_msk())
    lines: list[str] = []

    lines.append(f"💳 Путь к оплате — {project_name}")
    lines.append(f"Данные: {now_str}")
    lines.append("Период: 7 дней")

    MIN_PRICING_FOR_CONCLUSION = 5

    if payment_path is None and metrics is None:
        lines.append("\nДанных по оплате пока нет. Запустите /run для обновления.")
        return "\n".join(lines)

    # Данные
    if payment_path:
        pricing_viewed = _n(payment_path.get("pricing_viewed"))
        pricing_viewed_raw = payment_path.get("pricing_viewed")  # может быть None
        cta_clicked = _n(payment_path.get("payment_cta_clicked"))
        pp_started = _n(payment_path.get("payment_started"))
        pp_success = _n(payment_path.get("payment_success"))
        pp_failed = _n(payment_path.get("payment_failed"))
        pp_returned = _n(payment_path.get("payment_returned"))
    else:
        pricing_viewed_raw = None
        pricing_viewed = 0
        cta_clicked = 0
        pp_started = _n(metrics.payment_started) if metrics else 0
        pp_success = _n(metrics.payment_success) if metrics else 0
        pp_failed = 0
        pp_returned = 0

    lines.append("\nШаги к оплате:")
    if pricing_viewed_raw is None:
        lines.append("— открыли тарифы: данных нет (событие не настроено)")
    else:
        lines.append(f"— открыли тарифы: {pricing_viewed} раз")
    if payment_path:
        lines.append(f"— нажали кнопку оплаты: {cta_clicked} раз")
    lines.append(f"— начали оплату: {pp_started} раз")
    if pp_failed:
        lines.append(f"— ошибок при оплате: {pp_failed}")
    if pp_returned:
        lines.append(f"— вернулись со страницы оплаты: {pp_returned}")
    lines.append(f"— успешно оплатили: {pp_success} раз")

    # Вывод — stage-aware, без запрещённых фраз
    lines.append("\nВывод:")

    if pricing_viewed_raw is None:
        lines.append(
            "Событие просмотра тарифов не отслеживается — неизвестно, "
            "доходят ли пользователи до тарифного экрана."
        )
    elif pricing_viewed < MIN_PRICING_FOR_CONCLUSION and pp_started == 0:
        lines.append(
            f"Тарифов открыли мало ({pricing_viewed} раз). "
            "Данных пока недостаточно, чтобы говорить о проблеме с ценой или тарифным экраном. "
            "Проверять нужно момент, где продукт предлагает оплатить."
        )
    elif pricing_viewed >= MIN_PRICING_FOR_CONCLUSION and cta_clicked == 0:
        lines.append(
            f"Тарифы открывали {pricing_viewed} раз, но никто не нажал кнопку оплаты. "
            "Возможная причина: непонятная ценность, цена или момент предложения."
        )
    elif cta_clicked > 0 and pp_started == 0:
        lines.append(
            "Кнопку оплаты нажимали, но оплата не инициировалась. "
            "Вероятно техническая проблема в начале платёжного процесса."
        )
    elif pp_started > 0 and pp_success == 0:
        from app.rules import MIN_PAYMENT_ATTEMPTS_FOR_PAYMENT_ALERT  # noqa
        if pp_started < MIN_PAYMENT_ATTEMPTS_FOR_PAYMENT_ALERT:
            lines.append(
                f"Есть {pp_started} попытка оплаты без успеха — это ранний сигнал, "
                "пока не критичная проблема. "
                f"При {MIN_PAYMENT_ATTEMPTS_FOR_PAYMENT_ALERT}+ неуспешных попытках стоит проверять шлюз."
            )
        else:
            lines.append(
                f"{pp_started} попыток оплаты без успеха — стоит проверить платёжный шлюз, "
                "логи YooKassa, возможные причины отказов."
            )
    elif pp_success > 0:
        lines.append(f"Есть {pp_success} успешных оплат. Путь к оплате работает.")
    else:
        lines.append(
            "Попыток оплаты пока нет. "
            "Платёжный шлюз пока не главный подозреваемый — "
            "пользователи ещё не доходят до попытки оплаты."
        )

    if pp_returned > 0 and pp_success == 0:
        lines.append(
            f"\n⚠️ {pp_returned} раз пользователи уходили со страницы оплаты без завершения. "
            "Это не успешная оплата — стоит проверить, что видит пользователь на этом шаге."
        )

    if pp_failed > 0:
        lines.append(
            f"\n⚠️ {pp_failed} оплат завершились ошибкой — "
            "стоит посмотреть причины отказов в YooKassa."
        )

    lines.append("\nДоска: /board")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /deep_direct — короткий статус обновления рекламных данных
# ---------------------------------------------------------------------------

def build_deep_direct_status(
    *,
    intel_status: str,
    intel_rows: int,
    intel_error: str | None,
    legacy_ok: bool,
    project_name: str,
) -> str:
    """
    Сообщение после /deep_direct — владельческий язык, без технических терминов.
    Три сценария: успех, частичный результат, полный провал.
    """
    lines: list[str] = []
    lines.append(f"📡 Реклама — {project_name}")

    if intel_status == "ok" and intel_rows > 0:
        lines.append(f"\nАнализ рекламы обновлён: проверено {intel_rows} поисковых запросов за 7 дней.")
        lines.append("\nЧто будет учтено в следующих отчётах:")
        lines.append("— запросы, которые могут тратить бюджет впустую")
        lines.append("— запросы, за которыми стоит наблюдать")
        if not legacy_ok:
            lines.append(
                "\nЧасть детализации по группам объявлений сейчас недоступна. "
                "Анализ поисковых запросов обновлён и будет учтён в /run."
            )
        lines.append(
            "\nВыводы по запросам основаны на смысле запроса и расходе — "
            "точная привязка каждого запроса к регистрации сейчас недоступна."
        )
        lines.append("\nПодробности: /ads\nОбщий вывод по бизнесу: /run")

    elif intel_status == "ok" and intel_rows == 0:
        lines.append(
            "\nДанные по поисковым запросам получены, но список пуст. "
            "Возможно, кампании ещё не набрали достаточно трафика."
        )
        lines.append("\nПодробности: /ads")

    elif intel_status == "not_configured":
        lines.append(
            "\nПодключение к Яндекс.Директу не настроено. "
            "Остальные команды работают в обычном режиме."
        )

    elif intel_status == "timeout":
        lines.append(
            "\nОбновление заняло слишком много времени и было прервано. "
            "Попробуйте /deep_direct ещё раз через несколько минут."
        )

    else:
        lines.append("\nНе удалось обновить анализ рекламы.")
        lines.append("Основные команды /run, /ads, /funnel и /pay продолжают работать.")
        lines.append("Попробуйте позже или проверьте доступ к Яндекс.Директу.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /today — управленческий отчёт текущего дня
# ---------------------------------------------------------------------------

def build_today_report(
    project_name: str,
    metrics: "NormalizedMetrics | None",
    *,
    payment_path: dict | None = None,
    direct_intelligence: "DirectIntelligenceResult | None" = None,
    new_registrations_since_deploy: int | None = None,
    new_registrations_target: int = 30,
    feedback_target: int = 10,
    recent_journeys: list[dict] | None = None,
) -> str:
    """
    /today — рабочая доска текущей проверки. Не просто отчёт, а ответ на
    вопрос "что сейчас происходит и что делать дальше".

    recent_journeys: если передан (per-user journeys endpoint доступен) --
    добавляется блок "Последний коммерческий путь" или "Последний застрявший
    путь", без PII, только user_key.

    Использует только осознанные пользовательские действия:
    регистрации, созданные каналы, feedback первого поста, открытия тарифов,
    старты и успехи оплаты. НЕ использует raw post_generations как
    доказательство вовлечённости.

    new_registrations_since_deploy: если переданo явно — используется для
    прогресс-бара "новые регистрации после деплоя". Если None, считаем что
    эта метрика недоступна и честно об этом пишем.
    """
    lines: list[str] = []
    lines.append(f"Сегодня — {project_name}")
    lines.append(f"Данные: {_fmt_dt_msk(_now_msk())}")

    signup = _n(metrics.signup) if metrics else 0
    activation_1 = _n(metrics.activation_1) if metrics else 0
    pp_started = _n(payment_path.get("payment_started")) if payment_path else 0
    pp_success = _n(payment_path.get("payment_success")) if payment_path else 0
    pricing_viewed_raw = payment_path.get("pricing_viewed") if payment_path else None
    pricing_viewed = _n(pricing_viewed_raw) if pricing_viewed_raw is not None else 0
    pricing_tracked = pricing_viewed_raw is not None

    # Новые сигналы из onboarding/feedback — единственный источник правды
    # о вовлечённости. Raw post_generations (activation_2) НЕ используется
    # здесь и нигде в /today.
    choice_counts = payment_path.get("onboarding_choice_counts") if payment_path else None
    fb_good_raw = payment_path.get("first_post_feedback_good") if payment_path else None
    fb_bad_raw = payment_path.get("first_post_feedback_bad") if payment_path else None
    fb_good = _n(fb_good_raw)
    fb_bad = _n(fb_bad_raw)
    has_feedback_data = fb_good_raw is not None or fb_bad_raw is not None
    total_feedback = fb_good + fb_bad
    has_new_signals = bool(choice_counts) or has_feedback_data

    MIN_PRICING = 5
    MIN_SIGNUP_FOR_CONCLUSIONS = 10

    # ── Определяем стадию (для следующего решения/кандидата) ─────────────
    if pp_success > 0:
        stage = "scale"
    elif pp_started > 0:
        stage = "payment_flow"
    elif pricing_tracked and pricing_viewed >= MIN_PRICING:
        stage = "tariff_screen"
    elif activation_1 > 0:
        stage = "path_to_tariffs"
    elif signup > 0:
        stage = "activation"
    else:
        stage = "traffic"

    # ── Главная проверка ─────────────────────────────────────────────────
    lines.append("\nГлавная проверка:")
    if stage == "path_to_tariffs":
        lines.append("Почему пользователи создают канал, но почти не открывают тарифы?")
    elif stage == "tariff_screen":
        lines.append("Почему пользователи открывают тарифы, но не начинают оплату?")
    elif stage == "payment_flow":
        lines.append("Почему попытки оплаты не доходят до успеха?")
    elif stage == "scale":
        lines.append("Какая экономика привлечения и можно ли масштабировать рекламу?")
    elif stage == "activation":
        lines.append("Почему пользователи регистрируются, но не создают канал?")
    else:
        lines.append("Достаточно ли качественный трафик приходит из рекламы?")

    # ── Прогресс проверки (текстовые шкалы) ──────────────────────────────
    lines.append("\nПрогресс проверки:")
    has_progress_data = False

    if new_registrations_since_deploy is not None:
        has_progress_data = True
        bar = progress_bar(new_registrations_since_deploy, new_registrations_target)
        lines.append(
            f"Новые регистрации после деплоя: "
            f"{new_registrations_since_deploy} / {new_registrations_target}"
        )
        lines.append(bar)

    if has_feedback_data:
        has_progress_data = True
        bar = progress_bar(total_feedback, feedback_target)
        lines.append(f"\nОтзывы о первом посте: {total_feedback} / {feedback_target}")
        lines.append(bar)

    if not has_progress_data:
        lines.append("Новые данные после деплоя ещё не накопились.")

    # ── Что смотрим ───────────────────────────────────────────────────────
    lines.append("\nЧто смотрим:")
    lines.append("— выбор сценария онбординга")
    lines.append("— оценка первого поста")
    lines.append("— причины «не подходит»")
    lines.append("— открытия тарифов")
    lines.append("— старты оплаты")

    # ── Следующее решение ─────────────────────────────────────────────────
    lines.append("\nСледующее решение:")
    if stage == "path_to_tariffs":
        if has_feedback_data and fb_good > fb_bad:
            lines.append(
                "Если первый пост нравится, но тарифы не открывают — "
                "следующий кандидат: очередь постов на неделю."
            )
        else:
            lines.append(
                "Дождаться отзывов о первом посте. Если пост не нравится — "
                "эксперимент с промптом или тематикой. "
                "Если нравится, но тарифы не открывают — "
                "следующий кандидат: очередь постов на неделю."
            )
    elif stage == "tariff_screen":
        lines.append(
            "Проверить тарифный экран вручную с нового аккаунта. "
            "Если ценность неочевидна — эксперимент с подачей тарифов."
        )
    elif stage == "payment_flow":
        lines.append("Проверить YooKassa логи и устранить причину неуспешных оплат.")
    elif stage == "scale":
        lines.append("Оценить стоимость привлечения и принять решение о масштабировании бюджета.")
    else:
        lines.append("Ждать накопления данных по текущей стадии.")

    # ── Сегодня делать ───────────────────────────────────────────────────
    lines.append("\nСегодня делать:")
    lines.append("— смотреть новых пользователей")
    lines.append("— проверять, что Telegram Ads размечается отдельно")
    lines.append("— ждать данные текущей проверки")

    # ── Что не трогать ───────────────────────────────────────────────────
    lines.append("\nЧто не трогать:")
    for item in ["бюджет", "ставки", "лендинг", "цены", "тарифы", "дизайн", "картинки", "free quota"]:
        lines.append(f"— {item}")

    # ── Последний путь пользователя (если journeys доступны) ─────────────
    if recent_journeys:
        from app.notifications import pick_recent_commercial_journey, pick_recent_stuck_journey, _short_path_summary
        stuck = pick_recent_stuck_journey(recent_journeys)
        if stuck:
            journey, minutes = stuck
            lines.append(f"\nПоследний застрявший путь:\n{_short_path_summary(journey)}, "
                        f"оплату не начал {minutes} мин.")
        else:
            commercial = pick_recent_commercial_journey(recent_journeys)
            if commercial:
                lines.append(f"\nПоследний коммерческий путь:\n{_short_path_summary(commercial)}")

    lines.append("\nПодробности: /run  /funnel  /pay  /ads")
    lines.append("Подробности проверки: /experiments")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /experiments — текущие проверки/гипотезы
# ---------------------------------------------------------------------------

def build_experiments_report(
    project_name: str,
    *,
    new_registrations_since_deploy: int | None = None,
    new_registrations_target: int = 30,
    payment_path: dict | None = None,
    feedback_target: int = 10,
    recent_journeys: list[dict] | None = None,
) -> str:
    """
    Owner-facing список активных проверок/гипотез.
    Owner-facing название — "Проверки", без сложной методологии (нет слов
    "эксперимент", "гипотеза" как заголовков — только внутри текста где уместно).
    """
    lines: list[str] = []
    lines.append(f"Проверки — {project_name}")
    lines.append(f"Данные: {_fmt_dt_msk(_now_msk())}")

    fb_good_raw = payment_path.get("first_post_feedback_good") if payment_path else None
    fb_bad_raw = payment_path.get("first_post_feedback_bad") if payment_path else None
    fb_good = _n(fb_good_raw)
    fb_bad = _n(fb_bad_raw)
    has_feedback_data = fb_good_raw is not None or fb_bad_raw is not None
    total_feedback = fb_good + fb_bad

    # ── Текущая активная проверка ────────────────────────────────────────
    lines.append("\nТекущая активная проверка:")
    lines.append("\nНазвание: Путь после первого поста")
    lines.append(
        "\nГлавный вопрос: "
        "Почему пользователи создают канал, но почти не открывают тарифы?"
    )
    lines.append(
        "\nГипотеза: "
        "Люди получают первый результат, но не понимают следующий платный шаг."
    )

    lines.append("\nЧто собираем:")
    lines.append("— новые регистрации после деплоя")
    lines.append("— выбор сценария онбординга")
    lines.append("— feedback первого поста")
    lines.append("— причины «не подходит»")
    lines.append("— открытия тарифов")
    lines.append("— старты оплаты")

    # ── Прогресс ──────────────────────────────────────────────────────────
    lines.append("\nПрогресс:")
    has_progress_data = False
    if new_registrations_since_deploy is not None:
        has_progress_data = True
        lines.append(
            f"Новые регистрации после деплоя: "
            f"{new_registrations_since_deploy} / {new_registrations_target}"
        )
        lines.append(progress_bar(new_registrations_since_deploy, new_registrations_target))
    if has_feedback_data:
        has_progress_data = True
        lines.append(f"\nОтзывы о первом посте: {total_feedback} / {feedback_target}")
        lines.append(progress_bar(total_feedback, feedback_target))
    if not has_progress_data:
        lines.append("Новые данные после деплоя ещё не накопились.")

    # ── Следующее решение ─────────────────────────────────────────────────
    lines.append(
        "\nСледующее решение: "
        "Если первый пост нравится, но тарифы не открывают — "
        "тестируем «очередь постов на неделю»."
    )

    # ── Статус ────────────────────────────────────────────────────────────
    lines.append("\nСтатус:")
    reg_count = new_registrations_since_deploy or 0
    if not has_progress_data:
        status = "идёт сбор данных"
    elif reg_count < new_registrations_target * 0.3 and total_feedback < feedback_target * 0.3:
        status = "идёт сбор данных"
    elif reg_count < new_registrations_target or total_feedback < feedback_target:
        status = "данных недостаточно"
    else:
        status = "можно принимать решение"
    lines.append(status)

    # ── Следующие кандидаты ──────────────────────────────────────────────
    lines.append("\nСледующие кандидаты:")
    lines.append(
        "\n1. Очередь постов на неделю — главный кандидат.\n"
        "Причина: платная ценность должна быть не в одном посте, "
        "а в регулярном ведении канала."
    )
    lines.append(
        "\n2. Анализ существующего канала — наблюдаем.\n"
        "Причина: если много пользователей выбирают анализ канала, "
        "это может стать отдельным сценарием."
    )

    # ── Отложено ──────────────────────────────────────────────────────────
    lines.append("\nОтложено:")
    deferred = [
        ("картинки в постах", "не главный блокер пути к тарифам"),
        ("зумерский дизайн", "не главный блокер пути к тарифам"),
        ("новая группа «Контент-завод»", "сначала нужно подтвердить текущую гипотезу"),
        ("увеличение бюджета", "монетизация ещё не доказана"),
        ("изменение лендинга", "узкое место сейчас не в трафике"),
    ]
    for item, reason in deferred:
        lines.append(f"— {item} ({reason})")

    # ── Последние пользовательские пути (если journeys доступны) ─────────
    if recent_journeys:
        from app.notifications import format_recent_journeys_block
        journeys_block = format_recent_journeys_block(recent_journeys, max_lines=5)
        if journeys_block:
            lines.append(journeys_block)

    lines.append("\nПодробности: /today  /funnel  /pay")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /board — главная компактная доска (единый builder для /board, /today, /run)
# ---------------------------------------------------------------------------

# Целевые объёмы для progress bars недели -- см. задачу "рефакторинг команд".
BOARD_WEEK_REGISTRATIONS_TARGET = 20
BOARD_WEEK_FEEDBACK_TARGET = 10
BOARD_WEEK_PRICING_TARGET = 5


def _determine_board_decision(
    signup: int, activation_1: int,
    pricing_tracked: bool, pricing_viewed: int,
    pp_started: int, pp_success: int,
    has_feedback_data: bool, fb_good: int, fb_bad: int,
) -> tuple[str, str]:
    """
    Возвращает (decision_label, what_is_missing) -- главное решение доски
    и краткое "до решения нужно добрать X".

    decision_label -- одно из:
      ЖДЁМ ДАННЫЕ / ЧИНИМ ПЕРВЫЙ ПОСТ / ЧИНИМ ПУТЬ К ТАРИФАМ /
      ЧИНИМ ТАРИФНЫЙ ЭКРАН / ЧИНИМ ОПЛАТУ / МАСШТАБИРУЕМ
    """
    MIN_SIGNUP_FOR_ANY_DECISION = 10

    if pp_success > 0:
        return "МАСШТАБИРУЕМ", "оценить экономику привлечения на объёме"

    if pp_started > 0:
        return "ЧИНИМ ОПЛАТУ", "разобраться с неуспешными попытками оплаты"

    if pricing_tracked and pricing_viewed >= BOARD_WEEK_PRICING_TARGET:
        return "ЧИНИМ ТАРИФНЫЙ ЭКРАН", "понять, почему открывшие тарифы не платят"

    if signup < MIN_SIGNUP_FOR_ANY_DECISION:
        return "ЖДЁМ ДАННЫЕ", f"набрать {MIN_SIGNUP_FOR_ANY_DECISION - signup} регистраций для выводов"

    if has_feedback_data and fb_bad > fb_good and (fb_good + fb_bad) >= 3:
        return "ЧИНИМ ПЕРВЫЙ ПОСТ", "первый пост чаще не нравится — разобраться, почему"

    if activation_1 > 0:
        return "ЧИНИМ ПУТЬ К ТАРИФАМ", "понять, почему созданный канал не ведёт к тарифам"

    return "ЖДЁМ ДАННЫЕ", "накопить больше данных по воронке"


def build_board_report(
    project_name: str,
    metrics: "NormalizedMetrics | None",
    *,
    payment_path: dict | None = None,
    new_registrations_since_deploy: int | None = None,
    new_registrations_target: int = BOARD_WEEK_REGISTRATIONS_TARGET,
    feedback_target: int = BOARD_WEEK_FEEDBACK_TARGET,
    pricing_target: int = BOARD_WEEK_PRICING_TARGET,
    skip_decision: bool = False,
) -> str:
    """
    /board -- компактное табло, единый builder для /board, /today, /run.
    НЕ отчёт: без длинных гипотез, без длинных критериев, без raw
    post_generations, без технических команд в тексте.

    Возвращает СТРОГО короткий текст (укладывается в один экран телефона).
    """
    signup = _n(metrics.signup) if metrics else 0
    activation_1 = _n(metrics.activation_1) if metrics else 0
    pp_started = _n(payment_path.get("payment_started")) if payment_path else 0
    pp_success = _n(payment_path.get("payment_success")) if payment_path else 0
    pricing_viewed_raw = payment_path.get("pricing_viewed") if payment_path else None
    pricing_viewed = _n(pricing_viewed_raw) if pricing_viewed_raw is not None else 0
    pricing_tracked = pricing_viewed_raw is not None

    fb_good_raw = payment_path.get("first_post_feedback_good") if payment_path else None
    fb_bad_raw = payment_path.get("first_post_feedback_bad") if payment_path else None
    fb_good = _n(fb_good_raw)
    fb_bad = _n(fb_bad_raw)
    has_feedback_data = fb_good_raw is not None or fb_bad_raw is not None
    total_feedback = fb_good + fb_bad

    reg_count = new_registrations_since_deploy if new_registrations_since_deploy is not None else signup

    # Решение считаем по тем же регистрациям, что показываем в блоке НЕДЕЛЯ
    # (reg_count), а не по сырому metrics.signup -- иначе при metrics=None
    # (утренняя сводка берёт регистрации из payment_path) доска показывала бы
    # "18/20", а решение говорило бы "набрать 10 регистраций".
    decision, missing = _determine_board_decision(
        reg_count, activation_1, pricing_tracked, pricing_viewed,
        pp_started, pp_success, has_feedback_data, fb_good, fb_bad,
    )

    lines: list[str] = []
    lines.append(f"📊 <b>Доска — {project_name}</b>")

    # skip_decision=True: состояние цикла (рекомендация/эксперимент/вердикт)
    # уже показано выше блоком Growth Loop -- РЕШЕНИЕ/ФОКУС/СЕГОДНЯ из
    # легаси-логики убираем, чтобы на доске не было двух противоречащих
    # мозгов ("ЧИНИМ ТАРИФНЫЙ ЭКРАН" под "ЭКСПЕРИМЕНТ: чиним первый пост").
    if not skip_decision:
        lines.append("\n<b>РЕШЕНИЕ</b>")
        lines.append(decision)
        lines.append(f"\nДо решения:\n{missing}")

    lines.append("\n<b>НЕДЕЛЯ</b>")
    lines.append(f"Регистрации   {reg_count} / {new_registrations_target}   {progress_bar(reg_count, new_registrations_target)}")
    lines.append(f"Отзывы        {total_feedback} / {feedback_target}   {progress_bar(total_feedback, feedback_target)}")
    lines.append(f"Тарифы        {pricing_viewed} / {pricing_target}   {progress_bar(pricing_viewed, pricing_target)}")
    lines.append(f"Оплаты        {pp_success}")

    if skip_decision:
        lines.append("\n<b>НЕ МЕНЯТЬ</b> 🔒")
        lines.append("бюджет, ставки, тарифы, цены, лендинг")
        lines.append("\nДетали: /journeys /checks /funnel /pay /ads")
        return "\n".join(lines)

    lines.append("\n<b>ФОКУС</b>")
    if decision == "ЧИНИМ ПЕРВЫЙ ПОСТ":
        focus = "первый пост → платный шаг"
    elif decision == "ЧИНИМ ПУТЬ К ТАРИФАМ":
        focus = "путь от канала к тарифному экрану"
    elif decision == "ЧИНИМ ТАРИФНЫЙ ЭКРАН":
        focus = "тарифный экран"
    elif decision == "ЧИНИМ ОПЛАТУ":
        focus = "оплата"
    elif decision == "МАСШТАБИРУЕМ":
        focus = "экономика привлечения"
    else:
        focus = "сбор данных"
    lines.append(focus)

    lines.append("\n<b>СЕГОДНЯ</b>")
    if decision == "ЖДЁМ ДАННЫЕ":
        today_action = "смотреть новых пользователей, ждать данные проверки"
    elif decision == "ЧИНИМ ПЕРВЫЙ ПОСТ":
        today_action = "разобрать причины «не подходит» в /journeys"
    elif decision == "ЧИНИМ ПУТЬ К ТАРИФАМ":
        today_action = "пройти путь от канала до тарифов самому"
    elif decision == "ЧИНИМ ТАРИФНЫЙ ЭКРАН":
        today_action = "проверить тарифный экран вручную"
    elif decision == "ЧИНИМ ОПЛАТУ":
        today_action = "проверить YooKassa логи"
    else:
        today_action = "оценить стоимость привлечения"
    lines.append(today_action)

    lines.append("\nНЕ МЕНЯТЬ")
    lines.append("бюджет, ставки, тарифы, цены, лендинг")

    lines.append("\nДетали: /journeys /checks /funnel /pay /ads")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /checks — проверки и правила принятия решения (alias: /experiments)
# ---------------------------------------------------------------------------

def build_checks_report(
    project_name: str,
    *,
    payment_path: dict | None = None,
) -> str:
    """
    /checks -- активная проверка, правила решения, следующий кандидат,
    что отложено. НЕ дублирует /board (не показывает "СЕГОДНЯ",
    не показывает progress bars недели).
    """
    lines: list[str] = []
    lines.append(f"Проверки — {project_name}")

    fb_good_raw = payment_path.get("first_post_feedback_good") if payment_path else None
    fb_bad_raw = payment_path.get("first_post_feedback_bad") if payment_path else None
    fb_good = _n(fb_good_raw)
    fb_bad = _n(fb_bad_raw)
    has_feedback_data = fb_good_raw is not None or fb_bad_raw is not None

    lines.append("\nАктивная проверка: Путь после первого поста")
    lines.append(
        "Вопрос: почему пользователи создают канал, но почти не открывают тарифы?"
    )

    lines.append("\nПравило решения:")
    lines.append(
        "— если первый пост нравится, но тарифы не открывают → тестируем "
        "«очередь постов на неделю»;"
    )
    lines.append(
        "— если первый пост чаще не нравится → сначала чиним качество результата;"
    )
    lines.append(
        "— если тарифы открывают, но не платят → смотрим тарифный экран."
    )

    lines.append("\nСледующий кандидат: Очередь постов на неделю")
    lines.append(
        "Причина: платная ценность должна быть не в одном посте, "
        "а в регулярном ведении канала."
    )

    lines.append("\nОтложено:")
    deferred = [
        "картинки в постах",
        "зумерский дизайн",
        "новая группа «Контент-завод»",
        "увеличение бюджета",
        "изменение лендинга",
    ]
    for item in deferred:
        lines.append(f"— {item}")

    lines.append("\nДоска: /board")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /journeys — последние пути пользователей (компактный список, не агрегаты)
# ---------------------------------------------------------------------------

def _journey_step_summary(journey: dict) -> str:
    """Короткая строка пути одного пользователя для /journeys."""
    steps = []
    steps.append("рег")
    if journey.get("channel_created_at"):
        steps.append("канал")
    feedback = journey.get("first_post_feedback")
    if feedback == "good" or feedback is True:
        steps.append("отзыв+")
    elif feedback == "bad" or feedback is False:
        steps.append("отзыв-")
    if journey.get("pricing_viewed_at"):
        steps.append("тарифы")
    if journey.get("payment_success_at"):
        steps.append("оплата✓")
    elif journey.get("payment_started_at"):
        steps.append("оплата...")
    return " → ".join(steps)


def _journey_next_action(journey: dict) -> str:
    """Короткое действие для пользователя."""
    if journey.get("payment_success_at"):
        return "оплатил — всё ок"
    if journey.get("payment_started_at"):
        return "ждём завершения оплаты"
    if journey.get("pricing_viewed_at"):
        minutes = journey.get("minutes_since_last_step") or 0
        if minutes >= 45:
            return f"застрял {minutes} мин — можно написать вручную"
        return "недавно открыл тарифы, ждём"
    feedback = journey.get("first_post_feedback")
    if feedback == "bad" or feedback is False:
        return "не понравился пост — смотреть причину"
    if journey.get("channel_created_at"):
        return "ждём первого отзыва"
    return "ждём создания канала"


def build_journeys_report(project_name: str, journeys: list[dict] | None, limit: int = 10) -> str:
    """
    /journeys -- последние N путей пользователей.
    Не показывает агрегированные цифры, кроме мини-сводки сверху.
    Не дублирует /funnel (там конверсия по шагам, тут конкретные пути).
    """
    lines: list[str] = []
    lines.append(f"Пути пользователей — {project_name}")

    if not journeys:
        lines.append("\nДанные по путям пользователей пока недоступны.")
        lines.append("\nДоска: /board")
        return "\n".join(lines)

    # Мини-сводка
    total = len(journeys)
    with_channel = sum(1 for j in journeys if j.get("channel_created_at"))
    with_pricing = sum(1 for j in journeys if j.get("pricing_viewed_at"))
    paid = sum(1 for j in journeys if j.get("payment_success_at"))
    lines.append(f"\nВсего путей: {total} | с каналом: {with_channel} | у тарифов: {with_pricing} | оплатили: {paid}")

    # Сортируем по продвинутости (дальше по воронке — выше)
    def _progress_score(j: dict) -> int:
        score = 0
        for field in ["channel_created_at", "first_post_feedback_at",
                       "pricing_viewed_at", "payment_started_at", "payment_success_at"]:
            if j.get(field):
                score += 1
        return score

    top = sorted(journeys, key=_progress_score, reverse=True)[:limit]

    lines.append("\nПоследние пути:")
    for j in top:
        user_key = j.get("user_key", "unknown")
        source = j.get("source") or j.get("utm_source") or "неизвестный источник"
        source_label = {
            "yandex_direct": "Яндекс.Директ", "direct": "Яндекс.Директ",
            "telegram_ads": "Telegram Ads", "tgads": "Telegram Ads",
        }.get((source or "").lower(), source)
        path = _journey_step_summary(j)
        action = _journey_next_action(j)
        lines.append(f"\n{user_key} ({source_label})")
        lines.append(f"  {path}")
        lines.append(f"  → {action}")

    lines.append("\nДоска: /board")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Ежедневная утренняя сводка: /board + ДИНАМИКА по дням (спарклайны)
# ---------------------------------------------------------------------------

_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[int | float | None]) -> str:
    """
    Текстовый мини-график для Telegram. None -> пробел (день без данных).
    Все значения равны -> средний символ (линия без наклона), не пустота.
    """
    nums = [v for v in values if v is not None]
    if not nums:
        return ""
    lo, hi = min(nums), max(nums)
    chars = []
    for v in values:
        if v is None:
            chars.append(" ")
        elif hi == lo:
            chars.append(_SPARK_CHARS[3])
        else:
            idx = int((v - lo) / (hi - lo) * (len(_SPARK_CHARS) - 1))
            chars.append(_SPARK_CHARS[idx])
    return "".join(chars)


# Метрики динамики: (ключ в daily_counters, подпись). Подписи выровнены
# по ширине как в блоке НЕДЕЛЯ на доске -- цифры не смешиваются.
_DYNAMICS_METRICS = [
    ("registrations", "Регистрации"),
    ("feedback_total", "Отзывы     "),
    ("pricing_viewed", "Тарифы     "),
    ("payment_success", "Оплаты     "),
]


def build_dynamics_block(history: list[dict]) -> str:
    """
    Блок ДИНАМИКА для утренней сводки. history -- список дневных снимков
    (от старых к новым), каждый: {"date": "MM-DD", "registrations": int|None,
    "feedback_total": ..., "pricing_viewed": ..., "payment_success": ...}.

    Значения -- НЕДЕЛЬНОЕ ОКНО на момент дня (payment_path_7d), не «за день»:
    так видно, растёт неделя или падает, без пересчёта дневных дельт.

    Меньше 2 точек -- честно говорим, что динамика копится, не рисуем
    график из одной точки.
    """
    lines = ["📈 <b>ДИНАМИКА</b> (неделя, по дням)"]
    if len(history) < 2:
        lines.append("Появится после 2 дней наблюдений.")
        return "\n".join(lines)

    for key, label in _DYNAMICS_METRICS:
        values = [h.get(key) for h in history]
        nums = [v for v in values if v is not None]
        if not nums:
            continue
        first, last = nums[0], nums[-1]
        delta = last - first
        arrow = "↗" if delta > 0 else ("↘" if delta < 0 else "→")
        lines.append(f"{label} {sparkline(values)}  {first}→{last} {arrow}")

    if len(lines) == 1:
        lines.append("Появится после 2 дней наблюдений.")
    return "\n".join(lines)


def build_daily_board_message(board_text: str, history: list[dict]) -> str:
    """
    Полный текст ежедневного утреннего push: доска + динамика.
    Отправляется раз в день независимо от наличия изменений -- владелец
    видит, что система работает и сколько данных осталось до решения.
    """
    parts = ["Ежедневная сводка", "", board_text, "", build_dynamics_block(history)]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Growth Loop на доске: рекомендация (сост. 2), эксперимент (3), вердикт (4)
# ---------------------------------------------------------------------------

def build_recommendation_block(rec) -> str:
    """
    Состояние 2: ПРЕДЛАГАЮ ЭКСПЕРИМЕНТ. Коротко: действие, почему, эксперимент,
    не менять. Детали (полный change set) -- по кнопке «Данные».
    """
    lines = [
        "<b>РЕШЕНИЕ</b>",
        "💡 <b>ПРЕДЛАГАЮ ЭКСПЕРИМЕНТ</b>",
        "",
        rec.title,
        "",
        "Действие:",
        rec.action,
        "",
        "Почему:",
    ]
    for ev in (rec.evidence_json or [])[:4]:
        lines.append(f"— {ev}")
    if rec.expected_effect:
        lines.append("")
        lines.append(f"Ожидаем: {rec.expected_effect}")
    lines.append("")
    lines.append(f"Эксперимент: {rec.target_sample} новых ({_metric_ru(rec.sample_metric)}) или {rec.max_runtime_days} дней")
    locked = rec.locked_variables_json or []
    if locked:
        lines.append(f"Не менять: {', '.join(locked[:5])}")
    return "\n".join(lines)


_METRIC_RU = {
    "registrations": "регистраций",
    "pricing_viewed": "открытий тарифов",
    "payment_started": "стартов оплаты",
    "payment_success": "оплат",
    "channels_created": "созданных каналов",
    "first_post_feedback_good": "хороших отзывов",
    "first_post_feedback_total": "отзывов",
}


def _metric_ru(metric: str) -> str:
    return _METRIC_RU.get(metric, metric)


def _fmt_rate(rate) -> str:
    if rate is None:
        return "—"
    return f"{min(rate, 1.0):.0%}"  # защитный clamp: доля не может быть > 100%


def build_experiment_block(exp, progress: dict) -> str:
    """Состояние 3: ЭКСПЕРИМЕНТ ИДЁТ. Прогресс + текущий результат + не менять."""
    lines = [
        "🧪 <b>ЭКСПЕРИМЕНТ ИДЁТ</b>",
        "",
        exp.title,
        "",
        f"Прогресс: {progress['current_sample']} / {exp.target_sample} ({_metric_ru(exp.sample_metric)})   "
        f"{progress_bar(progress['current_sample'], exp.target_sample)}",
    ]
    base_rate = progress.get("baseline_rate")
    cur_rate = progress.get("current_rate")
    if base_rate is not None or cur_rate is not None:
        lines.append(
            f"Результат сейчас: {_metric_ru(exp.primary_metric)} {_fmt_rate(base_rate)} → {_fmt_rate(cur_rate)}"
        )
    locked = exp.locked_variables_json or []
    if locked:
        lines.append("")
        lines.append(f"Не менять: {', '.join(locked[:5])}")
    return "\n".join(lines)


def build_verdict_block(exp) -> str:
    """Состояние 4: ВЕРДИКТ завершённого эксперимента."""
    lines = [
        "⚖️ <b>ВЕРДИКТ</b>",
        exp.verdict or "—",
        "",
        exp.title,
    ]
    if exp.result_summary:
        lines.append("")
        lines.append(exp.result_summary)
    return "\n".join(lines)


def build_recommendation_details(rec) -> str:
    """Кнопка «Данные»: полный change set, критерии, риск, гипотеза."""
    lines = [f"Данные рекомендации — {rec.title}", ""]
    if rec.hypothesis:
        lines += ["Гипотеза:", rec.hypothesis, ""]
    lines.append("Change set:")
    for item in (rec.change_set_json or []):
        lines.append(f"— {item}")
    lines.append("")
    lines.append(f"Измеряем: {rec.measure or rec.primary_metric}")
    lines.append(f"Успех: {rec.success_criterion or '—'}")
    lines.append(f"Провал: {rec.failure_criterion or '—'}")
    lines.append(f"Риск: {rec.risk or '—'}")
    lines.append(f"Достоверность: {rec.confidence}")
    return "\n".join(lines)


def build_recommendation_why(rec) -> str:
    """Кнопка «Почему»: доказательства целиком."""
    lines = [f"Почему — {rec.title}", ""]
    for ev in (rec.evidence_json or []):
        lines.append(f"— {ev}")
    if not (rec.evidence_json or []):
        lines.append("Доказательства не заполнены.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Утренний рассказ: 6-8 человеческих строк вместо приборной панели
# ---------------------------------------------------------------------------

def _delta_phrase(history: list[dict], key: str, word: str) -> str | None:
    """«+2 регистрации» из разницы двух последних дневных точек."""
    if len(history) < 2:
        return None
    prev, cur = history[-2].get(key), history[-1].get(key)
    if prev is None or cur is None:
        return None
    d = int(cur) - int(prev)
    if d <= 0:
        return None
    return f"+{d} {word}"


def build_morning_story(
    history: list[dict],
    experiment_line: str | None,
    owner_action: str,
) -> str:
    """
    Верх утренней сводки: что произошло за сутки (дельты дневных точек),
    где эксперимент, и ГЛАВНАЯ строка -- что нужно от владельца.
    Без блоков и прогресс-баров: их место на /board.
    """
    lines = ["☀️ <b>Доброе утро.</b>"]

    deltas = [p for p in (
        _delta_phrase(history, "registrations", "регистрации"),
        _delta_phrase(history, "feedback_total", "отзыва"),
        _delta_phrase(history, "pricing_viewed", "открытия тарифов"),
        _delta_phrase(history, "payment_success", "ОПЛАТЫ"),
    ) if p]
    if deltas:
        lines.append("За сутки: " + ", ".join(deltas) + ".")
    else:
        lines.append("За сутки новых событий не было — при 1–3 регистрациях в день это нормально.")

    if experiment_line:
        lines.append(experiment_line)

    lines.append("")
    lines.append(f"👉 <b>От тебя сегодня:</b> {owner_action}")
    return "\n".join(lines)


def experiment_one_liner(exp, progress: dict) -> str:
    """«Эксперимент „X“: 4/10 отзывов, пока 50% good против 25% — предварительный сигнал.»"""
    base = _fmt_rate(progress.get("baseline_rate"))
    cur_rate = progress.get("current_rate")
    cur = _fmt_rate(cur_rate)
    n = progress.get("current_sample", 0)
    tail = ""
    if cur_rate is not None:
        d = progress.get("delta_metric", 0)
        if d >= 3:
            tail = " — предварительный сигнал"
        elif d >= 1:
            tail = " — единичные события, выводов не делаем"
    return (
        f"Эксперимент «{exp.title}»: {n}/{exp.target_sample} ({_metric_ru(exp.sample_metric)}), "
        f"{_metric_ru(exp.primary_metric)} {base} → {cur}{tail}."
    )
