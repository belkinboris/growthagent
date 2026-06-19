"""
Service layer (вызывается из scheduler.py после analyzer.py).

Отвечает за:
- сохранение MetricSnapshot по каждому окну;
- persistence Alert с дедупликацией по fingerprint;
- escalation / resolve;
- системные алерты integration_down (отдельно от бизнес-алертов);
- подготовку структуры данных для Telegram/LLM (это сами Telegram и LLM
  пока не делают -- только готовят, что им передать).

Этот файл ничего не знает про TruePost -- работает с Project, Integration,
MetricSnapshot, Alert как универсальными моделями.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from sqlmodel import Session, select

from app.analyzer import AlertCandidate, pick_primary_candidate, secondary_candidates
from app.config import get_settings
from app.models import (
    Alert,
    AlertCategory,
    AlertSeverity,
    AlertStatus,
    ConfidenceLevel,
    Integration,
    IntegrationStatus,
    IntegrationType,
    MetricSnapshot,
    Project,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Упорядоченность severity/confidence для сравнения "выросла/не выросла"
# ---------------------------------------------------------------------------

_SEVERITY_RANK = {
    AlertSeverity.info: 0,
    AlertSeverity.p3: 1,
    AlertSeverity.p2: 2,
    AlertSeverity.p1: 3,
    AlertSeverity.p0: 4,
}

_CONFIDENCE_RANK = {
    ConfidenceLevel.low: 0,
    ConfidenceLevel.medium: 1,
    ConfidenceLevel.high: 2,
}

# Если тот же fingerprint (то есть severity/confidence не изменились или даже
# снизились) встречается подряд столько раз -- считаем это эскалацией "по
# упорству", а не по изменению ранга. Иначе проблема, которая держится
# неделю на одном уровне, никогда не подаст голос второй раз.
PERSISTENCE_ESCALATION_THRESHOLD = 5


class AlertChangeType(str, Enum):
    new = "new"
    repeated = "repeated"
    escalated = "escalated"
    resolved = "resolved"


@dataclass
class AlertChange:
    alert: Alert
    change_type: AlertChangeType
    candidate: AlertCandidate | None  # None для resolved (кандидата уже нет)


@dataclass
class CycleResult:
    """
    Итог одного прогона цикла наблюдения. Это и есть подготовленная
    структура для Telegram/LLM -- они оба будут читать именно это, не
    обращаясь к БД напрямую.
    """

    project_id: int
    changes: list[AlertChange] = field(default_factory=list)
    integration_down_changes: list[AlertChange] = field(default_factory=list)
    primary_candidate: AlertCandidate | None = None
    secondary: list[AlertCandidate] = field(default_factory=list)
    metrics_by_window: dict = field(default_factory=dict)

    @property
    def has_notifiable_changes(self) -> bool:
        """
        True, если есть хотя бы одно изменение, которое стоит отправлять в
        Telegram (new / escalated / resolved). repeated не считается --
        это и есть смысл дедупликации, повторная отправка того же самого
        алерта только утомляет.
        """
        notifiable = {AlertChangeType.new, AlertChangeType.escalated, AlertChangeType.resolved}
        all_changes = self.changes + self.integration_down_changes
        return any(c.change_type in notifiable for c in all_changes)


# ---------------------------------------------------------------------------
# Сохранение снэпшотов
# ---------------------------------------------------------------------------


def save_snapshot(
    session: Session,
    project_id: int,
    period_key: str,
    period_start: datetime,
    period_end: datetime,
    source: str,
    metrics: dict,
    as_of: datetime | None,
) -> MetricSnapshot:
    snapshot = MetricSnapshot(
        project_id=project_id,
        period_key=period_key,
        period_start=period_start,
        period_end=period_end,
        source=source,
        as_of=as_of,
        metrics_json=metrics,
    )
    session.add(snapshot)
    session.commit()
    session.refresh(snapshot)
    return snapshot


# ---------------------------------------------------------------------------
# Persistence алертов: дедупликация, escalation, resolve
# ---------------------------------------------------------------------------


def _find_open_alert(session: Session, project_id: int, fingerprint: str) -> Alert | None:
    return session.exec(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.fingerprint == fingerprint,
            Alert.status.in_([AlertStatus.open, AlertStatus.sent, AlertStatus.acknowledged, AlertStatus.escalated]),
        )
    ).first()


def _is_rank_increase(old: Alert, candidate: AlertCandidate) -> bool:
    severity_increased = _SEVERITY_RANK.get(candidate.severity, 0) > _SEVERITY_RANK.get(old.severity, 0)
    confidence_increased = _CONFIDENCE_RANK.get(candidate.confidence, 0) > _CONFIDENCE_RANK.get(old.confidence, 0)
    return severity_increased or confidence_increased


def upsert_alert_from_candidate(
    session: Session,
    project_id: int,
    candidate: AlertCandidate,
) -> AlertChange:
    """
    Главная функция дедупликации. Если открытого алерта с этим fingerprint
    нет -- создаёт новый (change_type=new). Если есть -- обновляет
    occurrence_count/last_seen_at/payload и решает, является ли это
    escalation (по росту severity/confidence ИЛИ по упорству -- N повторов
    подряд без изменения ранга) или просто repeated.
    """
    existing = _find_open_alert(session, project_id, candidate.fingerprint)

    if existing is None:
        alert = Alert(
            project_id=project_id,
            fingerprint=candidate.fingerprint,
            category=candidate.category,
            severity=candidate.severity,
            confidence=candidate.confidence,
            title=candidate.title,
            message=candidate.hypothesis,
            payload_json=candidate.payload,
            status=AlertStatus.open,
            occurrence_count=1,
            escalation_level=0,
        )
        session.add(alert)
        session.commit()
        session.refresh(alert)
        return AlertChange(alert=alert, change_type=AlertChangeType.new, candidate=candidate)

    rank_increased = _is_rank_increase(existing, candidate)

    existing.occurrence_count += 1
    existing.last_seen_at = utcnow()
    existing.payload_json = candidate.payload
    # severity/confidence у алерта всегда отражают последнее наблюдение,
    # даже если ранг не вырос -- иначе сообщение в Telegram будет
    # показывать устаревшие цифры рядом со свежим payload.
    existing.severity = candidate.severity
    existing.confidence = candidate.confidence
    existing.title = candidate.title
    existing.message = candidate.hypothesis

    persistence_escalation = (
        not rank_increased
        and existing.occurrence_count > 0
        and existing.occurrence_count % PERSISTENCE_ESCALATION_THRESHOLD == 0
    )

    if rank_increased or persistence_escalation:
        existing.status = AlertStatus.escalated
        existing.escalation_level += 1
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return AlertChange(alert=existing, change_type=AlertChangeType.escalated, candidate=candidate)

    session.add(existing)
    session.commit()
    session.refresh(existing)
    return AlertChange(alert=existing, change_type=AlertChangeType.repeated, candidate=candidate)


def resolve_missing_alerts(
    session: Session,
    project_id: int,
    seen_fingerprints: set[str],
    checked_categories: set[AlertCategory],
) -> list[AlertChange]:
    """
    Помечает resolved те открытые алерты, чей fingerprint не появился среди
    кандидатов этого прогона -- НО только если категория этого алерта
    реально была проверена в этом прогоне (т.е. все источники, нужные
    правилу, отвечали). Если источник не отвечал, мы не проверяли правило,
    а значит не можем сказать "проблема исчезла" -- это не resolve, а молчание.

    checked_categories -- категории, для которых хотя бы одно правило было
    оценено в этом прогоне (независимо от того, сработало оно или нет).
    Это вычисляется в scheduler.py исходя из того, какие источники были
    sources_ok в каждом окне.
    """
    changes: list[AlertChange] = []

    open_alerts = session.exec(
        select(Alert).where(
            Alert.project_id == project_id,
            Alert.status.in_([AlertStatus.open, AlertStatus.sent, AlertStatus.acknowledged, AlertStatus.escalated]),
        )
    ).all()

    for alert in open_alerts:
        if alert.fingerprint in seen_fingerprints:
            continue
        if alert.category not in checked_categories:
            # Не проверяли в этом прогоне -- молчим, не резолвим.
            continue
        alert.status = AlertStatus.resolved
        alert.resolved_at = utcnow()
        session.add(alert)
        session.commit()
        session.refresh(alert)
        changes.append(AlertChange(alert=alert, change_type=AlertChangeType.resolved, candidate=None))

    return changes


# ---------------------------------------------------------------------------
# integration_down -- системные алерты, отдельно от бизнес-категорий
# ---------------------------------------------------------------------------


def check_integration_freshness(
    session: Session,
    project: Project,
    integration_type: IntegrationType,
    as_of: datetime | None,
    error: str | None,
) -> AlertChange | None:
    """
    Вызывается scheduler.py после попытки получить данные от источника.
    Если был error -- интеграция помечается error. Если as_of слишком
    старый -- stale. В обоих случаях создаётся/обновляется системный
    Alert категории integration_down с fingerprint, не зависящим от
    бизнес-правил (project_id/integration_down/{integration_type}).

    Если всё в порядке -- резолвит существующий integration_down алерт
    для этого источника, если он был открыт.
    """
    settings = get_settings()
    fingerprint = f"{project.id}/integration_down/{integration_type.value}"

    integration = session.exec(
        select(Integration).where(
            Integration.project_id == project.id, Integration.type == integration_type
        )
    ).first()

    is_stale = (
        as_of is not None
        and (utcnow() - as_of) > timedelta(minutes=settings.integration_stale_minutes)
    )

    if error:
        if integration:
            integration.status = IntegrationStatus.error
            integration.last_error = error
            session.add(integration)
            session.commit()

        candidate_like = AlertCandidate(
            fingerprint=fingerprint,
            rule_id="integration_down",
            period_key="n/a",
            title=f"Интеграция {integration_type.value} не отвечает",
            category=AlertCategory.integration_down,
            severity=AlertSeverity.p0,
            affected_step="n/a",
            confidence=ConfidenceLevel.high,
            hypothesis=f"Не удалось получить данные от {integration_type.value}: {error}",
            check_action="Проверить токен доступа и доступность сервиса.",
            do_not_action="Не делать выводов о бизнесе по отсутствующим данным из этого источника.",
            payload={"error": error},
        )
        return upsert_alert_from_candidate(session, project.id, candidate_like)

    if is_stale:
        if integration:
            integration.status = IntegrationStatus.stale
            session.add(integration)
            session.commit()

        candidate_like = AlertCandidate(
            fingerprint=fingerprint,
            rule_id="integration_down",
            period_key="n/a",
            title=f"Данные {integration_type.value} устарели",
            category=AlertCategory.integration_down,
            severity=AlertSeverity.p0,
            affected_step="n/a",
            confidence=ConfidenceLevel.high,
            hypothesis=f"Последние данные от {integration_type.value} датированы {as_of}, это старше допустимого порога.",
            check_action="Проверить, что интеграция продолжает синхронизироваться.",
            do_not_action="Не делать выводов о бизнесе по устаревшим данным.",
            payload={"as_of": as_of.isoformat() if as_of else None},
        )
        return upsert_alert_from_candidate(session, project.id, candidate_like)

    # Источник в порядке -- резолвим, если был открытый integration_down алерт
    if integration:
        integration.status = IntegrationStatus.ok
        integration.last_sync_at = utcnow()
        integration.last_error = None
        session.add(integration)
        session.commit()

    existing = _find_open_alert(session, project.id, fingerprint)
    if existing:
        existing.status = AlertStatus.resolved
        existing.resolved_at = utcnow()
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return AlertChange(alert=existing, change_type=AlertChangeType.resolved, candidate=None)

    return None


# ---------------------------------------------------------------------------
# Главная точка входа для одного прогона цикла (вызывается scheduler.py)
# ---------------------------------------------------------------------------


def process_cycle(
    session: Session,
    project_id: int,
    candidates: list[AlertCandidate],
    checked_categories: set[AlertCategory],
    metrics_by_window: dict,
) -> CycleResult:
    """
    Принимает уже посчитанные analyzer.py candidates (бизнес-правила) и
    собирает persistence-изменения. integration_down обрабатывается
    ОТДЕЛЬНО, заранее, в scheduler.py через check_integration_freshness()
    для каждого источника -- эта функция получает только бизнес-кандидатов.

    Возвращает CycleResult с подготовленной структурой для Telegram/LLM.
    """
    changes: list[AlertChange] = []
    seen_fingerprints: set[str] = set()

    for candidate in candidates:
        change = upsert_alert_from_candidate(session, project_id, candidate)
        changes.append(change)
        seen_fingerprints.add(candidate.fingerprint)

    resolved = resolve_missing_alerts(session, project_id, seen_fingerprints, checked_categories)
    changes.extend(resolved)

    primary = pick_primary_candidate(candidates)
    secondary = secondary_candidates(candidates, primary)

    return CycleResult(
        project_id=project_id,
        changes=changes,
        primary_candidate=primary,
        secondary=secondary,
        metrics_by_window=metrics_by_window,
    )
