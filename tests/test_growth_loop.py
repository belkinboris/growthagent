"""
Тесты Growth Loop v1 -- замкнутый контур:
диагностика -> рекомендация -> approve/defer/reject -> эксперимент ->
прогресс -> автоматический вердикт -> следующая рекомендация.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app import growth_loop
from app.growth_loop import (
    DEFAULT_THRESHOLDS,
    accept_recommendation,
    defer_recommendation,
    diagnose,
    diagnosis_fingerprint,
    experiment_progress,
    get_active_recommendation,
    get_running_experiment,
    maybe_finish_experiment,
    propose_if_needed,
    reject_recommendation,
    tick,
)
from app.models import (
    GrowthExperiment,
    GrowthExperimentStatus,
    GrowthRecommendation,
    GrowthRecommendationStatus,
    Project,
)
from app.truepost_playbook import truepost_playbook


def _factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    def factory():
        return Session(engine)

    return factory


def _project(session: Session) -> Project:
    p = Project(name="TruePost", type="telegram_saas", is_active=True)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _pp(**kw) -> dict:
    base = dict(
        registrations=20, channels_created=16,
        first_post_feedback_good=7, first_post_feedback_bad=3,
        pricing_viewed=2, payment_started=0, payment_success=0,
    )
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Диагностика (лестница правил)
# ---------------------------------------------------------------------------

class TestDiagnose:

    def test_tracking_when_no_data(self):
        assert diagnose(None)["area"] == "tracking"
        assert diagnose({"registrations": None})["area"] == "tracking"

    def test_collect_data_when_few_registrations(self):
        d = diagnose(_pp(registrations=4))
        assert d["area"] == "collect_data"
        assert d["data_sufficient"] is False

    def test_onboarding_when_low_channel_rate(self):
        d = diagnose(_pp(registrations=20, channels_created=5))
        assert d["area"] == "onboarding"

    def test_collect_feedback_when_few_reviews(self):
        d = diagnose(_pp(first_post_feedback_good=1, first_post_feedback_bad=1))
        assert d["area"] == "collect_feedback"

    def test_first_post_when_bad_dominates(self):
        d = diagnose(_pp(first_post_feedback_good=3, first_post_feedback_bad=4))
        assert d["area"] == "first_post"

    def test_commercial_bridge_main_scenario(self):
        """Главный сценарий: good feedback есть, тарифы не открывают."""
        d = diagnose(_pp(first_post_feedback_good=7, first_post_feedback_bad=3, pricing_viewed=2))
        assert d["area"] == "commercial_bridge"
        assert d["data_sufficient"] is True

    def test_pricing_screen_when_views_but_no_starts(self):
        d = diagnose(_pp(pricing_viewed=6, payment_started=0))
        assert d["area"] == "pricing_screen"

    def test_payment_path_when_starts_but_no_success(self):
        d = diagnose(_pp(pricing_viewed=6, payment_started=2, payment_success=0))
        assert d["area"] == "payment_path"

    def test_scale_when_payments_exist(self):
        d = diagnose(_pp(pricing_viewed=6, payment_started=3, payment_success=2))
        assert d["area"] == "scale"

    def test_one_bad_review_does_not_flip_strategy(self):
        """Один плохой отзыв при недоборе выборки не меняет стратегию."""
        d = diagnose(_pp(first_post_feedback_good=0, first_post_feedback_bad=1))
        assert d["area"] == "collect_feedback"  # не first_post

    def test_thresholds_configurable(self):
        t = dict(DEFAULT_THRESHOLDS, min_registrations=100)
        d = diagnose(_pp(registrations=50), t)
        assert d["area"] == "collect_data"


# ---------------------------------------------------------------------------
# Playbook: каждая область даёт валидный контент
# ---------------------------------------------------------------------------

class TestPlaybook:

    @pytest.mark.parametrize("area", [
        "collect_data", "collect_feedback", "onboarding", "first_post",
        "commercial_bridge", "pricing_screen", "payment_path", "scale",
    ])
    def test_all_areas_have_content(self, area):
        content = truepost_playbook(area, _pp(), DEFAULT_THRESHOLDS)
        assert content is not None
        assert content["title"] and content["action"]
        assert content.get("change_set"), f"{area}: change_set обязателен"
        assert content.get("locked_variables"), f"{area}: locked_variables обязателен"
        assert content.get("success_criterion") and content.get("failure_criterion")

    def test_tracking_gives_none(self):
        assert truepost_playbook("tracking", _pp(), DEFAULT_THRESHOLDS) is None

    def test_no_owner_jargon(self):
        for area in ["commercial_bridge", "pricing_screen", "collect_data"]:
            c = truepost_playbook(area, _pp(), DEFAULT_THRESHOLDS)
            blob = str(c).lower()
            for banned in ["confidence interval", "decision engine", "pmf", "штаб"]:
                assert banned not in blob


# ---------------------------------------------------------------------------
# Рекомендация: propose / accept / defer / reject / fingerprint
# ---------------------------------------------------------------------------

class TestRecommendationFlow:

    def test_propose_creates_single_proposed(self):
        f = _factory()
        with f() as s:
            p = _project(s)
            rec = propose_if_needed(s, p.id, _pp(), truepost_playbook)
            assert rec is not None
            assert rec.status == GrowthRecommendationStatus.proposed
            assert rec.area == "commercial_bridge"
            assert "очередь" in rec.title.lower()
            # Повторный вызов не создаёт вторую
            rec2 = propose_if_needed(s, p.id, _pp(), truepost_playbook)
            assert rec2 is None
            assert get_active_recommendation(s, p.id).id == rec.id

    def test_accept_creates_experiment_with_baseline(self):
        f = _factory()
        with f() as s:
            p = _project(s)
            pp = _pp()
            rec = propose_if_needed(s, p.id, pp, truepost_playbook)
            exp = accept_recommendation(s, rec, pp)
            assert exp.status == GrowthExperimentStatus.running
            assert exp.baseline_json["registrations"] == 20
            assert exp.baseline_json["pricing_viewed"] == 2
            assert exp.primary_metric == "pricing_viewed"
            assert exp.locked_variables_json == rec.locked_variables_json
            assert rec.status == GrowthRecommendationStatus.accepted
            # Пока эксперимент идёт -- новая рекомендация не предлагается
            assert propose_if_needed(s, p.id, pp, truepost_playbook) is None

    def test_rejected_fingerprint_not_reproposed_until_new_data(self):
        f = _factory()
        with f() as s:
            p = _project(s)
            pp = _pp()
            rec = propose_if_needed(s, p.id, pp, truepost_playbook)
            reject_recommendation(s, rec, "не хочу")
            # Те же данные -> не предлагаем
            assert propose_if_needed(s, p.id, pp, truepost_playbook) is None
            # Данные выросли (другой бакет) -> предлагаем снова
            pp2 = _pp(registrations=30, first_post_feedback_good=10)
            rec2 = propose_if_needed(s, p.id, pp2, truepost_playbook)
            assert rec2 is not None
            assert rec2.fingerprint != rec.fingerprint

    def test_deferred_blocks_until_date(self):
        f = _factory()
        with f() as s:
            p = _project(s)
            pp = _pp()
            rec = propose_if_needed(s, p.id, pp, truepost_playbook)
            defer_recommendation(s, rec, days=7)
            assert propose_if_needed(s, p.id, pp, truepost_playbook) is None
            # Срок истёк -> можно снова
            rec.defer_until = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
            s.add(rec); s.commit()
            assert propose_if_needed(s, p.id, pp, truepost_playbook) is not None

    def test_fingerprint_buckets(self):
        assert diagnosis_fingerprint("x", _pp(registrations=20)) == diagnosis_fingerprint("x", _pp(registrations=21))
        assert diagnosis_fingerprint("x", _pp(registrations=20)) != diagnosis_fingerprint("x", _pp(registrations=27))


# ---------------------------------------------------------------------------
# Эксперимент: прогресс и автоматический вердикт
# ---------------------------------------------------------------------------

def _make_experiment(s: Session, p: Project, baseline: dict, **kw) -> GrowthExperiment:
    rec = GrowthRecommendation(
        project_id=p.id, area="commercial_bridge", title="Очередь на неделю",
        action="показать очередь", primary_metric="pricing_viewed",
        sample_metric="registrations", target_sample=kw.get("target_sample", 14),
        status=GrowthRecommendationStatus.accepted,
    )
    s.add(rec); s.commit(); s.refresh(rec)
    exp = GrowthExperiment(
        project_id=p.id, recommendation_id=rec.id, title=rec.title,
        area=rec.area, baseline_json=baseline,
        primary_metric="pricing_viewed", sample_metric="registrations",
        target_sample=kw.get("target_sample", 14),
        min_runtime_days=kw.get("min_runtime_days", 0),
        max_runtime_days=kw.get("max_runtime_days", 14),
        started_at=kw.get("started_at", datetime.now(timezone.utc).replace(tzinfo=None)),
    )
    s.add(exp); s.commit(); s.refresh(exp)
    return exp


class TestExperiment:

    def test_progress_counts_delta_from_baseline(self):
        f = _factory()
        with f() as s:
            p = _project(s)
            exp = _make_experiment(s, p, baseline=_pp(registrations=20, pricing_viewed=2))
            prog = experiment_progress(exp, _pp(registrations=28, pricing_viewed=5))
            assert prog["current_sample"] == 8
            assert prog["delta_metric"] == 3
            assert prog["baseline_rate"] == pytest.approx(0.1)
            assert prog["current_rate"] == pytest.approx(3 / 8)

    def test_not_finished_before_target_and_time(self):
        f = _factory()
        with f() as s:
            p = _project(s)
            exp = _make_experiment(s, p, baseline=_pp(), target_sample=14)
            finished = maybe_finish_experiment(s, exp, _pp(registrations=25, pricing_viewed=3))
            assert finished is None
            assert exp.current_sample == 5
            assert exp.status == GrowthExperimentStatus.running

    def test_verdict_won(self):
        f = _factory()
        with f() as s:
            p = _project(s)
            exp = _make_experiment(s, p, baseline=_pp(registrations=20, pricing_viewed=2), target_sample=14)
            # 14 новых, 5 открытий тарифов на новых: 10% -> 36%
            finished = maybe_finish_experiment(
                s, exp, _pp(registrations=34, pricing_viewed=7))
            assert finished is not None
            assert finished.status == GrowthExperimentStatus.won
            assert finished.verdict == "ЭКСПЕРИМЕНТ ВЫИГРАЛ"
            assert "10%" in finished.result_summary and "36%" in finished.result_summary
            assert finished.ended_at is not None

    def test_verdict_lost(self):
        f = _factory()
        with f() as s:
            p = _project(s)
            exp = _make_experiment(s, p, baseline=_pp(registrations=20, pricing_viewed=4), target_sample=14)
            # 14 новых, 0 новых открытий: 20% -> 0%
            finished = maybe_finish_experiment(
                s, exp, _pp(registrations=34, pricing_viewed=4))
            assert finished.status == GrowthExperimentStatus.lost
            assert finished.verdict == "ЭКСПЕРИМЕНТ НЕ СРАБОТАЛ"
            assert "откатить" in finished.result_summary

    def test_verdict_not_won_on_one_or_two_events(self):
        """1-2 события не объявляются победой даже при высоком rate."""
        f = _factory()
        with f() as s:
            p = _project(s)
            exp = _make_experiment(s, p, baseline=_pp(registrations=20, pricing_viewed=0), target_sample=14)
            finished = maybe_finish_experiment(
                s, exp, _pp(registrations=34, pricing_viewed=2))
            assert finished.status != GrowthExperimentStatus.won

    def test_verdict_inconclusive_on_max_runtime_without_sample(self):
        f = _factory()
        with f() as s:
            p = _project(s)
            exp = _make_experiment(
                s, p, baseline=_pp(registrations=20, pricing_viewed=2),
                target_sample=14, max_runtime_days=7,
                started_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=8),
            )
            finished = maybe_finish_experiment(
                s, exp, _pp(registrations=23, pricing_viewed=3))
            assert finished is not None
            assert finished.status == GrowthExperimentStatus.inconclusive
            assert finished.verdict == "ДАННЫХ НЕДОСТАТОЧНО"

    def test_min_runtime_prevents_instant_verdict(self):
        f = _factory()
        with f() as s:
            p = _project(s)
            exp = _make_experiment(
                s, p, baseline=_pp(registrations=20, pricing_viewed=2),
                target_sample=5, min_runtime_days=3,
            )
            # выборка есть, но min_runtime не прошёл
            finished = maybe_finish_experiment(
                s, exp, _pp(registrations=30, pricing_viewed=6))
            assert finished is None


# ---------------------------------------------------------------------------
# tick: замкнутый цикл + уведомления scheduler-обёртки
# ---------------------------------------------------------------------------

class TestTickAndNotify:

    def test_full_loop(self):
        """Полный контур: propose -> accept -> прогресс -> вердикт -> новое предложение."""
        f = _factory()
        with f() as s:
            p = _project(s)
            pp = _pp()
            # 1. Первый тик: предлагает
            r = tick(s, p.id, pp, truepost_playbook)
            rec = r["new_recommendation"]
            assert rec is not None and rec.area == "commercial_bridge"
            # 2. Пока proposed -- второй тик молчит
            r = tick(s, p.id, pp, truepost_playbook)
            assert r["new_recommendation"] is None and r["finished_experiment"] is None
            # 3. Принимаем
            exp = accept_recommendation(s, rec, pp)
            exp.min_runtime_days = 0
            s.add(exp); s.commit()
            # 4. Тик с прогрессом -- ещё не финиш
            r = tick(s, p.id, _pp(registrations=25, pricing_viewed=4), truepost_playbook)
            assert r["finished_experiment"] is None
            assert get_running_experiment(s, p.id).current_sample == 5
            # 5. Выборка набрана -- вердикт, и сразу новая рекомендация возможна
            r = tick(s, p.id, _pp(registrations=34, pricing_viewed=8), truepost_playbook)
            assert r["finished_experiment"] is not None
            assert r["finished_experiment"].status == GrowthExperimentStatus.won

    def test_notify_wrapper_dedups(self):
        from app.scheduler import growth_loop_tick_and_notify

        f = _factory()
        with f() as s:
            p = _project(s)
            project_id = p.id

        sent = []

        async def fake_send(settings, text):
            sent.append(text)
            return True

        class S:
            bot_token = "x"
            admin_chat_ids_list = ["1"]

        class FakeProject:
            id = project_id

        r1 = asyncio.run(growth_loop_tick_and_notify(
            FakeProject(), S(), _pp(), _send=fake_send, _session_factory=f))
        assert r1["proposal_sent"] is True
        assert any("НОВОЕ ПРЕДЛОЖЕНИЕ" in t for t in sent)
        # Повторный тик: рекомендация уже proposed -- ничего не шлём
        r2 = asyncio.run(growth_loop_tick_and_notify(
            FakeProject(), S(), _pp(), _send=fake_send, _session_factory=f))
        assert r2["proposal_sent"] is False
        assert len(sent) == 1


# ---------------------------------------------------------------------------
# Доска: блоки состояний
# ---------------------------------------------------------------------------

class TestBoardBlocks:

    def test_recommendation_block(self):
        from app.commercial_report import build_recommendation_block
        f = _factory()
        with f() as s:
            p = _project(s)
            rec = propose_if_needed(s, p.id, _pp(), truepost_playbook)
            text = build_recommendation_block(rec)
            assert "ПРЕДЛАГАЮ ЭКСПЕРИМЕНТ" in text
            assert "Действие:" in text and "Почему:" in text
            assert "Не менять:" in text
            assert "confidence interval" not in text.lower()

    def test_experiment_block(self):
        from app.commercial_report import build_experiment_block
        f = _factory()
        with f() as s:
            p = _project(s)
            exp = _make_experiment(s, p, baseline=_pp(registrations=20, pricing_viewed=2))
            prog = experiment_progress(exp, _pp(registrations=28, pricing_viewed=5))
            text = build_experiment_block(exp, prog)
            assert "ЭКСПЕРИМЕНТ ИДЁТ" in text
            assert "8 / 14" in text
            assert "10%" in text and "38%" in text  # 3/8

    def test_verdict_block(self):
        from app.commercial_report import build_verdict_block
        f = _factory()
        with f() as s:
            p = _project(s)
            exp = _make_experiment(s, p, baseline=_pp(), target_sample=5, min_runtime_days=0)
            maybe_finish_experiment(s, exp, _pp(registrations=26, pricing_viewed=6))
            text = build_verdict_block(exp)
            assert "ВЕРДИКТ" in text
            assert exp.verdict in text

    def test_details_and_why(self):
        from app.commercial_report import build_recommendation_details, build_recommendation_why
        f = _factory()
        with f() as s:
            p = _project(s)
            rec = propose_if_needed(s, p.id, _pp(), truepost_playbook)
            details = build_recommendation_details(rec)
            assert "Change set:" in details and "Успех:" in details and "Провал:" in details
            why = build_recommendation_why(rec)
            assert "Почему" in why
            assert any(line.startswith("—") for line in why.splitlines())


# ---------------------------------------------------------------------------
# Виртуальная метрика first_post_feedback_total (фикс бага «0% → 300%»)
# ---------------------------------------------------------------------------

class TestVirtualFeedbackMetric:

    def test_get_metric_sums_feedback(self):
        from app.growth_loop import get_metric
        pp = _pp(first_post_feedback_good=3, first_post_feedback_bad=9)
        assert get_metric(pp, "first_post_feedback_total") == 12
        assert get_metric(pp, "registrations") == 20
        assert get_metric(None, "first_post_feedback_total") == 0

    def test_first_post_experiment_rate_cannot_exceed_100(self):
        """Сценарий бага с прода: 1 новая регистрация, 3 новых good-отзыва
        давали rate 300%. С выборкой в отзывах rate ≤ 100%."""
        f = _factory()
        with f() as s:
            p = _project(s)
            content = truepost_playbook("first_post", _pp(first_post_feedback_good=3, first_post_feedback_bad=9), DEFAULT_THRESHOLDS)
            assert content["sample_metric"] == "first_post_feedback_total"
            rec = GrowthRecommendation(
                project_id=p.id, area="first_post", title=content["title"],
                action=content["action"], primary_metric=content["primary_metric"],
                sample_metric=content["sample_metric"], target_sample=content["target_sample"],
                status=GrowthRecommendationStatus.proposed,
            )
            s.add(rec); s.commit(); s.refresh(rec)
            baseline = _pp(registrations=16, first_post_feedback_good=3, first_post_feedback_bad=9)
            exp = accept_recommendation(s, rec, baseline)
            # +1 регистрация, +3 good, +1 bad
            now = _pp(registrations=17, first_post_feedback_good=6, first_post_feedback_bad=10)
            prog = experiment_progress(exp, now)
            assert prog["current_sample"] == 4      # новых отзывов
            assert prog["delta_metric"] == 3        # из них good
            assert prog["current_rate"] == pytest.approx(0.75)
            assert prog["current_rate"] <= 1.0
            assert prog["baseline_rate"] == pytest.approx(3 / 12)

    def test_display_clamped(self):
        from app.commercial_report import _fmt_rate
        assert _fmt_rate(3.0) == "100%"
        assert _fmt_rate(0.42) == "42%"
        assert _fmt_rate(None) == "—"
