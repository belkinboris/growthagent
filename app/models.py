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
