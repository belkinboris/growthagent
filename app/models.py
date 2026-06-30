"""
Модели данных Growth Agent.

Ядро не знает про TruePost/АвтоПост. Project -- универсальная сущность
для "любого подключённого digital-проекта". funnel_mapping для перевода
полей конкретного продукта в нормализованные ключи воронки хранится в
Project.settings_json, а не зашит в код.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel, JSON, Column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ActionMode(str, Enum):
    watch_only = "watch_only"
    recommend_only = "recommend_only"
    approval_required = "approval_required"
    autopilot_limited = "autopilot_limited"


class AlertStatus(str, Enum):
    open = "open"
    sent = "sent"
    acknowledged = "acknowledged"
    resolved = "resolved"
    escalated = "escalated"
    snoozed = "snoozed"


class AlertSeverity(str, Enum):
    p0 = "P0"
    p1 = "P1"
    p2 = "P2"
    p3 = "P3"
    info = "info"


class AlertCategory(str, Enum):
    traffic_no_signups = "traffic_no_signups"
    signups_no_activation = "signups_no_activation"
    activation_drop = "activation_drop"
    payments_started_no_success = "payments_started_no_success"
    pending_payments = "pending_payments"
    metrics_discrepancy = "metrics_discrepancy"
    integration_down = "integration_down"  # инфраструктурная, не бизнес-проблема


class ConfidenceLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class IntegrationType(str, Enum):
    project_metrics_api = "project_metrics_api"  # внутренний API подключённого продукта
    metrika = "metrika"
    direct = "direct"
    yookassa = "yookassa"
    telegram = "telegram"
    llm = "llm"


class IntegrationStatus(str, Enum):
    not_configured = "not_configured"
    ok = "ok"
    error = "error"
    stale = "stale"  # данные есть, но as_of слишком старый


# ---------------------------------------------------------------------------


class Project(SQLModel, table=True):
    """
    Один подключённый digital-проект. В v1 активен ровно один Project
    (заполняется из .env при старте), но модель не предполагает этого
    ограничения -- это просто текущая практика использования.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    type: str  # telegram_saas, mobile_app, web_app, ...
    base_url: Optional[str] = None
    main_goal: Optional[str] = None
    connector_name: str = "truepost"  # имя модуля в connectors/
    is_active: bool = True

    # funnel_mapping и metrika_goal_mapping живут здесь, а не в коде.
    # Пример структуры -- см. CONTRACT.md
    settings_json: dict = Field(default_factory=dict, sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=utcnow)


class FunnelStep(SQLModel, table=True):
    """
    Описание одного шага нормализованной воронки для конкретного проекта.
    Например, для АвтоПоста: key=activation_1, title="Канал создан".
    Используется в UI для подписи воронки человеческим языком.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id")
    key: str  # traffic | signup | activation_1 | activation_2 | payment_started | payment_success | revenue
    title: str
    order: int = 0
    description: Optional[str] = None


class Integration(SQLModel, table=True):
    """
    Статус подключения одного внешнего источника данных для проекта.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id")
    type: IntegrationType
    status: IntegrationStatus = IntegrationStatus.not_configured
    settings_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    last_sync_at: Optional[datetime] = None
    last_error: Optional[str] = None


class MetricSnapshot(SQLModel, table=True):
    """
    Один замер метрик за определённое окно (period_key) из определённого
    источника (source). metrics_json хранит данные как пришли от источника
    ПОСЛЕ нормализации воронки (см. CONTRACT.md) -- то есть ключи внутри
    metrics_json уже traffic/signup/activation_1/... а не users_created.
    Исходные "сырые" поля можно дополнительно сохранить под ключом "_raw"
    внутри того же JSON, если нужно для дебага.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id")
    period_key: str  # "3h" | "24h" | "7d"
    period_start: datetime
    period_end: datetime
    source: str  # "project_metrics_api" | "metrika" | "direct" | "yookassa"
    as_of: Optional[datetime] = None  # момент, на который источник насчитал данные
    metrics_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


class MetricBaseline(SQLModel, table=True):
    """
    Норма по метрике для проекта. В v1 таблица существует, но почти не
    используется -- правила работают на жёстких порогах. Заполняется
    руками или остаётся пустой.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id")
    metric_key: str
    baseline_value: Optional[float] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    source: Optional[str] = None
    window: Optional[str] = None  # "3h" | "24h" | "7d"
    sample_size: Optional[int] = None
    confidence: Optional[ConfidenceLevel] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Alert(SQLModel, table=True):
    """
    Один сигнал о проблеме. fingerprint используется для дедупликации --
    одинаковый fingerprint при открытом алерте не создаёт новый, а
    обновляет occurrence_count и last_seen_at существующего.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id")

    fingerprint: str  # стабильный хэш категории + ключевых параметров
    category: AlertCategory
    severity: AlertSeverity
    confidence: ConfidenceLevel

    title: str
    message: str
    payload_json: dict = Field(default_factory=dict, sa_column=Column(JSON))

    status: AlertStatus = AlertStatus.open
    occurrence_count: int = 1
    escalation_level: int = 0

    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    sent_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    snooze_until: Optional[datetime] = None

    created_at: datetime = Field(default_factory=utcnow)


class RecommendationStatus(str, Enum):
    new = "new"
    acknowledged = "acknowledged"
    dismissed = "dismissed"


class Recommendation(SQLModel, table=True):
    """
    В v1 почти не используется напрямую (LLM пишет текст внутри Alert.message),
    но таблица зарезервирована для будущего -- когда рекомендации станут
    отдельными от алертов структурированными объектами с собственным циклом
    жизни (например, для recommend_only режима).
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id")
    title: str
    body: str
    category: Optional[str] = None
    priority: int = 2
    status: RecommendationStatus = RecommendationStatus.new
    created_at: datetime = Field(default_factory=utcnow)


class AgentRun(SQLModel, table=True):
    """
    Журнал одного прогона цикла наблюдения. ok=False с заполненным
    errors_json означает, что часть источников не ответила -- сам прогон
    всё равно считается завершённым (partial success), это не падение сервиса.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id")
    mode: ActionMode = ActionMode.watch_only
    summary: Optional[str] = None
    input_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    output_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    ok: bool = True
    errors_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Direct Deep Diagnostics
# ---------------------------------------------------------------------------


class AttributionStatus(str, Enum):
    """
    Насколько достоверно регистрации/события продукта связаны с трафиком
    из Директа. В v1 у нас нет UTM-сквозной аналитики между Direct и
    TruePost (TruePost отдаёт просто "N регистраций за период", без
    привязки к источнику трафика) -- поэтому почти всегда not_available.
    Это поле существует, чтобы агент НИКОГДА не формулировал "клики дали
    регистрации" как причинно-следственную связь, если она не подтверждена.
    """
    confirmed = "confirmed"          # есть сквозная атрибуция (UTM -> событие)
    partial = "partial"              # есть косвенная связь (например, по времени)
    not_available = "not_available"  # регистрации считаются только как общее число за период


class DirectDimensionType(str, Enum):
    campaign = "campaign"
    ad_group = "ad_group"
    query = "query"
    keyword = "keyword"


class DirectGranularSnapshot(SQLModel, table=True):
    """
    Универсальная модель для granular-данных Директа (campaign/ad_group/
    query/keyword level). Одна таблица на все уровни вместо трёх отдельных
    моделей -- dimension_type различает уровень, parent_dimension_id
    связывает иерархию (ad_group знает свой campaign_id, query знает свой
    ad_group_id) без отдельных foreign key на разные родительские таблицы.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id")

    period_key: str  # "24h" | "7d" | "14d" | "30d"
    period_start: datetime
    period_end: datetime

    dimension_type: DirectDimensionType
    dimension_id: str          # campaign_id, ad_group_id, query text, или keyword text
    dimension_name: Optional[str] = None   # campaign_name, ad_group_name -- если доступно
    parent_dimension_id: Optional[str] = None  # campaign_id для ad_group, ad_group_id для query

    metrics_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    # ожидаемые ключи внутри metrics_json: impressions, clicks, cost, ctr,
    # cpc, conversions (опционально, если Direct API их отдаёт для проекта)

    created_at: datetime = Field(default_factory=utcnow)


class DeepDiagnosticsCache(SQLModel, table=True):
    """
    Кэш результата deep diagnostics, чтобы не дёргать granular-отчёты
    Директа на каждый /run -- только когда нужно (см. service.py:
    should_run_deep_diagnostics()). created_at + expires_at определяют,
    считается ли кэш свежим; trigger_reason фиксирует, почему diagnostics
    запускался (alert/manual/retry), для дебага и истории.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id")

    period_key: str
    trigger_reason: str  # "alert_triggered" | "manual_refresh" | "insufficient_data_retry"
    result_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    # result_json хранит сериализованный DiagnosticsResult (см. diagnostics.py)

    ok: bool = True
    error: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow)
    expires_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Аналитик Воронки 2.0: clean-period tracking, anti-repetition
# ---------------------------------------------------------------------------


class ProjectChangeEvent(SQLModel, table=True):
    """
    Точка изменения проекта (cutoff для clean-period анализа). Лёгкая
    таблица, не файл PROJECT_STATE.md -- решение принято осознанно: чтение
    большого markdown-файла на каждый /analyze дорого по токенам и не даёт
    структурированной даты для фильтрации статистики "до/после". Таблица
    с датой + описанием решает ровно ту задачу, которая нужна агенту
    (отрезать данные по cutoff), без необходимости парсить произвольный
    текст.

    dimension_type/dimension_id -- опциональная привязка к конкретному
    объекту (например, dimension_type="ad_group", dimension_id="12345" для
    "Группа Генератор постов очищена 23.06.2026"). Если изменение глобальное
    (например, "поменяли CTA на лендинге"), оба поля остаются None -- тогда
    cutoff применяется ко всему проекту, не к конкретному сегменту.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id")

    title: str  # "Группа Генератор постов очищена под Telegram-семантику"
    description: Optional[str] = None
    cutoff_at: datetime  # момент, после которого считается "новый" период

    dimension_type: Optional[str] = None  # "ad_group" | "campaign" | "landing" | "product" | None (глобально)
    dimension_id: Optional[str] = None  # конкретный ad_group_id и т.п., если применимо

    created_by: str = "manual"  # "manual" | "agent" -- кто добавил запись
    created_at: datetime = Field(default_factory=utcnow)


class AlertRepeatTracker(SQLModel, table=True):
    """
    Отдельная от Alert таблица для anti-repetition logic в /analyze и
    /deep_direct.

    Архитектурное решение (по запросу архитектора): НЕ простой счётчик
    "сколько раз подряд", а ЖУРНАЛ отдельных показов -- одна строка на
    каждый случай, когда находка была главным выводом. Это нужно, чтобы
    честно считать "N раз ЗА ПОСЛЕДНИЕ 7 ДНЕЙ" (скользящее окно), а не
    только "подряд без перерыва" -- предыдущая версия (один счётчик на
    fingerprint) физически не могла отличить "была главной 2 раза за
    последнюю неделю" от "была главной 2 раза год назад, потом давно не
    появлялась". Журнал решает это: считаем COUNT(*) WHERE shown_at >=
    now() - repeat_window_days.

    key_metric_value -- значение ключевой метрики находки на момент
    показа (например, clicks или register_success), сохраняется, чтобы
    реализовать "resurface_if_metric_changed_by 30%" -- если метрика
    существенно изменилась с последнего показа, находка имеет право
    вернуться как главная, даже если лимит показов в окне исчерпан --
    это не "тот же" вывод по сути, ситуация изменилась.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id")

    finding_fingerprint: str  # стабильный хэш сути находки (rule_id + dimension_id), не цифр
    surface: str  # "deep_direct" | "landing_funnel" | "analyze" -- где показывался

    shown_at: datetime = Field(default_factory=utcnow)
    key_metric_value: Optional[float] = None  # значение ключевой метрики находки на момент показа
    payload_json: dict = Field(default_factory=dict, sa_column=Column(JSON))


class NotificationLog(SQLModel, table=True):
    """
    Журнал отправленных live-уведомлений о пути пользователя.

    Нужна дедупликация: один и тот же шаг пути (регистрация, создание
    канала, feedback, открытие тарифов, начало оплаты) не должен
    уведомляться дважды, даже если /run обнаруживает прирост метрики
    несколько раз подряд (например, до накопления следующего изменения).

    event_key -- стабильный детерминированный ключ, НЕ инкрементальный id.
    Примеры (см. задачу):
      user_registered:<user_id>
      channel_created:<user_id>:<channel_id>
      first_post_feedback:<event_id>
      pricing_viewed:<event_id>
      payment_started:<payment_id>
      payment_success:<payment_id>

    Поскольку TruePost сейчас отдаёт только агрегаты (payment-path-diagnostics),
    а не individual ProductEvent id, event_key в v0 строится из агрегатных
    счётчиков на момент обнаружения прироста -- см. notifications.py
    build_event_key_from_delta(). Это временное решение до появления
    per-event API на стороне TruePost.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id")

    event_key: str = Field(index=True)
    event_type: str  # "user_registered" | "channel_created" | "onboarding_choice" |
                      # "first_post_feedback" | "pricing_viewed" | "payment_started" |
                      # "payment_success" | "payment_failed"
    user_id: Optional[str] = None  # nullable -- не всегда доступен из агрегатов

    sent_at: datetime = Field(default_factory=utcnow)
    payload_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
