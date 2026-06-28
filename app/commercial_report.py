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
            "Реклама начала приводить пользователей, и они активно пробуют продукт. "
            "Пока неизвестно, доходят ли они до тарифов — это событие не отслеживается."
        )
    elif pricing_viewed_tracked and pricing_viewed < MIN_PRICING_FOR_CONCLUSION and pp_payment_started == 0:
        lines.append(
            "Реклама начала приводить пользователей, и они активно пробуют продукт. "
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
                "Пользователи создают каналы и генерируют посты. "
                "Пока неизвестно, доходят ли они до тарифов — просмотр тарифов не отслеживается."
            )
        elif pricing_viewed < MIN_PRICING_FOR_CONCLUSION:
            issues.append(
                "Пользователи создают каналы и генерируют посты, но почти не открывают тарифы."
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
            "Пройти путь от первой генерации поста до тарифов. "
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
    lines.append(f"— {activation_2} генераций постов")
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

    lines.append("\n← /run  ← /funnel  ← /pay")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /funnel — продуктовая воронка
# ---------------------------------------------------------------------------

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
    lines.append(f"— {signup} зарегистрировались ({_pct(signup, clicks)} из кликов)")
    lines.append(f"— {activation_1} создали канал ({_pct(activation_1, signup)} из регистраций)")
    lines.append(f"— {activation_2} раз сгенерировали пост")
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
            f"Ранняя активация хорошая: люди регистрируются, создают каналы и генерируют посты. "
            "Переход к тарифам пока нельзя оценить: просмотр тарифов не отслеживается."
        )
    elif pricing_viewed is not None and pricing_viewed < MIN_PRICING_FOR_CONCLUSION:
        lines.append(
            f"Ранняя активация сильная ({_pct(activation_1, signup)} создали канал). "
            "Главный провал — между генерацией поста и открытием тарифов. "
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
            "Ранняя воронка работает: люди регистрируются, создают каналы и генерируют посты. "
            "Главный вопрос — доходят ли они до тарифов и оплаты."
        )

    # Динамика
    if prev_metrics is not None:
        deltas: list[str] = []
        pairs = [
            (signup, _n(prev_metrics.signup), "регистраций"),
            (activation_1, _n(prev_metrics.activation_1), "каналов"),
            (activation_2, _n(prev_metrics.activation_2), "генераций"),
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

    lines.append("\n← /run  /ads →  /pay →")
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

    lines.append("\n← /run  ← /funnel")
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
    Короткое сообщение после /deep_direct.
    Без слова 'legacy', без технического языка.
    """
    lines: list[str] = []
    lines.append(f"📡 Рекламные данные — {project_name}")

    if intel_status == "ok":
        lines.append(f"\n✅ Поисковые запросы обновлены: проанализировано {intel_rows} запросов за 7 дней.")
    elif intel_status == "not_configured":
        lines.append("\n⚙️ Подключение к Яндекс.Директу не настроено.")
    elif intel_status == "timeout":
        lines.append("\n⏱ Обновление поисковых запросов заняло слишком много времени. Попробуйте /deep_direct позже.")
    else:
        lines.append(f"\n⚠️ Не удалось обновить данные по поисковым запросам. Попробуйте позже.")

    if not legacy_ok:
        if intel_status == "ok":
            lines.append("Детализация по группам объявлений сейчас недоступна. Поисковые запросы обновлены и доступны в /ads.")
        else:
            lines.append("Детализация по группам объявлений сейчас недоступна.")

    if intel_status == "ok":
        lines.append("\nПодробный вывод: /ads")
        lines.append("Общий бизнес-вывод: /run")

    return "\n".join(lines)
