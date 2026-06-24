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
from typing import Optional

from sqlmodel import Session, select

from app.analyzer import AlertCandidate, pick_primary_candidate, secondary_candidates
from app.config import get_settings, DEEP_DIAGNOSTICS_CACHE_TTL_HOURS
from app.models import (
    Alert,
    AlertCategory,
    AlertRepeatTracker,
    AlertSeverity,
    AlertStatus,
    ConfidenceLevel,
    DeepDiagnosticsCache,
    Integration,
    IntegrationStatus,
    IntegrationType,
    MetricSnapshot,
    Project,
    ProjectChangeEvent,
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

# Онбординг endpoint в TruePost ещё не реализован. Пока это False, агент не
# показывает кнопку и не пытается делать вид, что диагностика доступна.
ONBOARDING_DIAGNOSTICS_ENABLED = False


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
    # Заполняется scheduler.py, если deep diagnostics был запущен в этом
    # цикле (автоматически по триггеру или из кэша). None, если diagnostics
    # не запускался -- например, Direct не настроен или primary_candidate
    # не относится к категориям, требующим granular-анализа.
    deep_diagnostics: dict | None = None
    # Симметрично deep_diagnostics, но для product onboarding diagnostics.
    # dict с ключом "status" ("ok"/"not_available"/"error") -- None означает
    # "вообще не запускался в этом цикле" (отличается от status="not_available",
    # которое означает "запускался, но endpoint отсутствует в TruePost").
    onboarding_diagnostics: dict | None = None
    # Симметрично onboarding_diagnostics, для landing funnel diagnostics.
    landing_funnel_diagnostics: dict | None = None
    # Независимо от того, запускалась ли диагностика автоматически в этом
    # цикле -- показывать ли кнопки ручного запуска. См. service.
    # should_show_deep_direct_button/should_show_onboarding_button: кнопки
    # не привязаны строго к primary_candidate, пользователь может вручную
    # проверить рекламу/онбординг, даже если сейчас главный сигнал другой.
    show_deep_direct_button: bool = False
    show_onboarding_button: bool = False
    show_landing_funnel_button: bool = False
    milestone_notifications: list[str] = field(default_factory=list)
    # Owner Decision Layer inputs. Populated by scheduler.py; read by Telegram
    # formatting only. They are intentionally optional so older cached objects
    # and tests keep working.
    source_statuses_by_window: dict = field(default_factory=dict)
    previous_metrics_by_window: dict = field(default_factory=dict)

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
        return any(c.change_type in notifiable for c in all_changes) or bool(self.milestone_notifications)


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


# ---------------------------------------------------------------------------
# Direct Deep Diagnostics: триггер и кэш
# ---------------------------------------------------------------------------

# Категории алертов, при которых deep diagnostics имеет смысл запускать
# автоматически -- это все категории, где причина может лежать в рекламе
# (трафик без конверсий, низкая конверсия). Категории про продукт
# (signups_no_activation, payments_started_no_success и т.п.) не запускают
# deep diagnostics -- там проблема не в рекламе, а в продукте, granular
# Direct-анализ не поможет её локализовать.
DEEP_DIAGNOSTICS_TRIGGER_CATEGORIES = {
    AlertCategory.traffic_no_signups,
}


def should_run_deep_diagnostics(primary_candidate: AlertCandidate | None) -> bool:
    """
    Решает, есть ли основание для автоматического запуска deep diagnostics
    в этом цикле. Не проверяет наличие кэша и не проверяет, настроен ли
    Direct -- это отдельные проверки в scheduler.py (там же, где известно,
    доступен ли connector). Эта функция отвечает только на вопрос "алерт
    такой, что deep diagnostics в принципе релевантен".
    """
    if primary_candidate is None:
        return False
    return primary_candidate.category in DEEP_DIAGNOSTICS_TRIGGER_CATEGORIES


def should_show_deep_direct_button(direct_configured: bool, metrics_7d) -> bool:
    """
    Решает, показывать ли кнопку "Проверить рекламу глубже" -- НЕЗАВИСИМО
    от того, что сейчас primary alert (по решению: кнопки диагностики не
    должны зависеть только от primary alert, см. обсуждение с архитектором).
    Условие показа: Direct настроен И есть хоть какие-то рекламные данные
    за 7d-окно (clicks не None) -- иначе кнопка вела бы в пустоту.
    """
    if not direct_configured:
        return False
    if metrics_7d is None:
        return False
    return metrics_7d.clicks is not None and metrics_7d.clicks > 0


# ---------------------------------------------------------------------------
# Product Onboarding Diagnostics: триггер, показ кнопки и кэш
# ---------------------------------------------------------------------------

# Категории алертов, при которых onboarding diagnostics запускается
# АВТОМАТИЧЕСКИ. Симметрично DEEP_DIAGNOSTICS_TRIGGER_CATEGORIES для
# Direct, но для продуктовой стороны воронки -- "регистрация без
# активации" это ровно тот случай, который должен сам себя диагностировать,
# не просить пользователя "проверить руками".
ONBOARDING_DIAGNOSTICS_TRIGGER_CATEGORIES = {
    AlertCategory.signups_no_activation,
}

# Namespace-префикс для period_key в DeepDiagnosticsCache, чтобы не путать
# кэш onboarding-диагностики с кэшем Direct deep diagnostics -- они хранятся
# в одной таблице (project_id + period_key), различить их по period_key
# проще, чем добавлять отдельное поле diagnostic_type в схему сейчас.
ONBOARDING_CACHE_PERIOD_KEY = "onboarding_24h"


def should_run_onboarding_diagnostics(primary_candidate: AlertCandidate | None) -> bool:
    """Автозапуск onboarding diagnostics -- симметрично should_run_deep_diagnostics()."""
    if not ONBOARDING_DIAGNOSTICS_ENABLED:
        return False
    if primary_candidate is None:
        return False
    return primary_candidate.category in ONBOARDING_DIAGNOSTICS_TRIGGER_CATEGORIES


def should_show_onboarding_button(product_configured: bool, metrics_7d) -> bool:
    """
    Решает, показывать ли кнопку "Проверить онбординг" -- НЕЗАВИСИМО от
    primary alert. Условие: product connector настроен И есть хотя бы
    одна регистрация за 7d-окно -- иначе нет смысла запускать диагностику
    пути, по которому никто не прошёл.
    """
    if not ONBOARDING_DIAGNOSTICS_ENABLED:
        return False
    if not product_configured:
        return False
    if metrics_7d is None:
        return False
    return metrics_7d.signup is not None and metrics_7d.signup > 0


# ---------------------------------------------------------------------------
# Milestone notifications
# ---------------------------------------------------------------------------


def _milestone_already_sent(session: Session, project_id: int, fingerprint: str) -> bool:
    existing = session.exec(
        select(AlertRepeatTracker).where(
            AlertRepeatTracker.project_id == project_id,
            AlertRepeatTracker.surface == "milestone",
            AlertRepeatTracker.finding_fingerprint == fingerprint,
        )
    ).first()
    return existing is not None


def _record_milestone(session: Session, project_id: int, fingerprint: str, metric_value: float | None, payload: dict) -> None:
    record_finding_shown(
        session=session,
        project_id=project_id,
        finding_fingerprint=fingerprint,
        surface="milestone",
        payload=payload,
        key_metric_value=metric_value,
    )


def collect_milestone_notifications(
    session: Session,
    project_id: int,
    metrics_7d,
    previous_metrics: dict | None = None,
) -> list[str]:
    """
    Возвращает новые milestone-уведомления и сразу помечает их как показанные.

    Это не алерты и не проблемы. Задача -- дать владельцу бизнеса важные
    отметки без спама: первая регистрация, каждые +5 регистраций, первая
    генерация, первая начатая оплата, первая успешная оплата, резкое изменение CPA.
    """
    if metrics_7d is None:
        return []

    notifications: list[str] = []

    signups = int(metrics_7d.signup or 0)
    activation_2 = int(metrics_7d.activation_2 or 0)
    payment_started = int(metrics_7d.payment_started or 0)
    payment_success = int(metrics_7d.payment_success or 0)
    spend = float(metrics_7d.spend or 0)

    def maybe_send(fingerprint: str, text: str, metric_value: float | None, payload: dict) -> None:
        if _milestone_already_sent(session, project_id, fingerprint):
            return
        _record_milestone(session, project_id, fingerprint, metric_value, payload)
        notifications.append(text)

    if signups > 0:
        if signups < 5:
            maybe_send(
                "signup:first",
                f"Первая регистрация зафиксирована: сейчас {signups}.",
                signups,
                {"metric": "signup", "value": signups},
            )
        else:
            signup_bucket = (signups // 5) * 5
            maybe_send(
                f"signup:bucket:{signup_bucket}",
                f"Регистраций стало {signup_bucket}+ за 7 дней.",
                signups,
                {"metric": "signup", "value": signups, "bucket": signup_bucket},
            )

    if activation_2 > 0:
        maybe_send(
            "activation_2:first",
            f"Появилась первая генерация поста: событий генерации сейчас {activation_2}.",
            activation_2,
            {"metric": "activation_2", "value": activation_2},
        )

    if payment_started > 0:
        maybe_send(
            "payment_started:first",
            f"Появилась первая начатая оплата: попыток оплаты сейчас {payment_started}. Это контрольная отметка, не P1-проблема.",
            payment_started,
            {"metric": "payment_started", "value": payment_started},
        )

    if payment_success > 0:
        maybe_send(
            "payment_success:first",
            f"Появилась первая успешная оплата: успешных оплат сейчас {payment_success}.",
            payment_success,
            {"metric": "payment_success", "value": payment_success},
        )

    # CPA считаем осторожно: только если есть расход и регистрации. Сравнение
    # идёт с предыдущим сохранённым снэпшотом, если он достаточно старый.
    if signups > 0 and spend > 0 and previous_metrics:
        current_cpa = spend / signups
        prev_signups = previous_metrics.get("signup") or 0
        prev_spend = previous_metrics.get("spend") or 0
        if prev_signups and prev_spend:
            previous_cpa = float(prev_spend) / int(prev_signups)
            if previous_cpa > 0:
                relative_change = (current_cpa - previous_cpa) / previous_cpa
                if abs(relative_change) >= 0.35 and abs(current_cpa - previous_cpa) >= 50:
                    direction = "рост" if relative_change > 0 else "снижение"
                    bucket = int(round(current_cpa / 50) * 50)
                    maybe_send(
                        f"cpa:{direction}:{bucket}",
                        f"Резкое {direction} CPA: было примерно {previous_cpa:.0f} ₽, стало примерно {current_cpa:.0f} ₽.",
                        current_cpa,
                        {
                            "metric": "cpa",
                            "current_cpa": round(current_cpa, 2),
                            "previous_cpa": round(previous_cpa, 2),
                            "relative_change": round(relative_change, 2),
                        },
                    )

    return notifications


# ---------------------------------------------------------------------------
# Landing Funnel Diagnostics: триггер, показ кнопки и кэш
# ---------------------------------------------------------------------------

# Landing funnel объединяет обе стороны воронки (Direct clicks -> landing ->
# CTA -> bot -> register -> activation), поэтому триггерится ОБОИМИ
# наборами категорий -- traffic_no_signups (правила A/B про переход с
# рекламы и CTA на лендинге) и signups_no_activation (правило E про
# активацию после регистрации). Не вводим отдельное множество с тем же
# содержимым -- строим объединением существующих, чтобы при добавлении
# новой категории в любое из двух множеств landing funnel не "отстала"
# и не пришлось помнить про третье место для синхронизации.
LANDING_FUNNEL_TRIGGER_CATEGORIES = DEEP_DIAGNOSTICS_TRIGGER_CATEGORIES | ONBOARDING_DIAGNOSTICS_TRIGGER_CATEGORIES

LANDING_FUNNEL_CACHE_PERIOD_KEY = "landing_funnel_24h"


def should_run_landing_funnel_diagnostics(primary_candidate: AlertCandidate | None) -> bool:
    """Автозапуск landing funnel diagnostics -- срабатывает на обеих сторонах воронки."""
    if primary_candidate is None:
        return False
    return primary_candidate.category in LANDING_FUNNEL_TRIGGER_CATEGORIES


def should_show_landing_funnel_button(product_configured: bool, metrics_7d) -> bool:
    """
    Решает, показывать ли кнопку "Проверить лендинг" -- НЕЗАВИСИМО от
    primary alert. Условие: product connector настроен (landing funnel
    endpoint -- часть TruePost internal API) И есть либо клики из Director,
    либо регистрации за 7d -- то есть хоть что-то происходит в воронке,
    что можно диагностировать.
    """
    if not product_configured:
        return False
    if metrics_7d is None:
        return False
    has_clicks = metrics_7d.clicks is not None and metrics_7d.clicks > 0
    has_signups = metrics_7d.signup is not None and metrics_7d.signup > 0
    return has_clicks or has_signups


def get_cached_diagnostics(session: Session, project_id: int, period_key: str) -> DeepDiagnosticsCache | None:
    """
    Возвращает свежую (не просроченную) запись кэша для данного периода,
    если она есть. Свежесть определяется expires_at, не TTL здесь заново --
    expires_at вычисляется один раз при сохранении (save_diagnostics_cache),
    чтобы логика "что считается свежим" жила в одном месте.
    """
    cached = session.exec(
        select(DeepDiagnosticsCache)
        .where(
            DeepDiagnosticsCache.project_id == project_id,
            DeepDiagnosticsCache.period_key == period_key,
            DeepDiagnosticsCache.ok == True,  # noqa: E712
        )
        .order_by(DeepDiagnosticsCache.created_at.desc())
    ).first()

    if cached is None:
        return None

    if cached.expires_at:
        expires_at = cached.expires_at
        # SQLite не хранит timezone -- при чтении обратно datetime приходит
        # naive, даже если при записи был aware (utcnow()). Приводим к aware
        # UTC перед сравнением, иначе TypeError на сравнении naive vs aware.
        # Для Postgres это no-op (там tzinfo сохраняется), для SQLite это
        # необходимое исправление.
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < utcnow():
            return None  # просрочен -- как будто кэша нет

    return cached


def save_diagnostics_cache(
    session: Session,
    project_id: int,
    period_key: str,
    trigger_reason: str,
    result_json: dict,
    ok: bool = True,
    error: str | None = None,
    ttl_hours: int = DEEP_DIAGNOSTICS_CACHE_TTL_HOURS,
) -> DeepDiagnosticsCache:
    """
    Сохраняет результат deep diagnostics (или ошибку) в кэш. ok=False
    используется, когда сам запуск diagnostics не удался (например, Direct
    API вернул ошибку при granular-запросе) -- такая запись НЕ считается
    валидным кэшем в get_cached_diagnostics (фильтр ok == True), поэтому
    следующий цикл попробует снова, не застревая на закэшированной ошибке.
    """
    cache_entry = DeepDiagnosticsCache(
        project_id=project_id,
        period_key=period_key,
        trigger_reason=trigger_reason,
        result_json=result_json,
        ok=ok,
        error=error,
        expires_at=utcnow() + timedelta(hours=ttl_hours),
    )
    session.add(cache_entry)
    session.commit()
    session.refresh(cache_entry)
    return cache_entry


# ---------------------------------------------------------------------------
# Clean-period tracking (Аналитик Воронки 2.0)
# ---------------------------------------------------------------------------


def add_change_event(
    session: Session,
    project_id: int,
    title: str,
    cutoff_at: datetime,
    description: str | None = None,
    dimension_type: str | None = None,
    dimension_id: str | None = None,
    created_by: str = "manual",
) -> ProjectChangeEvent:
    """
    Регистрирует точку изменения проекта для clean-period анализа. Вызывается
    либо вручную (через будущую команду/скрипт), либо агентом, когда он сам
    обнаруживает значимое изменение (например, видит, что у ad_group
    качественно изменился набор запросов после определённой даты -- такое
    можно детектировать, но это эвристика, не точный сигнал, поэтому
    created_by="agent" в этом случае).
    """
    event = ProjectChangeEvent(
        project_id=project_id,
        title=title,
        description=description,
        cutoff_at=cutoff_at,
        dimension_type=dimension_type,
        dimension_id=dimension_id,
        created_by=created_by,
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def get_active_change_events(
    session: Session,
    project_id: int,
    dimension_type: str | None = None,
    dimension_id: str | None = None,
) -> list[ProjectChangeEvent]:
    """
    Возвращает события изменений, релевантные для данного измерения (или
    все глобальные события, если dimension_type/dimension_id не переданы).
    Используется diagnostics.py, чтобы решить, нужно ли отрезать "грязный"
    период перед анализом конкретной группы/кампании/проекта целиком.
    """
    query = select(ProjectChangeEvent).where(ProjectChangeEvent.project_id == project_id)

    if dimension_type is not None and dimension_id is not None:
        # Релевантны события именно для этого измерения ИЛИ глобальные
        # (dimension_type IS NULL) -- глобальное изменение (например, смена
        # CTA на лендинге) влияет на все срезы, не только на один.
        query = query.where(
            (
                (ProjectChangeEvent.dimension_type == dimension_type)
                & (ProjectChangeEvent.dimension_id == dimension_id)
            )
            | (ProjectChangeEvent.dimension_type.is_(None))
        )

    return list(session.exec(query.order_by(ProjectChangeEvent.cutoff_at.desc())).all())


def get_latest_cutoff(
    session: Session,
    project_id: int,
    dimension_type: str | None = None,
    dimension_id: str | None = None,
) -> Optional[datetime]:
    """
    Возвращает самый свежий cutoff_at, релевантный для измерения, или None,
    если изменений не зарегистрировано. Это и есть "где начинается
    clean-period" -- всё, что раньше этой даты, должно анализироваться
    отдельно или не смешиваться с данными после.
    """
    events = get_active_change_events(session, project_id, dimension_type, dimension_id)
    if not events:
        return None

    # SQLite не хранит timezone -- при чтении обратно datetime приходит
    # naive, даже если при записи был aware (см. ту же проблему в
    # service.get_cached_diagnostics). Приводим к aware UTC перед max(),
    # иначе сравнение нескольких cutoff_at между собой может упасть с
    # TypeError, если хотя бы один из них naive.
    def _as_aware(dt: datetime) -> datetime:
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    return max(_as_aware(e.cutoff_at) for e in events)


# ---------------------------------------------------------------------------
# Anti-repetition logic (Аналитик Воронки 2.0)
# ---------------------------------------------------------------------------
#
# Архитектурное решение: НЕ "подряд без перерыва", а скользящее окно по
# календарным дням. Находка не должна быть главным выводом более
# max_main_repeats_in_window раз за последние repeat_window_days дней --
# даже если между показами был перерыв (находка "отдохнула" один день и
# вернулась). Старая версия логики ("подряд") давала мерцание -- находка
# гасла на один день и тут же возвращалась, что не решало исходную
# проблему ("одна и та же находка третий день подряд"). Window-based
# версия решает её строже: учитывает все показы за последнюю неделю,
# не только непосредственно предыдущий запуск.

REPEAT_WINDOW_DAYS = 7
MAX_MAIN_REPEATS_IN_WINDOW = 2
RESURFACE_IF_METRIC_CHANGED_BY = 0.30  # 30% -- доля изменения ключевой метрики


def record_finding_shown(
    session: Session,
    project_id: int,
    finding_fingerprint: str,
    surface: str,
    payload: dict,
    key_metric_value: float | None = None,
) -> AlertRepeatTracker:
    """
    Добавляет ОТДЕЛЬНУЮ запись о показе находки как главного вывода --
    не обновляет существующую строку, не инкрементирует счётчик. Каждый
    вызов = новая строка в журнале с собственным shown_at. Это и даёт
    возможность считать "сколько раз ЗА ОКНО", а не только "общее число
    раз когда-либо" -- старые показы естественным образом перестают
    учитываться, когда выходят за repeat_window_days, без отдельной
    операции "сброса".

    key_metric_value -- значение ключевой метрики этой находки на момент
    показа (например clicks для irrelevant_query_cluster, или
    clicks_share для ad_group_budget_drain) -- используется
    should_suppress_as_primary для resurface-логики.
    """
    tracker = AlertRepeatTracker(
        project_id=project_id,
        finding_fingerprint=finding_fingerprint,
        surface=surface,
        key_metric_value=key_metric_value,
        payload_json=payload,
    )
    session.add(tracker)
    session.commit()
    session.refresh(tracker)
    return tracker


def _extract_key_metric(payload: dict) -> Optional[float]:
    """
    Извлекает "главное число" находки из payload для resurface-сравнения.
    Порядок полей отражает то, что обычно является самой значимой цифрой
    для каждого типа находки (clicks -- для query cluster и budget drain,
    clicks_share -- запасной вариант). Если ни одного поля нет -- None,
    тогда resurface по изменению метрики просто не сработает (откатится
    на обычную window-логику), не будет ошибки сравнения с None.
    """
    for key in ("clicks", "clicks_share", "cost"):
        if key in payload and payload[key] is not None:
            try:
                return float(payload[key])
            except (TypeError, ValueError):
                continue
    return None


def should_suppress_as_primary(
    session: Session,
    project_id: int,
    finding_fingerprint: str,
    surface: str,
    current_payload: dict | None = None,
    repeat_window_days: int = REPEAT_WINDOW_DAYS,
    max_main_repeats_in_window: int = MAX_MAIN_REPEATS_IN_WINDOW,
    resurface_if_metric_changed_by: float = RESURFACE_IF_METRIC_CHANGED_BY,
) -> bool:
    """
    True, если находка уже была главным выводом max_main_repeats_in_window
    раз за последние repeat_window_days дней -- и при этом её ключевая
    метрика НЕ изменилась существенно с последнего показа.

    Три условия снятия подавления (любое освобождает находку, по
    решению архитектора):
    1. Появились новые данные -- здесь это означает: метрика изменилась
       на resurface_if_metric_changed_by и более относительно последнего
       показа (current_payload передаёт актуальные цифры).
    2. Метрика не изменилась существенно -- НЕ освобождает (это и есть
       основной случай подавления, "ничего не поменялось").
    3. Пользователь явно запросил эту тему -- это НЕ обрабатывается
       здесь: явный запрос (например /deep_direct с force=true от кнопки)
       должен идти через ПОЛНОСТЬЮ отдельный путь, не вызывающий
       should_suppress_as_primary вообще -- подавление применимо только
       к автоматическому выбору главного вывода, не к прямому запросу
       пользователя посмотреть конкретную находку.

    Не вызывает record_finding_shown сама -- разделение ответственности:
    эта функция только читает и решает, вызывающий код сам пишет историю.
    """
    cutoff = utcnow() - timedelta(days=repeat_window_days)

    recent_shows = session.exec(
        select(AlertRepeatTracker).where(
            AlertRepeatTracker.project_id == project_id,
            AlertRepeatTracker.finding_fingerprint == finding_fingerprint,
            AlertRepeatTracker.surface == surface,
        ).order_by(AlertRepeatTracker.shown_at.desc())
    ).all()

    # SQLite naive/aware datetime -- та же защита, что в get_cached_diagnostics
    # и get_latest_cutoff выше. shown_at может прийти naive при чтении из
    # SQLite, даже если был записан как aware -- приводим перед сравнением.
    def _as_aware(dt: datetime) -> datetime:
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    recent_shows_in_window = [s for s in recent_shows if _as_aware(s.shown_at) >= cutoff]

    if len(recent_shows_in_window) < max_main_repeats_in_window:
        return False  # лимит окна не достигнут -- показывать можно

    # Лимит достигнут -- проверяем, не изменилась ли метрика существенно
    # с последнего показа. Если current_payload не передан, resurface не
    # проверяется -- подавление действует безусловно (это нормально для
    # вызовов, где метрика не релевантна, например onboarding-находки).
    if current_payload is not None and recent_shows_in_window:
        last_show = recent_shows_in_window[0]  # самый свежий, т.к. order_by desc
        last_value = last_show.key_metric_value
        current_value = _extract_key_metric(current_payload)

        if last_value is not None and current_value is not None and last_value != 0:
            relative_change = abs(current_value - last_value) / abs(last_value)
            if relative_change >= resurface_if_metric_changed_by:
                return False  # метрика существенно изменилась -- находка может вернуться

    return True


def get_recent_finding_history(
    session: Session,
    project_id: int,
    surface: str,
    repeat_window_days: int = REPEAT_WINDOW_DAYS,
) -> list[AlertRepeatTracker]:
    """
    Возвращает все показы за окно для surface -- используется, чтобы
    собрать раздел "известные риски" (находки, подавленные как main, но
    не пропавшие совсем -- архитектор явно требует не скрывать их
    полностью, только убрать из главного вывода).
    """
    cutoff = utcnow() - timedelta(days=repeat_window_days)

    def _as_aware(dt: datetime) -> datetime:
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    all_shows = session.exec(
        select(AlertRepeatTracker).where(
            AlertRepeatTracker.project_id == project_id,
            AlertRepeatTracker.surface == surface,
        )
    ).all()
    return [s for s in all_shows if _as_aware(s.shown_at) >= cutoff]


# ---------------------------------------------------------------------------
# Динамика день-к-дню (Аналитик Воронки 2.0 — доводка по фидбэку архитектора)
# ---------------------------------------------------------------------------


def get_previous_snapshot(
    session: Session,
    project_id: int,
    period_key: str,
    min_age_hours: float = 1.0,
) -> Optional[MetricSnapshot]:
    """
    Возвращает предыдущий снэпшот того же period_key, СТАРШЕ min_age_hours
    от текущего момента -- нужно, чтобы при ручном повторном /run в течение
    одного часа агент не сравнивал текущие данные с собственным снэпшотом
    "только что", выдавая "+0 ко всему" как мнимую динамику. min_age_hours=1
    по умолчанию -- разумный компромисс: даже при ручном тестировании раз в
    несколько минут не считается "новым днём", но и не требует ждать сутки.

    Возвращает None, если истории нет -- вызывающий код должен честно
    написать "нет данных для сравнения", не подставлять 0 как "изменений нет"
    (это разные по смыслу вещи: "не выросло" vs "не с чем сравнить").
    """
    cutoff = utcnow() - timedelta(hours=min_age_hours)

    def _as_aware(dt: datetime) -> datetime:
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    candidates = session.exec(
        select(MetricSnapshot)
        .where(
            MetricSnapshot.project_id == project_id,
            MetricSnapshot.period_key == period_key,
        )
        .order_by(MetricSnapshot.created_at.desc())
        .limit(20)  # запас, чтобы найти достаточно старую запись без полного скана таблицы
    ).all()

    for snap in candidates:
        if _as_aware(snap.created_at) <= cutoff:
            return snap

    return None


def extract_normalized_metrics_from_snapshot(snapshot: MetricSnapshot) -> dict:
    """
    Достаёт нормализованные ключи воронки (signup/activation_1/...) из
    metrics_json снэпшота. Снэпшот хранит "raw" структуру вида
    {"product": {...}, "direct": {...}, ...} (см. scheduler._collect_window:
    raw_for_snapshot) -- но "product" уже нормализован коннектором
    TruePost ДО сохранения, поэтому здесь просто извлекаем нужный под-словарь,
    не делаем повторную нормализацию.

    Возвращает {} если ключа "product" нет вовсе (например, снэпшот только
    с данными Director без продуктовых метрик) -- не бросает исключение,
    вызывающий код получит пустой dict и честно скажет "нет данных".
    """
    product = snapshot.metrics_json.get("product") or {}
    direct = snapshot.metrics_json.get("direct") or {}
    return {
        "signup": product.get("signup"),
        "activation_1": product.get("activation_1"),
        "activation_2": product.get("activation_2"),
        "payment_started": product.get("payment_started"),
        "payment_success": product.get("payment_success"),
        "spend": direct.get("spend"),
        "clicks": direct.get("clicks"),
    }
