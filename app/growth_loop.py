"""
Growth Loop v1 -- замкнутый цикл принятия growth-решений.

Сигналы (payment_path) -> диагностика узкого места -> ОДНА рекомендация
-> change set -> подтверждение владельца -> эксперимент -> прогресс
-> автоматический вердикт -> следующая рекомендация.

Границы reusable / project-specific:
- ЭТОТ модуль -- универсальное ядро: диагностика по порогам, state machine
  рекомендации и эксперимента, расчёт прогресса, автоматический вердикт.
  Он НЕ знает названий TruePost-шагов и не содержит owner-facing текстов
  конкретного продукта.
- Контент рекомендаций (title/action/change_set/критерии) приходит из
  playbook -- callable, который по области узкого места возвращает dict.
  Для TruePost это app/truepost_playbook.py. Другой проект = другой playbook,
  ядро не меняется.

Всё детерминировано: НИКАКИХ LLM-вызовов, только правила и пороги.
Growth Loop только ПРЕДЛАГАЕТ -- ни рекламу, ни продукт сам не меняет.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlmodel import Session, select

from app.models import (
    GrowthExperiment,
    GrowthExperimentStatus,
    GrowthRecommendation,
    GrowthRecommendationStatus,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Пороги диагностики -- конфигурируемые через Project.settings_json
# ["growth_thresholds"], объяснимые (каждый порог = одно простое правило).
# Один tariff_view или один плохой отзыв стратегию не меняют: везде минимумы.
# ---------------------------------------------------------------------------

DEFAULT_THRESHOLDS: dict = {
    # Минимум регистраций за окно, чтобы вообще делать продуктовые выводы.
    "min_registrations": 10,
    # Низкое создание каналов: доля channel_created / registrations ниже -> onboarding.
    "low_channel_rate": 0.5,
    # Минимум отзывов о первом посте, чтобы судить о качестве результата.
    "min_feedback": 5,
    # Если доля bad среди отзывов выше -> чиним первый результат.
    "bad_feedback_share": 0.4,
    # Если доля good выше этого И тарифы открывают меньше min_pricing_rate -> коммерческий мост.
    "good_feedback_share": 0.6,
    # Доля pricing_viewed / registrations ниже которой мост считается сломанным.
    "min_pricing_rate": 0.2,
    # Минимум pricing_viewed чтобы судить о тарифном экране.
    "min_pricing_viewed": 5,
}


def get_thresholds(project_settings: dict | None) -> dict:
    merged = dict(DEFAULT_THRESHOLDS)
    overrides = (project_settings or {}).get("growth_thresholds") or {}
    for key, value in overrides.items():
        if key in merged:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# Диагностика узкого места (лестница правил, сверху вниз)
# ---------------------------------------------------------------------------

def _n(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def diagnose(payment_path: dict | None, thresholds: dict | None = None) -> dict:
    """
    Возвращает {"area": str, "evidence": [строки], "data_sufficient": bool}.

    Лестница (первый сработавший уровень и есть узкое место):
    tracking -> collect_data -> onboarding -> first_post -> commercial_bridge
    -> pricing_screen -> payment_path -> scale.
    """
    t = thresholds or DEFAULT_THRESHOLDS
    pp = payment_path or {}

    # 0. Tracking incomplete: сначала чиним измерение.
    if payment_path is None or pp.get("registrations") is None:
        return {
            "area": "tracking",
            "evidence": ["данные payment_path недоступны или регистрации не считаются"],
            "data_sufficient": False,
        }

    regs = _n(pp.get("registrations"))
    channels = _n(pp.get("channels_created"))
    fb_good = _n(pp.get("first_post_feedback_good"))
    fb_bad = _n(pp.get("first_post_feedback_bad"))
    fb_total = fb_good + fb_bad
    pricing = _n(pp.get("pricing_viewed"))
    pay_started = _n(pp.get("payment_started"))
    pay_success = _n(pp.get("payment_success"))

    # 1. Мало регистраций -> продуктовый вывод делать нельзя, узкое место
    # в объёме данных (реклама/бюджет), а не в продукте.
    if regs < t["min_registrations"]:
        return {
            "area": "collect_data",
            "evidence": [
                f"регистраций за окно: {regs} (нужно ≥ {t['min_registrations']} для продуктовых выводов)",
            ],
            "data_sufficient": False,
        }

    channel_rate = channels / regs if regs else 0.0

    # 2. Низкое создание каналов -> onboarding.
    if channel_rate < t["low_channel_rate"]:
        return {
            "area": "onboarding",
            "evidence": [
                f"канал создают {channels} из {regs} ({channel_rate:.0%}), порог {t['low_channel_rate']:.0%}",
            ],
            "data_sufficient": True,
        }

    # 3. Каналы создают, но отзывов о первом посте мало -> сигнал не о
    # продукте, а о сборе feedback (это тоже collect_data, но с иным фокусом).
    if fb_total < t["min_feedback"]:
        return {
            "area": "collect_feedback",
            "evidence": [
                f"канал создают {channels} из {regs} ({channel_rate:.0%}) — хорошо",
                f"отзывов о первом посте: {fb_total} (нужно ≥ {t['min_feedback']} для вывода о качестве)",
            ],
            "data_sufficient": False,
        }

    bad_share = fb_bad / fb_total
    good_share = fb_good / fb_total
    pricing_rate = pricing / regs if regs else 0.0

    # 4. Bad feedback доминирует -> первый результат.
    if bad_share >= t["bad_feedback_share"]:
        return {
            "area": "first_post",
            "evidence": [
                f"отзывы: {fb_good} good / {fb_bad} bad — bad {bad_share:.0%} (порог {t['bad_feedback_share']:.0%})",
            ],
            "data_sufficient": True,
        }

    # 5. Feedback преимущественно good, тарифы открывают мало -> коммерческий мост.
    if good_share >= t["good_feedback_share"] and pricing_rate < t["min_pricing_rate"]:
        return {
            "area": "commercial_bridge",
            "evidence": [
                f"отзывы: {fb_good} из {fb_total} положительные ({good_share:.0%})",
                f"тарифы открыли {pricing} из {regs} ({pricing_rate:.0%}), порог {t['min_pricing_rate']:.0%}",
                f"payment_started: {pay_started}",
            ],
            "data_sufficient": True,
        }

    # 6. Тарифы открывают, оплату не начинают -> тарифный экран.
    if pricing >= t["min_pricing_viewed"] and pay_started == 0:
        return {
            "area": "pricing_screen",
            "evidence": [
                f"тарифы открыли {pricing} раз, payment_started = 0",
            ],
            "data_sufficient": True,
        }

    # 7. Оплату начинают, но не завершают -> payment path.
    if pay_started > 0 and pay_success == 0:
        return {
            "area": "payment_path",
            "evidence": [
                f"payment_started: {pay_started}, payment_success: 0",
            ],
            "data_sufficient": True,
        }

    # 8. Оплаты повторяются -> экономика и масштабирование.
    if pay_success > 0:
        return {
            "area": "scale",
            "evidence": [f"payment_success: {pay_success} — проверяем экономику привлечения"],
            "data_sufficient": True,
        }

    # Ничего явно не сломано, но и оплат нет -- копим данные по мосту.
    return {
        "area": "collect_feedback",
        "evidence": ["явного узкого места по порогам нет, оплат нет — копим данные"],
        "data_sufficient": False,
    }


def diagnosis_fingerprint(area: str, payment_path: dict | None) -> str:
    """
    Отпечаток «та же рекомендация на тех же данных». Отклонённую рекомендацию
    не предлагаем повторно, пока отпечаток не изменился (данные выросли).
    Гранулярность нарочно грубая (бакеты по 5 регистраций / 3 отзыва):
    +1 регистрация не считается «новыми данными».
    """
    pp = payment_path or {}
    regs_bucket = _n(pp.get("registrations")) // 5
    fb_bucket = (_n(pp.get("first_post_feedback_good")) + _n(pp.get("first_post_feedback_bad"))) // 3
    pricing_bucket = _n(pp.get("pricing_viewed")) // 3
    return f"{area}:r{regs_bucket}:f{fb_bucket}:p{pricing_bucket}"


# ---------------------------------------------------------------------------
# State machine рекомендации
# ---------------------------------------------------------------------------

def get_active_recommendation(session: Session, project_id: int) -> Optional[GrowthRecommendation]:
    """Единственная рекомендация в статусе proposed (или None)."""
    return session.exec(
        select(GrowthRecommendation).where(
            GrowthRecommendation.project_id == project_id,
            GrowthRecommendation.status == GrowthRecommendationStatus.proposed,
        ).order_by(GrowthRecommendation.created_at.desc())
    ).first()


def get_running_experiment(session: Session, project_id: int) -> Optional[GrowthExperiment]:
    return session.exec(
        select(GrowthExperiment).where(
            GrowthExperiment.project_id == project_id,
            GrowthExperiment.status.in_(  # type: ignore[attr-defined]
                [GrowthExperimentStatus.running, GrowthExperimentStatus.enough_data]
            ),
        ).order_by(GrowthExperiment.started_at.desc())
    ).first()


def get_last_finished_experiment(session: Session, project_id: int) -> Optional[GrowthExperiment]:
    return session.exec(
        select(GrowthExperiment).where(
            GrowthExperiment.project_id == project_id,
            GrowthExperiment.status.in_(  # type: ignore[attr-defined]
                [GrowthExperimentStatus.won, GrowthExperimentStatus.lost,
                 GrowthExperimentStatus.inconclusive]
            ),
        ).order_by(GrowthExperiment.ended_at.desc())
    ).first()


def _fingerprint_blocked(session: Session, project_id: int, fingerprint: str) -> bool:
    """
    True если рекомендация с таким отпечатком уже была отклонена или отложена
    (и срок отсрочки не истёк) -- не предлагаем то же самое на тех же данных.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    prior = session.exec(
        select(GrowthRecommendation).where(
            GrowthRecommendation.project_id == project_id,
            GrowthRecommendation.fingerprint == fingerprint,
            GrowthRecommendation.status.in_(  # type: ignore[attr-defined]
                [GrowthRecommendationStatus.rejected, GrowthRecommendationStatus.deferred]
            ),
        )
    ).all()
    for rec in prior:
        if rec.status == GrowthRecommendationStatus.rejected:
            return True
        if rec.status == GrowthRecommendationStatus.deferred:
            defer_until = rec.defer_until
            if defer_until is None:
                return True
            if defer_until.tzinfo is not None:
                defer_until = defer_until.replace(tzinfo=None)
            if defer_until > now:
                return True
    return False


def propose_if_needed(
    session: Session,
    project_id: int,
    payment_path: dict | None,
    playbook: Callable[[str, dict, dict], Optional[dict]],
    project_settings: dict | None = None,
) -> Optional[GrowthRecommendation]:
    """
    Формирует новую рекомендацию, если: нет активной proposed, нет running
    эксперимента, диагностика дала область, playbook вернул контент, и такой
    же отпечаток не был отклонён/отложен. Возвращает созданную рекомендацию
    или None.
    """
    if get_active_recommendation(session, project_id) is not None:
        return None
    if get_running_experiment(session, project_id) is not None:
        return None

    thresholds = get_thresholds(project_settings)
    diagnosis = diagnose(payment_path, thresholds)
    area = diagnosis["area"]

    content = playbook(area, payment_path or {}, thresholds)
    if content is None:
        return None  # playbook не считает нужным предлагать что-то в этой области

    fingerprint = diagnosis_fingerprint(area, payment_path)
    if _fingerprint_blocked(session, project_id, fingerprint):
        return None

    evidence: list[str] = []
    for line in list(diagnosis["evidence"]) + list(content.get("extra_evidence", [])):
        # Дедуп по началу строки: диагностика и playbook могут говорить об
        # одном и том же факте чуть разными словами.
        if not any(line[:20] == seen[:20] for seen in evidence):
            evidence.append(line)

    rec = GrowthRecommendation(
        project_id=project_id,
        area=area,
        title=content["title"],
        action=content["action"],
        hypothesis=content.get("hypothesis", ""),
        evidence_json=evidence,
        confidence=content.get("confidence", "предварительный сигнал"),
        expected_effect=content.get("expected_effect", ""),
        risk=content.get("risk", ""),
        change_set_json=content.get("change_set", []),
        measure=content.get("measure", ""),
        primary_metric=content.get("primary_metric", ""),
        sample_metric=content.get("sample_metric", "registrations"),
        target_sample=content.get("target_sample", 14),
        min_runtime_days=content.get("min_runtime_days", 3),
        max_runtime_days=content.get("max_runtime_days", 14),
        success_criterion=content.get("success_criterion", ""),
        failure_criterion=content.get("failure_criterion", ""),
        locked_variables_json=content.get("locked_variables", []),
        fingerprint=fingerprint,
    )
    session.add(rec)
    session.commit()
    session.refresh(rec)
    return rec


def accept_recommendation(
    session: Session,
    rec: GrowthRecommendation,
    payment_path: dict | None,
) -> GrowthExperiment:
    """
    Принять: рекомендация фиксируется, создаётся эксперимент, baseline =
    snapshot payment_path В МОМЕНТ принятия. Запрещённые переменные
    переносятся в эксперимент.
    """
    rec.status = GrowthRecommendationStatus.accepted
    rec.decided_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(rec)

    baseline = dict(payment_path or {})
    baseline.pop("_from_cache", None)
    exp = GrowthExperiment(
        project_id=rec.project_id,
        recommendation_id=rec.id,
        title=rec.title,
        area=rec.area,
        hypothesis=rec.hypothesis,
        baseline_json=baseline,
        primary_metric=rec.primary_metric,
        sample_metric=rec.sample_metric,
        target_sample=rec.target_sample,
        min_runtime_days=rec.min_runtime_days,
        max_runtime_days=rec.max_runtime_days,
        success_criterion=rec.success_criterion,
        failure_criterion=rec.failure_criterion,
        locked_variables_json=list(rec.locked_variables_json or []),
        guardrail_json=[],
    )
    session.add(exp)
    session.commit()
    session.refresh(exp)
    return exp


def defer_recommendation(
    session: Session, rec: GrowthRecommendation, days: int = 7
) -> GrowthRecommendation:
    rec.status = GrowthRecommendationStatus.deferred
    rec.decided_at = datetime.now(timezone.utc).replace(tzinfo=None)
    rec.defer_until = rec.decided_at + timedelta(days=days)
    session.add(rec)
    session.commit()
    session.refresh(rec)
    return rec


def reject_recommendation(
    session: Session, rec: GrowthRecommendation, reason: str = ""
) -> GrowthRecommendation:
    rec.status = GrowthRecommendationStatus.rejected
    rec.decided_at = datetime.now(timezone.utc).replace(tzinfo=None)
    rec.reject_reason = (reason or "")[:500]
    session.add(rec)
    session.commit()
    session.refresh(rec)
    return rec


# ---------------------------------------------------------------------------
# Прогресс и автоматический вердикт
# ---------------------------------------------------------------------------

# Виртуальные метрики: считаются из payment_path на лету. Нужны, когда
# правильная выборка эксперимента -- не строка payment_path, а их сумма.
# Пример: для качества первого поста выборка = ВСЕ новые отзывы (good+bad),
# а не регистрации -- иначе rate может превышать 100% (отзывы оставляют и
# старые пользователи, не входящие в новые регистрации).
def get_metric(pp: dict | None, name: str) -> int:
    pp = pp or {}
    if name == "first_post_feedback_total":
        return _n(pp.get("first_post_feedback_good")) + _n(pp.get("first_post_feedback_bad"))
    return _n(pp.get(name))


def _rate(pp: dict, metric: str, sample_metric: str) -> Optional[float]:
    sample = get_metric(pp, sample_metric)
    if sample <= 0:
        return None
    return get_metric(pp, metric) / sample


def experiment_progress(exp: GrowthExperiment, payment_path: dict | None) -> dict:
    """
    Прогресс = прирост sample_metric относительно baseline (новые пользователи
    С МОМЕНТА старта), не абсолют. Возвращает
    {"current_sample", "baseline_rate", "current_rate", "delta_metric", "delta_sample"}.
    current_rate считается ТОЛЬКО по новым данным (дельта метрики / дельта
    выборки), чтобы baseline не размывал результат эксперимента.
    """
    pp = payment_path or {}
    base = exp.baseline_json or {}
    delta_sample = max(0, get_metric(pp, exp.sample_metric) - get_metric(base, exp.sample_metric))
    delta_metric = max(0, get_metric(pp, exp.primary_metric) - get_metric(base, exp.primary_metric))
    baseline_rate = _rate(base, exp.primary_metric, exp.sample_metric)
    current_rate = (delta_metric / delta_sample) if delta_sample > 0 else None
    return {
        "current_sample": delta_sample,
        "delta_metric": delta_metric,
        "baseline_rate": baseline_rate,
        "current_rate": current_rate,
    }


# Честные формулировки достоверности при малых выборках.
def _confidence_words(delta_sample: int, delta_metric: int) -> str:
    if delta_sample >= 30 and delta_metric >= 8:
        return "сильный повторяющийся сигнал"
    if delta_metric >= 3:
        return "предварительный сигнал"
    if delta_metric >= 1:
        return "единичные события, решение по ним не меняем"
    return "данных недостаточно"


def _days_since(dt: datetime) -> float:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return (now - dt).total_seconds() / 86400.0


def maybe_finish_experiment(
    session: Session,
    exp: GrowthExperiment,
    payment_path: dict | None,
) -> Optional[GrowthExperiment]:
    """
    Обновляет current_sample; если достигнут target_sample (и min_runtime)
    или истёк max_runtime -- выносит вердикт и закрывает эксперимент.
    Возвращает эксперимент, если он ТОЛЬКО ЧТО завершился, иначе None.

    Вердикт (детерминированный):
    - won: current_rate по новым данным >= baseline_rate * 1.5 И
      delta_metric >= 3 (не объявляем победу по 1-2 событиям);
    - lost: выборка набрана, current_rate <= baseline_rate (улучшения нет);
    - inconclusive: всё остальное (в т.ч. закрытие по max_runtime без выборки).
    """
    progress = experiment_progress(exp, payment_path)
    exp.current_sample = progress["current_sample"]
    session.add(exp)
    session.commit()

    days_running = _days_since(exp.started_at)
    sample_reached = exp.current_sample >= exp.target_sample and days_running >= exp.min_runtime_days
    time_exceeded = days_running >= exp.max_runtime_days

    if not sample_reached and not time_exceeded:
        return None

    baseline_rate = progress["baseline_rate"] or 0.0
    current_rate = progress["current_rate"]
    delta_metric = progress["delta_metric"]
    confidence = _confidence_words(exp.current_sample, delta_metric)

    def fmt(rate: Optional[float]) -> str:
        return f"{rate:.0%}" if rate is not None else "—"

    if current_rate is not None and delta_metric >= 3 and current_rate >= max(baseline_rate * 1.5, baseline_rate + 0.05):
        exp.status = GrowthExperimentStatus.won
        exp.verdict = "ЭКСПЕРИМЕНТ ВЫИГРАЛ"
        keep = "оставить изменение"
    elif exp.current_sample >= exp.target_sample and (current_rate is None or current_rate <= baseline_rate):
        exp.status = GrowthExperimentStatus.lost
        exp.verdict = "ЭКСПЕРИМЕНТ НЕ СРАБОТАЛ"
        keep = "откатить изменение"
    else:
        exp.status = GrowthExperimentStatus.inconclusive
        exp.verdict = "ДАННЫХ НЕДОСТАТОЧНО"
        keep = "изменение можно оставить, но решение по нему не принято"

    exp.result_summary = (
        f"{exp.primary_metric}: {fmt(baseline_rate)} → {fmt(current_rate)} "
        f"(новых {exp.sample_metric}: {exp.current_sample}, событий: {delta_metric}). "
        f"Достоверность: {confidence}. Действие: {keep}."
    )
    exp.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(exp)
    session.commit()
    session.refresh(exp)
    return exp


def cancel_experiment(session: Session, exp: GrowthExperiment, reason: str = "") -> GrowthExperiment:
    """Откат/отмена вручную: эксперимент закрывается без вердикта о метрике."""
    exp.status = GrowthExperimentStatus.cancelled
    exp.verdict = "ОТМЕНЁН ВЛАДЕЛЬЦЕМ"
    exp.result_summary = (reason or "отменён вручную")[:500]
    exp.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(exp)
    session.commit()
    session.refresh(exp)
    return exp


# ---------------------------------------------------------------------------
# Тик цикла: вызывается из scheduler после обновления payment_path
# ---------------------------------------------------------------------------

def tick(
    session: Session,
    project_id: int,
    payment_path: dict | None,
    playbook: Callable[[str, dict, dict], Optional[dict]],
    project_settings: dict | None = None,
) -> dict:
    """
    Один шаг Growth Loop. Детерминированный, без LLM и без внешних вызовов.
    Возвращает {"finished_experiment": GrowthExperiment|None,
                "new_recommendation": GrowthRecommendation|None}
    -- вызывающий код решает, что отправить владельцу.
    """
    finished = None
    running = get_running_experiment(session, project_id)
    if running is not None:
        finished = maybe_finish_experiment(session, running, payment_path)

    new_rec = None
    # Новую рекомендацию предлагаем только если нет живого эксперимента
    # (одна основная продуктовая проверка одновременно).
    if get_running_experiment(session, project_id) is None:
        new_rec = propose_if_needed(
            session, project_id, payment_path, playbook, project_settings
        )
    return {"finished_experiment": finished, "new_recommendation": new_rec}
