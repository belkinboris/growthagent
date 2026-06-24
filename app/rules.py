"""
Декларативные правила анализа.

Каждое правило -- объект Rule с условием (Callable) и шаблонами текста.
Правила работают ИСКЛЮЧИТЕЛЬНО с нормализованными ключами воронки
(traffic, signup, activation_1, activation_2, payment_started,
payment_success, revenue, pending_payments) -- никаких полей конкретного
продукта (users_created и т.п.) здесь быть не может.

fingerprint правила строится из (project_id, rule_id, period_key,
affected_step) -- НЕ из самих значений метрик. Конкретные цифры (clicks,
spend, signup...) идут в payload, не в fingerprint. Это гарантирует, что
изменение цифр не создаёт новый алерт -- меняется occurrence_count и
last_seen_at существующего.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from app.config import MIN_CLICKS_FOR_CONVERSION_CHECK, MIN_SIGNUP_CONVERSION_WARN_PERCENT
from app.models import AlertCategory, AlertSeverity


# Минимум попыток оплаты, после которого отсутствие успешных оплат можно
# считать проблемой платёжного шага. Одна попытка -- это milestone/ранний
# сигнал для наблюдения, но не P1-проблема и не главный вывод /run.
MIN_PAYMENT_ATTEMPTS_FOR_PAYMENT_ALERT = 3


@dataclass
class NormalizedMetrics:
    """
    Метрики одного окна (period_key) для одного проекта, уже после
    нормализации воронки и объединения с данными рекламы (если есть).
    Любое поле может быть None, если источник не настроен или не ответил.
    """

    period_key: str  # "3h" | "24h" | "7d"

    # из Project Metrics API
    signup: Optional[int] = None
    activation_1: Optional[int] = None
    activation_2: Optional[int] = None
    payment_started: Optional[int] = None
    payment_success: Optional[int] = None
    revenue: Optional[float] = None
    pending_payments: Optional[int] = None

    # из Яндекс.Директа
    spend: Optional[float] = None
    clicks: Optional[int] = None
    impressions: Optional[int] = None
    ctr: Optional[float] = None

    # из Яндекс.Метрики (отдельно от Project Metrics API -- для сравнения,
    # см. правило metrics_discrepancy)
    metrika_signup: Optional[int] = None

    # какие источники реально ответили в этом окне -- используется, чтобы
    # не путать "0 событий" с "источник не отвечал"
    sources_ok: set[str] = field(default_factory=set)


@dataclass
class RuleResult:
    rule_id: str
    title: str
    category: AlertCategory
    severity: AlertSeverity
    affected_step: str
    hypothesis: str
    check_action: str
    do_not_action: str
    sample_size: int
    metric_type: str  # "traffic" | "conversion" -- для confidence.py
    payload: dict


@dataclass
class Rule:
    rule_id: str
    title: str
    category: AlertCategory
    severity: AlertSeverity
    affected_step: str  # к какому шагу воронки относится -- часть fingerprint
    metric_type: str  # "traffic" | "conversion"
    required_sources: tuple[str, ...]  # источники, без которых правило не может сработать
    condition: Callable[[NormalizedMetrics], bool]
    hypothesis_template: str
    check_action_template: str
    do_not_action_template: str
    sample_size_fn: Callable[[NormalizedMetrics], int]
    payload_fn: Callable[[NormalizedMetrics], dict]

    def fingerprint(self, project_id: int, period_key: str) -> str:
        return f"{project_id}/{self.rule_id}/{period_key}/{self.affected_step}"

    def is_checkable(self, sources_ok: set[str]) -> bool:
        """
        True, если все источники, нужные правилу, реально отвечали в этом
        прогоне. Используется scheduler.py, чтобы построить checked_categories
        для resolve_missing_alerts() -- категория считается проверенной,
        если хотя бы одно её правило было checkable.
        """
        return set(self.required_sources).issubset(sources_ok)

    def evaluate(self, metrics: NormalizedMetrics) -> Optional[RuleResult]:
        if not self.condition(metrics):
            return None
        payload = self.payload_fn(metrics)
        return RuleResult(
            rule_id=self.rule_id,
            title=self.title,
            category=self.category,
            severity=self.severity,
            affected_step=self.affected_step,
            hypothesis=self.hypothesis_template.format(**payload),
            check_action=self.check_action_template.format(**payload),
            do_not_action=self.do_not_action_template.format(**payload),
            sample_size=self.sample_size_fn(metrics),
            metric_type=self.metric_type,
            payload=payload,
        )


def _has(*sources: str):
    def check(m: NormalizedMetrics, required=sources) -> bool:
        return all(s in m.sources_ok for s in required)
    return check


# ---------------------------------------------------------------------------
# Правила. Порядок имеет значение для приоритизации в analyzer.py, но не
# для самого набора условий -- каждое правило независимо.
# ---------------------------------------------------------------------------

RULES: list[Rule] = [

    Rule(
        rule_id="spend_no_signups",
        title="Расход без регистраций",
        category=AlertCategory.traffic_no_signups,
        severity=AlertSeverity.p1,
        affected_step="signup",
        metric_type="traffic",
        required_sources=("direct", "product"),
        condition=lambda m: (
            _has("direct", "product")(m)
            and (m.spend or 0) > 500
            and (m.signup or 0) == 0
        ),
        hypothesis_template=(
            "Потрачено {spend:.0f} ₽, но ни одной регистрации. "
            "Вероятная зона проблемы -- связка объявление → лендинг → форма регистрации."
        ),
        check_action_template="Пройти путь пользователя руками: клик по объявлению → лендинг → регистрация.",
        do_not_action_template="Не увеличивать бюджет и не менять ставки, пока не проверена связка.",
        sample_size_fn=lambda m: m.clicks or 0,
        payload_fn=lambda m: {"spend": m.spend or 0, "clicks": m.clicks or 0},
    ),

    Rule(
        rule_id="clicks_no_signups",
        title="Клики без регистраций",
        category=AlertCategory.traffic_no_signups,
        severity=AlertSeverity.p1,
        affected_step="signup",
        metric_type="traffic",
        required_sources=("direct", "product"),
        condition=lambda m: (
            _has("direct", "product")(m)
            and (m.clicks or 0) >= 30
            and (m.signup or 0) == 0
        ),
        hypothesis_template=(
            "{clicks} кликов, 0 регистраций. Вероятная зона проблемы -- "
            "лендинг или мобильный путь пользователя."
        ),
        check_action_template="Проверить лендинг с мобильного устройства, как реальный пользователь.",
        do_not_action_template="Не менять рекламные объявления, пока не проверен лендинг.",
        sample_size_fn=lambda m: m.clicks or 0,
        payload_fn=lambda m: {"clicks": m.clicks or 0},
    ),

    Rule(
        rule_id="signups_no_activation_1",
        title="Регистрации без активации (шаг 1)",
        category=AlertCategory.signups_no_activation,
        severity=AlertSeverity.p1,
        affected_step="activation_1",
        metric_type="conversion",
        required_sources=("product",),
        condition=lambda m: (
            _has("product")(m)
            and (m.signup or 0) > 0
            and (m.activation_1 or 0) == 0
        ),
        hypothesis_template=(
            "{signup} регистраций, но ни один пользователь не дошёл до первого "
            "шага активации. Вероятная зона проблемы -- онбординг сразу после регистрации."
        ),
        check_action_template="Проверить путь после регистрации — Аналитик Воронки попробует сделать это автоматически по доступным данным.",
        do_not_action_template="Не менять рекламу на основании этого сигнала -- текущий главный сигнал указывает на онбординг после регистрации, но данных пока мало.",
        sample_size_fn=lambda m: m.signup or 0,
        payload_fn=lambda m: {"signup": m.signup or 0},
    ),

    Rule(
        rule_id="activation_1_no_activation_2",
        title="Активация (шаг 1) без активации (шаг 2)",
        category=AlertCategory.activation_drop,
        severity=AlertSeverity.p2,
        affected_step="activation_2",
        metric_type="conversion",
        required_sources=("product",),
        condition=lambda m: (
            _has("product")(m)
            and (m.activation_1 or 0) > 0
            and (m.activation_2 or 0) == 0
        ),
        hypothesis_template=(
            "{activation_1} пользователей прошли первый шаг активации, но ни "
            "один не дошёл до второго. Вероятная зона проблемы -- следующий шаг после первого."
        ),
        check_action_template="Проверить, понятен ли пользователю следующий шаг после первой активации.",
        do_not_action_template="Не менять рекламу и онбординг до первого шага одновременно -- сначала проверить именно этот переход.",
        sample_size_fn=lambda m: m.activation_1 or 0,
        payload_fn=lambda m: {"activation_1": m.activation_1 or 0},
    ),

    Rule(
        rule_id="payments_started_no_success",
        title="Начатые оплаты без успешных",
        category=AlertCategory.payments_started_no_success,
        severity=AlertSeverity.p1,
        affected_step="payment_success",
        metric_type="conversion",
        required_sources=("product",),
        condition=lambda m: (
            _has("product")(m)
            and (m.payment_started or 0) >= MIN_PAYMENT_ATTEMPTS_FOR_PAYMENT_ALERT
            and (m.payment_success or 0) == 0
        ),
        hypothesis_template=(
            "{payment_started} начатых оплат, 0 успешных. Вероятная зона проблемы -- "
            "форма оплаты, способ платежа или техническая ошибка в YooKassa."
        ),
        check_action_template="Пройти оплату самостоятельно тестовым платежом, проверить логи YooKassa.",
        do_not_action_template="Не менять тарифы и цены -- проблема похожа на техническую, не на ценовую.",
        sample_size_fn=lambda m: m.payment_started or 0,
        payload_fn=lambda m: {"payment_started": m.payment_started or 0},
    ),

    Rule(
        rule_id="pending_payments",
        title="Зависшие платежи",
        category=AlertCategory.pending_payments,
        severity=AlertSeverity.p2,
        affected_step="payment_success",
        metric_type="conversion",
        required_sources=("product",),
        condition=lambda m: _has("product")(m) and (m.pending_payments or 0) > 0,
        hypothesis_template=(
            "{pending_payments} платежей в подвешенном статусе. Может быть "
            "нормальной задержкой обработки, а может быть зависшим webhook."
        ),
        check_action_template="Проверить статус этих платежей в кабинете YooKassa.",
        do_not_action_template="Не повторять платёж и не менять настройки оплаты до проверки.",
        sample_size_fn=lambda m: m.pending_payments or 0,
        payload_fn=lambda m: {"pending_payments": m.pending_payments or 0},
    ),

    Rule(
        rule_id="metrics_discrepancy",
        title="Расхождение продукта и Метрики",
        category=AlertCategory.metrics_discrepancy,
        severity=AlertSeverity.p2,
        affected_step="signup",
        metric_type="conversion",
        required_sources=("product", "metrika"),
        condition=lambda m: (
            _has("product", "metrika")(m)
            and (m.signup or 0) > 0
            and (m.metrika_signup or 0) == 0
        ),
        hypothesis_template=(
            "Продукт показывает {signup} регистраций, а цель register_success в "
            "Метрике -- 0. Вероятная причина -- не настроена цель или счётчик не "
            "стоит на странице успеха."
        ),
        check_action_template="Проверить настройку цели register_success и установку счётчика Метрики на странице успеха.",
        do_not_action_template="Не делать выводов про рекламу на основе данных Метрики, пока расхождение не объяснено.",
        sample_size_fn=lambda m: m.signup or 0,
        payload_fn=lambda m: {"signup": m.signup or 0},
    ),
]


def make_low_signup_conversion_rule(
    min_clicks: float = MIN_CLICKS_FOR_CONVERSION_CHECK,
    min_conversion_percent: float = MIN_SIGNUP_CONVERSION_WARN_PERCENT,
) -> Rule:
    """
    Не P1 -- это "стоит присмотреться", а не "проблема подтверждена".
    Срабатывает только при достаточном объёме кликов (по умолчанию 100+),
    чтобы не путать статистический шум с реальной низкой конверсией.
    Учитывая default sample threshold confidence.py (medium >= 50 кликов),
    при min_clicks=100 это правило само по себе уже будет давать high confidence.

    Параметризовано, а не захардкожено в условии, чтобы пороги можно было
    переопределить из Project.settings_json["thresholds"] в будущем без
    правки кода правила.
    """

    def condition(m: NormalizedMetrics) -> bool:
        if not _has("direct", "product")(m):
            return False
        clicks = m.clicks or 0
        signup = m.signup or 0
        if clicks < min_clicks or signup <= 0:
            return False
        conversion_percent = (signup / clicks) * 100
        return conversion_percent < min_conversion_percent

    def payload_fn(m: NormalizedMetrics) -> dict:
        clicks = m.clicks or 0
        signup = m.signup or 0
        conversion_percent = (signup / clicks) * 100 if clicks else 0.0
        return {
            "clicks": clicks,
            "signup": signup,
            "conversion_percent": round(conversion_percent, 1),
        }

    return Rule(
        rule_id="low_signup_conversion",
        title="Низкая конверсия в регистрацию",
        category=AlertCategory.traffic_no_signups,
        severity=AlertSeverity.p2,
        affected_step="signup",
        metric_type="traffic",
        required_sources=("direct", "product"),
        condition=condition,
        hypothesis_template=(
            "За тот же период было {clicks} кликов из Директа и {signup} регистраций "
            "в продукте -- соотношение {conversion_percent}%, ниже ожидаемого уровня. "
            "Атрибуция регистраций к Директу не подтверждена, поэтому вывод предварительный."
        ),
        check_action_template=(
            "Проверить соответствие рекламного объявления, первого экрана лендинга "
            "и формы регистрации -- не обещает ли реклама не то, что показывает сайт."
        ),
        do_not_action_template="Не менять всё сразу -- сначала найти, на каком именно шаге теряются люди.",
        sample_size_fn=lambda m: m.clicks or 0,
        payload_fn=payload_fn,
    )


def get_rules(thresholds: Optional[dict] = None) -> list[Rule]:
    """
    Возвращает полный список правил. thresholds -- опциональный override
    порогов из Project.settings_json["thresholds"], например:
    {"min_clicks_for_conversion_check": 80, "min_signup_conversion_warn_percent": 3.0}

    В v1 вызывается без аргументов почти всегда (используются дефолты из
    config.py), но сигнатура готова для будущей per-project настройки без
    изменения analyzer.py.
    """
    thresholds = thresholds or {}
    rules = list(RULES)
    rules.append(
        make_low_signup_conversion_rule(
            min_clicks=thresholds.get("min_clicks_for_conversion_check", MIN_CLICKS_FOR_CONVERSION_CHECK),
            min_conversion_percent=thresholds.get(
                "min_signup_conversion_warn_percent", MIN_SIGNUP_CONVERSION_WARN_PERCENT
            ),
        )
    )
    return rules


def checkable_categories(sources_ok: set[str], thresholds: Optional[dict] = None) -> set:
    """
    Возвращает множество AlertCategory, для которых хотя бы одно правило
    было checkable при данном наборе доступных источников. Используется
    scheduler.py для построения checked_categories, передаваемых в
    resolve_missing_alerts() -- категория считается проверенной, если её
    можно было оценить в этом прогоне, независимо от того, сработало
    правило или нет.
    """
    result = set()
    for rule in get_rules(thresholds):
        if rule.is_checkable(sources_ok):
            result.add(rule.category)
    return result
    """
    Возвращает полный список правил. thresholds -- опциональный override
    порогов из Project.settings_json["thresholds"], например:
    {"min_clicks_for_conversion_check": 80, "min_signup_conversion_warn_percent": 3.0}

    В v1 вызывается без аргументов почти всегда (используются дефолты из
    config.py), но сигнатура готова для будущей per-project настройки без
    изменения analyzer.py.
    """
    thresholds = thresholds or {}
    rules = list(RULES)
    rules.append(
        make_low_signup_conversion_rule(
            min_clicks=thresholds.get("min_clicks_for_conversion_check", MIN_CLICKS_FOR_CONVERSION_CHECK),
            min_conversion_percent=thresholds.get(
                "min_signup_conversion_warn_percent", MIN_SIGNUP_CONVERSION_WARN_PERCENT
            ),
        )
    )
    return rules
