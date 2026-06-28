"""
Классификатор поисковых запросов для Daily Business Review.

Принимает строки из SEARCH_QUERY_PERFORMANCE_REPORT (с метриками и
опционально конверсиями по целям) и возвращает классификацию:

  WINNER       -- запрос дал регистрации/активации, не трогать
  WATCH        -- есть клики/расход, данных для вывода мало
  SAFE_NEGATIVE -- семантически нерелевантен, очевидный мусор
  DO_NOT_TOUCH -- широкий/релевантный термин без доказанного мусора

Safe negative -- только семантика, не "нет регистраций при малом расходе".
Релевантный low-spend запрос всегда WATCH, не SAFE_NEGATIVE.

Protected terms (PROTECTED_TERMS) не минусуются никогда.

Этот модуль не пишет в БД и не делает HTTP-запросов.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Константы порогов -- все в одном месте, не размазаны по коду
# ---------------------------------------------------------------------------

# Минимальный расход (руб), при котором можно рекомендовать safe_negative.
# Запросы дешевле этого порога -- watch, не kill (данных мало).
MIN_SPEND_FOR_NEGATIVE_RUB = 100.0

# Минимальное число кликов для вывода о нерелевантности.
MIN_CLICKS_FOR_NEGATIVE = 5

# Минимальное число регистраций (по reliable goal), при котором запрос
# считается winner.
MIN_REGISTRATIONS_FOR_WINNER = 1

# Минимальный расход для winner-вывода. Запрос с 1 регистрацией за 5 руб
# -- слишком мало данных для "winner, можно расширять".
MIN_SPEND_FOR_WINNER_RUB = 30.0


# ---------------------------------------------------------------------------
# Protected terms -- нельзя минусовать без очень сильного semantic evidence.
# Широкие/релевантные термины для AI-постинга в Telegram.
# ---------------------------------------------------------------------------

PROTECTED_TERMS: frozenset[str] = frozenset([
    # Telegram-specific -- очень высокий сигнал релевантности
    "telegram", "телеграм", "tg",
    # AI/нейросеть для постинга
    "нейросеть", "нейросети",
    # Автопостинг -- core продукт
    "автопостинг", "автопост",
    # Боты для постинга (не generic "бот")
    "бот для постинга", "бот для канала", "бот telegram", "telegram бот",
    # Прямые упоминания продукта / категории
    "smm бот", "smm автоматизация",
])


# ---------------------------------------------------------------------------
# Семантические категории очевидного мусора
# Каждая категория -- список паттернов (подстрок в нижнем регистре).
# Запрос считается мусорным если подходит хотя бы под одну категорию И
# не содержит protected term из PROTECTED_TERMS.
# ---------------------------------------------------------------------------

_GARBAGE_PATTERNS: dict[str, list[str]] = {
    "adult_18_plus": [
        "18+", "эротик", "порно", "секс", "adult", "onlyfans", "xxx",
        "хентай", "hentai", "эрот",
    ],
    "youtube_decoration": [
        "шапка youtube", "шапка ютуб", "оформление youtube", "оформление ютуб",
        "превью youtube", "превью ютуб", "обложка youtube", "ютуб канал оформление",
        "youtube banner", "yt шапка", "шапка канала ютуб", "шапка канала youtube",
        "оформление ютуб канала", "ютуб оформление",
    ],
    "max_streaming": [
        # Стриминг MAX/Макс -- явно не Telegram-постинг.
        # Однословные "max"/"макс" могут быть мусором если пришли без контекста.
        # Паттерны: отдельное слово (через пробелы/начало/конец строки)
        # или в комбинации с явными стриминговыми словами.
        "кино max", "сериал max", "смотреть max", "подписка max", "max онлайн",
        "кино макс", "сериал макс", "смотреть макс", "подписка макс", "макс онлайн",
        "стриминг max", "стриминг макс",
    ],
    "cross_platform_not_telegram": [
        # Кросспостинг в НЕ-Telegram соцсети без упоминания Telegram
        "вконтакте автопост", "vk автопост", "instagram автопост",
        "facebook автопост", "tiktok автопост", "youtube автопост",
        "одноклассники автопост",
        "кросспостинг во все", "кросспостинг в соцсети", "постинг во все соцсети",
        "публикация во все соцсети", "постинг в соцсети",
    ],
    "file_transfer": [
        "перенос файлов", "передача файлов", "синхронизация файлов",
        "конвертер файлов", "конвертация файлов",
    ],
    "profile_decoration_non_post": [
        # Оформление профиля/аватара без связи с постингом
        "оформление профиля", "аватар профиль", "аватарка профиль",
        "фото профиля", "шапка профиля",
    ],
    "irrelevant_platform": [
        # Явно нерелевантные платформы без связи с Telegram/AI-постингом
        "wordpress блог", "blogger.com", "livejournal",
    ],
    "academic": [
        "реферат", "курсовая", "дипломная", "сочинение на тему",
        "домашнее задание", "эссе по", "шпаргалка",
    ],
}


# ---------------------------------------------------------------------------
# Типы классификации и структуры данных
# ---------------------------------------------------------------------------

class QueryLabel(str, Enum):
    WINNER = "winner"
    WATCH = "watch"
    SAFE_NEGATIVE = "safe_negative"
    DO_NOT_TOUCH = "do_not_touch"


class ActionType(str, Enum):
    ADS_ACTION_SUGGESTED = "ADS_ACTION_SUGGESTED"
    PRODUCT_ACTION_SUGGESTED = "PRODUCT_ACTION_SUGGESTED"
    PAYMENT_ACTION_SUGGESTED = "PAYMENT_ACTION_SUGGESTED"
    DO_NOT_TOUCH = "DO_NOT_TOUCH"
    WAIT_FOR_DATA = "WAIT_FOR_DATA"


@dataclass
class ActionItem:
    action_type: ActionType
    description: str
    rationale: str
    confidence: str = "medium"  # low | medium | high
    payload: dict = field(default_factory=dict)


@dataclass
class QueryClassification:
    query: str
    label: QueryLabel
    reason: str
    campaign_name: str = ""
    ad_group_name: str = ""
    clicks: int = 0
    cost: float = 0.0
    impressions: int = 0
    registrations: Optional[int] = None        # None = нет reliable goal data
    registration_attribution: str = "none"     # reliable | unreliable | none
    action_item: Optional[ActionItem] = None
    garbage_category: Optional[str] = None     # заполняется при safe_negative


@dataclass
class DirectIntelligenceResult:
    """
    Структурированный результат анализа Direct granular data.
    Возвращается classify_search_queries() и сохраняется в кэш.
    owner_report.py только форматирует этот объект в текст.
    """
    period_label: str                          # "7д" / "10д" / "с 23.06"
    winners: list[QueryClassification] = field(default_factory=list)
    watch: list[QueryClassification] = field(default_factory=list)
    safe_negatives: list[QueryClassification] = field(default_factory=list)
    do_not_touch: list[QueryClassification] = field(default_factory=list)
    action_items: list[ActionItem] = field(default_factory=list)
    spend_gate: Optional["SpendGateResult"] = None
    # Маркировки надёжности данных
    has_registration_attribution: bool = False  # есть ли reliable goal data
    registration_attribution_note: str = ""
    missing_data: list[str] = field(default_factory=list)
    total_queries_analyzed: int = 0
    total_spend: float = 0.0
    total_clicks: int = 0

    def to_dict(self) -> dict:
        """Для сериализации в кэш."""
        def _q(q: QueryClassification) -> dict:
            return {
                "query": q.query,
                "label": q.label.value,
                "reason": q.reason,
                "campaign_name": q.campaign_name,
                "ad_group_name": q.ad_group_name,
                "clicks": q.clicks,
                "cost": q.cost,
                "impressions": q.impressions,
                "registrations": q.registrations,
                "registration_attribution": q.registration_attribution,
                "garbage_category": q.garbage_category,
                "action_item": {
                    "action_type": q.action_item.action_type.value,
                    "description": q.action_item.description,
                    "rationale": q.action_item.rationale,
                    "confidence": q.action_item.confidence,
                } if q.action_item else None,
            }
        def _a(a: ActionItem) -> dict:
            return {
                "action_type": a.action_type.value,
                "description": a.description,
                "rationale": a.rationale,
                "confidence": a.confidence,
                "payload": a.payload,
            }
        return {
            "period_label": self.period_label,
            "winners": [_q(q) for q in self.winners],
            "watch": [_q(q) for q in self.watch],
            "safe_negatives": [_q(q) for q in self.safe_negatives],
            "do_not_touch": [_q(q) for q in self.do_not_touch],
            "action_items": [_a(a) for a in self.action_items],
            "spend_gate": self.spend_gate.to_dict() if self.spend_gate else None,
            "has_registration_attribution": self.has_registration_attribution,
            "registration_attribution_note": self.registration_attribution_note,
            "missing_data": self.missing_data,
            "total_queries_analyzed": self.total_queries_analyzed,
            "total_spend": self.total_spend,
            "total_clicks": self.total_clicks,
        }


# ---------------------------------------------------------------------------
# Spend Gate
# ---------------------------------------------------------------------------

# Константы для spend gate
SPEND_GATE_REGISTRATIONS_SCALE_MIN = 50      # ниже -- не масштабировать бюджет
SPEND_GATE_REGISTRATIONS_MONETIZATION_WARN = 50   # выше + нет payment intent = warning
SPEND_GATE_MIN_ACTIVATION_RATE = 0.5         # channels_created / registrations


class SpendGateVerdict(str, Enum):
    CONTROLLED_SPEND_OK = "controlled_spend_ok"       # продолжать с текущим бюджетом
    DO_NOT_SCALE = "do_not_scale"                     # не масштабировать
    MONETIZATION_NOT_PROVEN = "monetization_not_proven"  # warning: нет payment intent
    PAUSE_RECOMMENDED = "pause_recommended"           # пауза / снижение


@dataclass
class SpendGateResult:
    verdict: SpendGateVerdict
    spend_rub: float
    registrations: int
    has_activation: bool
    has_payment_intent: bool       # payment_started > 0 или pricing_viewed достаточно
    has_payment_success: bool
    explanation: str
    action_items: list[ActionItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "spend_rub": self.spend_rub,
            "registrations": self.registrations,
            "has_activation": self.has_activation,
            "has_payment_intent": self.has_payment_intent,
            "has_payment_success": self.has_payment_success,
            "explanation": self.explanation,
            "action_items": [
                {
                    "action_type": a.action_type.value,
                    "description": a.description,
                    "rationale": a.rationale,
                    "confidence": a.confidence,
                }
                for a in self.action_items
            ],
        }


def evaluate_spend_gate(
    spend_rub: float,
    registrations: int,
    channels_created: int,
    payment_started: int,
    payment_success: int,
    pricing_viewed: Optional[int],
    min_pricing_viewed_for_intent: int = 5,
) -> SpendGateResult:
    """
    Оценивает текущее состояние рекламного бюджета.

    Логика (в порядке приоритета):
    1. Нет регистраций при значимом расходе -> pause_recommended
    2. Нет активации -> do_not_scale (онбординг сломан)
    3. Есть регистрации + активация, нет payment intent -> do_not_scale
    4. Много регистраций + активация, нет payment intent -> monetization_not_proven
    5. Есть payment_success -> controlled_spend_ok
    6. Есть payment intent, нет success -> do_not_scale (ждём конверсий)
    7. Иначе -> controlled_spend_ok (продолжать наблюдать)
    """
    activation_rate = channels_created / registrations if registrations > 0 else 0.0
    has_activation = activation_rate >= SPEND_GATE_MIN_ACTIVATION_RATE and channels_created > 0
    has_payment_intent = (
        payment_started > 0
        or (pricing_viewed is not None and pricing_viewed >= min_pricing_viewed_for_intent)
    )
    has_payment_success = payment_success > 0

    action_items: list[ActionItem] = []

    if registrations == 0 and spend_rub > 500:
        return SpendGateResult(
            verdict=SpendGateVerdict.PAUSE_RECOMMENDED,
            spend_rub=spend_rub,
            registrations=registrations,
            has_activation=False,
            has_payment_intent=False,
            has_payment_success=False,
            explanation=(
                f"Потрачено {spend_rub:.0f} ₽, но регистраций нет. "
                "Рекомендуется проверить посадочную страницу и трафик до остановки рекламы."
            ),
            action_items=[ActionItem(
                ActionType.ADS_ACTION_SUGGESTED,
                "Проверить лендинг и quality score перед продолжением расхода",
                "Нет регистраций при значимом расходе",
                confidence="high",
            )],
        )

    if registrations > 0 and not has_activation:
        return SpendGateResult(
            verdict=SpendGateVerdict.DO_NOT_SCALE,
            spend_rub=spend_rub,
            registrations=registrations,
            has_activation=False,
            has_payment_intent=has_payment_intent,
            has_payment_success=has_payment_success,
            explanation=(
                f"Регистрации есть ({registrations}), но активация слабая "
                f"({channels_created} создали канал). Масштабировать рекламу до "
                "улучшения онбординга нет смысла."
            ),
            action_items=[ActionItem(
                ActionType.PRODUCT_ACTION_SUGGESTED,
                "Улучшить онбординг: больше пользователей должны создавать канал после регистрации",
                f"Activation rate {activation_rate:.0%} ниже {SPEND_GATE_MIN_ACTIVATION_RATE:.0%}",
                confidence="high",
            )],
        )

    if has_payment_success:
        return SpendGateResult(
            verdict=SpendGateVerdict.CONTROLLED_SPEND_OK,
            spend_rub=spend_rub,
            registrations=registrations,
            has_activation=has_activation,
            has_payment_intent=True,
            has_payment_success=True,
            explanation=(
                f"Регистрации ({registrations}), активация и успешные оплаты есть. "
                "Контролируемый расход обоснован."
            ),
        )

    if registrations >= SPEND_GATE_REGISTRATIONS_MONETIZATION_WARN and not has_payment_intent:
        return SpendGateResult(
            verdict=SpendGateVerdict.MONETIZATION_NOT_PROVEN,
            spend_rub=spend_rub,
            registrations=registrations,
            has_activation=has_activation,
            has_payment_intent=False,
            has_payment_success=False,
            explanation=(
                f"⚠ {registrations} регистраций и активация есть, но монетизация не доказана "
                f"(payment intent = 0). Масштабировать бюджет рискованно. "
                "Фокус — путь до тарифов и оплаты."
            ),
            action_items=[ActionItem(
                ActionType.PRODUCT_ACTION_SUGGESTED,
                "Проверить путь от активации до тарифного экрана/paywall",
                f"{registrations} регистраций без payment intent",
                confidence="high",
            ), ActionItem(
                ActionType.DO_NOT_TOUCH,
                "Не масштабировать рекламный бюджет до появления payment intent",
                "Монетизация не доказана",
                confidence="high",
            )],
        )

    if has_payment_intent and not has_payment_success:
        return SpendGateResult(
            verdict=SpendGateVerdict.DO_NOT_SCALE,
            spend_rub=spend_rub,
            registrations=registrations,
            has_activation=has_activation,
            has_payment_intent=True,
            has_payment_success=False,
            explanation=(
                f"Регистрации и активация есть, есть попытки оплаты. "
                "Ждём конверсии в успешную оплату до масштабирования бюджета."
            ),
            action_items=[ActionItem(
                ActionType.WAIT_FOR_DATA,
                "Ждать первых успешных оплат до решения о масштабировании",
                "Есть payment intent, нет payment success",
                confidence="medium",
            )],
        )

    # Есть регистрации и активация, нет payment intent, не достигли порога warning
    return SpendGateResult(
        verdict=SpendGateVerdict.DO_NOT_SCALE,
        spend_rub=spend_rub,
        registrations=registrations,
        has_activation=has_activation,
        has_payment_intent=False,
        has_payment_success=False,
        explanation=(
            f"Регистрации ({registrations}) и активация есть. "
            "Рекламу не останавливать. Масштабировать не стоит — ждём данных по тарифам/оплате."
        ),
        action_items=[ActionItem(
            ActionType.DO_NOT_TOUCH,
            "Не менять бюджет/ставки/кампании резко",
            "Реклама работает, монетизация в процессе накопления данных",
            confidence="medium",
        )],
    )


# ---------------------------------------------------------------------------
# Классификация запросов
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Нижний регистр, убираем лишние пробелы."""
    return re.sub(r"\s+", " ", text.lower().strip())


def _contains_protected_term(query_lower: str) -> Optional[str]:
    """Возвращает первый найденный protected term или None."""
    for term in PROTECTED_TERMS:
        if term in query_lower:
            return term
    return None


def _detect_garbage_category(query_lower: str) -> Optional[str]:
    """
    Возвращает название garbage-категории если запрос подходит под мусор,
    иначе None. Не называем мусором то что содержит protected term.
    """
    if _contains_protected_term(query_lower):
        return None

    # Специальные случаи: однословные или двусловные запросы без контекста.
    # "max" / "макс" сами по себе почти всегда стриминг MAX, не наш продукт.
    # "хентай" — взрослый контент.
    stripped = query_lower.strip()
    if stripped in ("max", "макс", "хентай", "hentai"):
        return "max_streaming" if stripped in ("max", "макс") else "adult_18_plus"

    for category, patterns in _GARBAGE_PATTERNS.items():
        for pattern in patterns:
            if pattern in query_lower:
                return category
    return None


def classify_query(
    query: str,
    clicks: int = 0,
    cost: float = 0.0,
    impressions: int = 0,
    registrations: Optional[int] = None,
    registration_attribution: str = "none",
    campaign_name: str = "",
    ad_group_name: str = "",
) -> QueryClassification:
    """
    Классифицирует один поисковый запрос.

    registration_attribution:
      "reliable"   -- конверсии из Direct с явным GoalId для регистраций
      "unreliable" -- total conversions из Direct, GoalId не задан
      "none"       -- нет данных по конверсиям из Direct

    Если registration_attribution != "reliable", регистрации не учитываются
    при классификации winner/safe_negative -- используется только семантика
    и spend/clicks.
    """
    q = _normalize(query)

    # 1. Winner -- только если есть RELIABLE attribution с регистрациями
    if (
        registration_attribution == "reliable"
        and registrations is not None
        and registrations >= MIN_REGISTRATIONS_FOR_WINNER
        and cost >= MIN_SPEND_FOR_WINNER_RUB
    ):
        return QueryClassification(
            query=query,
            label=QueryLabel.WINNER,
            reason=f"{registrations} регистр., расход {cost:.0f} ₽",
            campaign_name=campaign_name,
            ad_group_name=ad_group_name,
            clicks=clicks,
            cost=cost,
            impressions=impressions,
            registrations=registrations,
            registration_attribution=registration_attribution,
            action_item=ActionItem(
                ActionType.DO_NOT_TOUCH,
                f'Не минусовать, не снижать ставки: "{query}" даёт регистрации',
                f"{registrations} регистр. по reliable goal",
                confidence="high",
            ),
        )

    # 2. Safe negative -- семантический мусор + достаточно данных.
    # Проверяем ДО protected-check: очевидный мусор (18+, youtube-шапки,
    # перенос файлов и т.п.) минусуется даже если рядом стоит слово "канал",
    # "контент" и т.п. -- контекст здесь явно нерелевантный.
    # НО: если запрос содержит сильный protected term (telegram, телеграм,
    # нейросеть, автопостинг, бот), garbage-вывод отменяется.
    STRONG_PROTECTED: frozenset[str] = frozenset([
        "telegram", "телеграм", "tg",
        "нейросеть", "нейросети",
        "автопостинг", "автопост",
        "бот", "боты",
    ])
    has_strong_protected = any(term in q for term in STRONG_PROTECTED)
    garbage_cat = None if has_strong_protected else _detect_garbage_category(q)

    if garbage_cat and clicks >= MIN_CLICKS_FOR_NEGATIVE and cost >= MIN_SPEND_FOR_NEGATIVE_RUB:
        return QueryClassification(
            query=query,
            label=QueryLabel.SAFE_NEGATIVE,
            reason=f"семантика: {garbage_cat.replace('_', ' ')}",
            campaign_name=campaign_name,
            ad_group_name=ad_group_name,
            clicks=clicks,
            cost=cost,
            impressions=impressions,
            registrations=registrations,
            registration_attribution=registration_attribution,
            garbage_category=garbage_cat,
            action_item=ActionItem(
                ActionType.ADS_ACTION_SUGGESTED,
                f'Рассмотреть минус-фразу exact: "{query}"',
                f"Семантика ({garbage_cat.replace('_', ' ')}), {clicks} кл., {cost:.0f} ₽",
                confidence="high",
                payload={"query": query, "category": garbage_cat},
            ),
        )

    # Мусор, но мало данных -> watch (не DO_NOT_TOUCH)
    if garbage_cat:
        return QueryClassification(
            query=query,
            label=QueryLabel.WATCH,
            reason=f"возможно мусор ({garbage_cat.replace('_', ' ')}), но данных мало ({clicks} кл., {cost:.0f} ₽)",
            campaign_name=campaign_name,
            ad_group_name=ad_group_name,
            clicks=clicks,
            cost=cost,
            impressions=impressions,
            registrations=registrations,
            registration_attribution=registration_attribution,
            garbage_category=garbage_cat,
        )

    # 3. Protected term -- DO_NOT_TOUCH (любой protected term, не только сильный)
    protected = _contains_protected_term(q)
    if protected:
        return QueryClassification(
            query=query,
            label=QueryLabel.DO_NOT_TOUCH,
            reason=f'содержит защищённый термин "{protected}"',
            campaign_name=campaign_name,
            ad_group_name=ad_group_name,
            clicks=clicks,
            cost=cost,
            impressions=impressions,
            registrations=registrations,
            registration_attribution=registration_attribution,
        )

    # 4. Watch -- всё остальное (релевантное или неопределённое, данных недостаточно)
    return QueryClassification(
        query=query,
        label=QueryLabel.WATCH,
        reason=f"данных пока недостаточно ({clicks} кл., {cost:.0f} ₽)",
        campaign_name=campaign_name,
        ad_group_name=ad_group_name,
        clicks=clicks,
        cost=cost,
        impressions=impressions,
        registrations=registrations,
        registration_attribution=registration_attribution,
    )


def classify_search_queries(
    query_rows: list[dict],
    period_label: str = "7д",
    registration_goal_id: Optional[int] = None,
    spend_gate_data: Optional[dict] = None,
) -> DirectIntelligenceResult:
    """
    Классифицирует список строк из SEARCH_QUERY_PERFORMANCE_REPORT.

    query_rows -- список dict с ключами:
      query, clicks, cost, impressions, ctr, cpc,
      campaign_name, ad_group_name,
      registrations (опц.), registration_attribution (опц.)

    registration_goal_id -- если задан, считаем что атрибуция надёжная
      (GoalId явно указан в запросе). Если None -- attribution = "none".

    spend_gate_data -- dict с ключами для SpendGate (опционально):
      spend_rub, registrations, channels_created, payment_started,
      payment_success, pricing_viewed.

    Возвращает DirectIntelligenceResult.
    """
    result = DirectIntelligenceResult(period_label=period_label)

    has_reliable_attribution = registration_goal_id is not None
    result.has_registration_attribution = has_reliable_attribution

    if not has_reliable_attribution:
        result.registration_attribution_note = (
            "GoalId для регистраций не задан — атрибуция кликов/запросов к "
            "регистрациям недоступна. Настройте DIRECT_REGISTRATION_GOAL_ID. "
            "Классификация winner/safe_negative основана только на семантике."
        )
        result.missing_data.append("registration_goal_id")
    else:
        result.registration_attribution_note = (
            f"Атрибуция регистраций по GoalId={registration_goal_id}."
        )

    classifications: list[QueryClassification] = []
    total_spend = 0.0
    total_clicks = 0

    for row in query_rows:
        query = row.get("query", "").strip()
        if not query:
            continue

        clicks = int(row.get("clicks") or 0)
        cost = float(row.get("cost") or 0.0)
        impressions = int(row.get("impressions") or 0)
        campaign_name = row.get("campaign_name", "")
        ad_group_name = row.get("ad_group_name", "")

        # Конверсии только если reliable attribution
        if has_reliable_attribution:
            raw_reg = row.get("registrations")
            registrations = int(raw_reg) if raw_reg is not None else None
            attribution = "reliable"
        else:
            registrations = None
            attribution = "none"

        total_spend += cost
        total_clicks += clicks

        classification = classify_query(
            query=query,
            clicks=clicks,
            cost=cost,
            impressions=impressions,
            registrations=registrations,
            registration_attribution=attribution,
            campaign_name=campaign_name,
            ad_group_name=ad_group_name,
        )
        classifications.append(classification)

        # Собираем action items из классификации запроса
        if classification.action_item:
            result.action_items.append(classification.action_item)

    # Сортируем по расходу убыванию внутри каждой категории
    result.winners = sorted(
        [c for c in classifications if c.label == QueryLabel.WINNER],
        key=lambda c: c.cost, reverse=True,
    )
    result.watch = sorted(
        [c for c in classifications if c.label == QueryLabel.WATCH],
        key=lambda c: c.cost, reverse=True,
    )
    result.safe_negatives = sorted(
        [c for c in classifications if c.label == QueryLabel.SAFE_NEGATIVE],
        key=lambda c: c.cost, reverse=True,
    )
    result.do_not_touch = sorted(
        [c for c in classifications if c.label == QueryLabel.DO_NOT_TOUCH],
        key=lambda c: c.cost, reverse=True,
    )

    result.total_queries_analyzed = len(classifications)
    result.total_spend = round(total_spend, 2)
    result.total_clicks = total_clicks

    # Spend gate
    if spend_gate_data:
        result.spend_gate = evaluate_spend_gate(
            spend_rub=spend_gate_data.get("spend_rub", 0.0),
            registrations=spend_gate_data.get("registrations", 0),
            channels_created=spend_gate_data.get("channels_created", 0),
            payment_started=spend_gate_data.get("payment_started", 0),
            payment_success=spend_gate_data.get("payment_success", 0),
            pricing_viewed=spend_gate_data.get("pricing_viewed"),
        )
        # Добавляем spend_gate action items в общий список (без дублей)
        if result.spend_gate:
            for ai in result.spend_gate.action_items:
                result.action_items.append(ai)

    return result
