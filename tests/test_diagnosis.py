"""
Тесты разбора воронки (задача R7).

Главное, что здесь проверяется, -- умение отличить ВИДИМЫЙ обрыв от
ПРИЧИНЫ. Это ровно тот разбор, который владелец сделал руками в чате по
АвтоПосту, когда платформа не смогла: формально люди отваливаются на
тарифном экране, а на деле им не нравится первый пост, и красить кнопку
бесполезно.

Второе -- честность: на пустых и на крошечных данных разбор обязан
отказаться от вывода, а не сочинить его.
"""
import pytest

from app.diagnosis import diagnose


# Реальные числа владельца за неделю (АвтоПост, август 2026).
OWNER_CASE = {
    "registrations": 66, "channels_created": 58, "post_generations": 52,
    "pricing_viewed": 80, "payment_cta_clicked": 10,
    "payment_started": 6, "payment_success": 3,
    "first_post_feedback_good": 11, "first_post_feedback_bad": 26,
    "first_post_feedback_reasons": {"wrong_style": 14, "too_generic": 8, "other": 4},
}


class TestOwnerCase:
    """Разбор обязан повторить то, что владелец вывел сам."""

    def test_names_the_root_cause_not_the_visible_break(self):
        d = diagnose(OWNER_CASE)
        assert d.ok
        # Вывод -- про первый пост, а не про тарифный экран.
        assert "первый пост" in d.headline.lower()
        assert d.root_cause and "не тот стиль" in d.root_cause

    def test_visible_break_is_still_named(self):
        """Обрыв не замалчиваем: он реален, просто это следствие."""
        d = diagnose(OWNER_CASE)
        assert d.visible_break == "нажали «выбрать тариф»"
        assert any("80" in e and "10" in e for e in d.evidence)

    def test_action_forbids_fixing_the_symptom(self):
        d = diagnose(OWNER_CASE)
        assert "первого поста" in d.action
        assert "не трогать" in d.action, "иначе владелец пойдёт красить кнопку"

    def test_evidence_has_the_feedback_numbers(self):
        d = diagnose(OWNER_CASE)
        assert any("26" in e and "11" in e and "70%" in e for e in d.evidence)

    def test_chain_marks_the_worst_step(self):
        d = diagnose(OWNER_CASE)
        worst = [s for s in d.chain if s.is_worst]
        assert len(worst) == 1
        assert worst[0].key == "payment_cta_clicked"

    def test_full_chain_is_reported(self):
        d = diagnose(OWNER_CASE)
        assert [s.key for s in d.chain] == [
            "registrations", "channels_created", "post_generations",
            "pricing_viewed", "payment_cta_clicked", "payment_started", "payment_success",
        ]


class TestGenericReasonIsNotAnInsight:
    """
    Баг, найденный владельцем на живом продукте: если самая частая причина
    в отзывах — «другое» (человек поставил «плохо», не написав, что не
    так), разбор писал «главная причина — другое». Это звучит как вывод,
    хотя означает «мы не знаем» — владелец не понимает, что делать с такой
    формулировкой.
    """

    def test_generic_reason_is_not_named_as_the_cause(self):
        case = dict(OWNER_CASE, first_post_feedback_reasons={"other": 20, "wrong_style": 3})
        d = diagnose(case)
        assert "другое" not in d.headline.lower()
        assert "другое" not in (d.root_cause or "").lower()
        assert "другое" not in (d.action or "").lower()

    def test_specific_reason_wins_even_if_fewer_votes_than_other(self):
        """«Другое» не должно перетягивать вывод на себя, даже если голосов
        за него больше: конкретная причина полезнее для действия."""
        case = dict(OWNER_CASE, first_post_feedback_reasons={"other": 20, "wrong_style": 3})
        d = diagnose(case)
        assert "не тот стиль" in d.root_cause

    def test_all_generic_is_stated_honestly(self):
        """Если ни одной конкретной причины нет вообще -- так и говорим,
        а не выдумываем причину и не называем пустоту выводом."""
        case = dict(OWNER_CASE, first_post_feedback_reasons={"other": 20})
        d = diagnose(case)
        assert "другое" not in d.headline.lower()
        assert any("не написав" in e or "не уточнили" in e for e in d.evidence)
        assert "прочитать" in d.action.lower() or "отзыв" in d.action.lower()


class TestWithoutQualitySignal:
    """Без отзывов корень назвать нельзя -- значит, и не называем."""

    def test_break_is_the_conclusion_when_feedback_is_good(self):
        case = dict(OWNER_CASE, first_post_feedback_good=30, first_post_feedback_bad=4)
        d = diagnose(case)
        assert "первый пост" not in d.headline.lower()
        assert d.root_cause is None
        assert "выбрать тариф" in d.headline

    def test_tiny_feedback_sample_does_not_become_a_cause(self):
        """Два «плохо» против одного «хорошо» -- это три человека, не 67%."""
        case = dict(OWNER_CASE, first_post_feedback_good=1, first_post_feedback_bad=2,
                    first_post_feedback_reasons={"wrong_style": 2})
        d = diagnose(case)
        assert d.root_cause is None
        assert any("мало" in e for e in d.evidence)

    def test_no_feedback_keys_at_all(self):
        case = {k: v for k, v in OWNER_CASE.items() if "feedback" not in k}
        d = diagnose(case)
        assert d.ok and d.root_cause is None


class TestHonestRefusal:
    def test_no_data_at_all(self):
        d = diagnose(None)
        assert not d.ok and "нет данных" in d.hint

    def test_empty_dict(self):
        d = diagnose({})
        assert not d.ok

    def test_too_few_steps_to_connect(self):
        d = diagnose({"registrations": 40})
        assert not d.ok and "мало шагов" in d.hint

    def test_tiny_funnel_refuses_a_verdict(self):
        """На двух людях любой процент случаен."""
        d = diagnose({"registrations": 2, "payment_success": 1})
        assert not d.ok and "случайным" in d.hint

    def test_small_but_workable_sample_is_marked_as_direction(self):
        case = {"registrations": 6, "channels_created": 5, "post_generations": 4,
                "pricing_viewed": 4, "payment_cta_clicked": 1, "payment_success": 1}
        d = diagnose(case)
        assert d.ok
        assert "не доказанный факт" in d.confidence_note


class TestChainArithmetic:
    def test_missing_step_is_skipped_not_zeroed(self):
        """Продукт не отдал шаг -- его нет в цепочке. Ноль означал бы
        «этот шаг не прошёл никто», а это другое утверждение."""
        case = dict(OWNER_CASE)
        del case["pricing_viewed"]
        d = diagnose(case)
        assert "pricing_viewed" not in [s.key for s in d.chain]

    def test_healthy_funnel_has_no_break(self):
        case = {"registrations": 100, "channels_created": 90, "post_generations": 85,
                "pricing_viewed": 70, "payment_cta_clicked": 60,
                "payment_started": 55, "payment_success": 50}
        d = diagnose(case)
        assert d.ok and d.visible_break is None
        assert "нет" in d.headline.lower()

    def test_biggest_absolute_loss_wins_over_worst_percent(self):
        """Оба шага плохи по проценту, но обрыв, где потеряно больше людей,
        важнее: «180 → 80» (потеряно 100) весомее, чем «10 → 3» (потеряно 7),
        хотя во втором проценты хуже."""
        case = {"registrations": 200, "channels_created": 190, "post_generations": 180,
                "pricing_viewed": 80, "payment_cta_clicked": 60,
                "payment_started": 10, "payment_success": 3}
        d = diagnose(case)
        assert d.visible_break == "посмотрели цены"

    def test_step_above_half_is_not_called_a_break(self):
        """Худший из рабочих шагов -- всё ещё рабочий шаг."""
        case = {"registrations": 100, "channels_created": 88, "post_generations": 80,
                "pricing_viewed": 70, "payment_cta_clicked": 62,
                "payment_started": 55, "payment_success": 51}
        d = diagnose(case)
        assert d.visible_break is None


class TestDashboardIntegration:
    def test_dashboard_returns_diagnosis(self, monkeypatch, tmp_path):
        from tests.test_platform_api import _client, _login, _project_id

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        from app.service import PAYMENT_PATH_CACHE_PERIOD_KEY, save_diagnostics_cache

        with session_factory() as session:
            save_diagnostics_cache(session, pid, PAYMENT_PATH_CACHE_PERIOD_KEY,
                                    "test", OWNER_CASE, ok=True)
        _login(client)
        d = client.get("/growth/api/dashboard").json()["diagnosis"]
        assert d["ok"] is True
        assert "первый пост" in d["headline"].lower()
        assert d["action"]
        assert len(d["chain"]) == 7

    def test_dashboard_diagnosis_is_honest_without_data(self, monkeypatch, tmp_path):
        from tests.test_platform_api import _client, _login

        client, session_factory = _client(monkeypatch, tmp_path)
        _login(client)
        d = client.get("/growth/api/dashboard").json()["diagnosis"]
        assert d["ok"] is False
        assert d["hint"]


class TestAreaAndMetricTitlesAreRussian:
    """
    Владелец видел сырые коды area/sample_metric на экранах истории решений
    и идущей проверки («first_post», «(first_post_feedback_total)») --
    /api/growth и /api/history обязаны отдавать готовые русские подписи,
    а не только код для внутренних расчётов.
    """

    def test_recommendation_has_area_title(self, monkeypatch, tmp_path):
        from tests.test_platform_api import _client, _login, _project_id

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        from app import growth_loop
        from app.models import GrowthRecommendation

        with session_factory() as session:
            session.add(GrowthRecommendation(
                project_id=pid, area="first_post", title="Чиним качество первого поста",
                action="...", hypothesis="...", confidence="сигнал",
                primary_metric="first_post_feedback_good",
                sample_metric="first_post_feedback_total", target_sample=10,
                fingerprint=f"{pid}/first_post/x",
            ))
            session.commit()
        _login(client)
        g = client.get("/growth/api/growth").json()
        assert g["recommendation"]["area_title"] == "первый пост"
        assert "first_post" not in g["recommendation"]["area_title"]

    def test_running_experiment_has_readable_sample_metric(self, monkeypatch, tmp_path):
        from tests.test_platform_api import _client, _login, _project_id
        from app.models import GrowthExperiment, GrowthRecommendation

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            rec = GrowthRecommendation(
                project_id=pid, area="first_post", title="Чиним качество первого поста",
                action="...", hypothesis="...", confidence="сигнал",
                primary_metric="first_post_feedback_good",
                sample_metric="first_post_feedback_total", target_sample=10,
                fingerprint=f"{pid}/first_post/y", status="accepted",
            )
            session.add(rec)
            session.commit()
            session.refresh(rec)
            session.add(GrowthExperiment(
                project_id=pid, recommendation_id=rec.id, area="first_post",
                title="Чиним качество первого поста",
                hypothesis="...", status="running",
                primary_metric="first_post_feedback_good",
                sample_metric="first_post_feedback_total", target_sample=10,
            ))
            session.commit()
        _login(client)
        g = client.get("/growth/api/growth").json()
        assert g["experiment"]["sample_metric_title"] == "отзывов о первом посте"
        assert g["experiment"]["area_title"] == "первый пост"
