"""
Курс продукта (задача R12): аналитик обязан замечать застой.

Жалоба владельца, с которой началась задача: «АвтоПост на месте стоит уже
месяц, PMF не найден, а аналитик не помогает и может вести в тупик».
До R12 это было правдой по построению: все правила смотрели на один срез,
и доска месяцами могла писать «всё в норме» стоящему продукту.

Три обязательных свойства:
1. Застой называется застоем, с разбором, ГДЕ он (спрос / деньги / всё).
2. Анти-тупик: проверки воронки шли, а курс не сдвинулся — сказано прямо,
   что полировка воронки сейчас не поможет.
3. Не раздражать: пока продукт растёт или данных мало — никаких советов.
"""
import pytest

from app.course import MIN_WEEKS_FOR_A_COURSE, assess

STALLED_MONTH = [
    {"signup": 16, "payment_success": 0},
    {"signup": 14, "payment_success": 1},
    {"signup": 18, "payment_success": 0},
    {"signup": 17, "payment_success": 1},
]


class TestStalledProduct:
    def test_month_of_flat_numbers_is_called_stalled(self):
        c = assess(STALLED_MONTH)
        assert c.ok and c.stalled

    def test_registrations_without_money_is_the_named_diagnosis(self):
        """Самый частый случай перед PMF: люди приходят, деньги — нет."""
        c = assess(STALLED_MONTH)
        assert "деньги" in c.headline.lower()
        assert "не заплатил" in c.action or "заплатили" in c.action

    def test_action_does_not_send_the_founder_to_interview_people(self):
        """
        Возражение владельца (R14): «сходите и поговорите с пятью людьми» --
        это ручной труд фаундера, ровно то, чего платформа должна его
        избавить. Спрашивает продукт, а не человек с блокнотом.
        """
        c = assess(STALLED_MONTH)
        low = c.action.lower()
        assert "сам продукт" in low or "задаст" in low
        assert "поговорите" not in low and "разговоры" not in low

    def test_weekly_numbers_are_shown_as_series(self):
        """«16 → 14 → 18 → 17» убедительнее слова «стоит»."""
        c = assess(STALLED_MONTH)
        assert any("→" in e for e in c.evidence)

    def test_no_demand_is_not_called_a_conversion_problem(self):
        c = assess([{"signup": 2, "payment_success": 0}] * 4)
        assert c.stalled
        assert "новых людей почти нет" in c.headline
        assert any("не «плохая конверсия»" in e for e in c.evidence)
        assert "канал" in c.action

    def test_general_stall_demands_a_product_level_hypothesis(self):
        c = assess([{"signup": 30, "payment_success": 5}] * 4)
        assert c.stalled
        assert "гипотеза уровня продукта" in c.action


class TestDeadEndGuard:
    def test_experiments_without_movement_is_named_a_dead_end(self):
        """Главная защита от «аналитик ведёт в тупик»."""
        c = assess(STALLED_MONTH, finished_experiments=2)
        assert "тупик" in c.dead_end_note
        assert "2 проверки" in c.dead_end_note

    def test_no_experiments_no_dead_end_scolding(self):
        c = assess(STALLED_MONTH, finished_experiments=0)
        assert c.dead_end_note == ""

    def test_growing_product_is_never_scolded(self):
        growing = [{"signup": 10, "payment_success": 1},
                   {"signup": 14, "payment_success": 2},
                   {"signup": 19, "payment_success": 3},
                   {"signup": 25, "payment_success": 5}]
        c = assess(growing, finished_experiments=3)
        assert not c.stalled
        assert c.dead_end_note == ""


class TestHonesty:
    def test_too_few_weeks_refuses_a_verdict(self):
        c = assess([{"signup": 10, "payment_success": 1}] * (MIN_WEEKS_FOR_A_COURSE - 1))
        assert not c.ok
        assert "случайность" in c.hint

    def test_flat_within_noise_band_is_not_a_fall(self):
        """17 после 16 — это не «падение», это шум."""
        c = assess(STALLED_MONTH)
        assert c.registrations_trend == "стоит"

    def test_missing_metric_does_not_crash(self):
        c = assess([{"signup": 20}] * 4)
        assert c.ok
        assert c.payments_trend == "мало данных"

    def test_growth_advice_is_quiet(self):
        """Растущему продукту не даём указаний — только «курс верный»."""
        growing = [{"signup": 10, "payment_success": 1},
                   {"signup": 15, "payment_success": 2},
                   {"signup": 22, "payment_success": 4},
                   {"signup": 30, "payment_success": 6}]
        c = assess(growing)
        assert not c.stalled
        assert "Курс верный" in c.action


class TestDashboardIntegration:
    def _snapshot(self, session, pid, created_at, signup, payments):
        import json
        from app.models import MetricSnapshot
        snap = MetricSnapshot(
            project_id=pid, period_key="7d",
            period_start=created_at, period_end=created_at,
            source="project_metrics_api",
            metrics_json={"product": {"signup": signup, "payment_success": payments}},
        )
        snap.created_at = created_at
        session.add(snap)

    def test_dashboard_reports_the_stall(self, monkeypatch, tmp_path):
        from datetime import timedelta

        from app.models import utcnow
        from tests.test_platform_api import _client, _login, _project_id

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        now = utcnow()
        with session_factory() as session:
            for weeks_ago, (s, p) in enumerate([(17, 1), (18, 0), (14, 1), (16, 0)]):
                self._snapshot(session, pid, now - timedelta(days=7 * weeks_ago), s, p)
            session.commit()
        _login(client)

        course = client.get("/growth/api/dashboard").json()["course"]
        assert course["ok"] is True
        assert course["stalled"] is True
        assert course["weeks"] >= 4
        assert "деньги" in course["headline"].lower()

    def test_dashboard_is_honest_without_history(self, monkeypatch, tmp_path):
        from tests.test_platform_api import _client, _login

        client, session_factory = _client(monkeypatch, tmp_path)
        _login(client)
        course = client.get("/growth/api/dashboard").json()["course"]
        assert course["ok"] is False
        assert course["hint"]

    def test_one_point_per_week_not_per_snapshot(self, monkeypatch, tmp_path):
        """
        Снимки пишутся раз в несколько часов. Если считать каждый снимок
        «неделей», три дня наблюдений выглядели бы как месяцы истории.
        """
        from datetime import timedelta

        from app.models import utcnow
        from tests.test_platform_api import _client, _login, _project_id

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        now = utcnow()
        with session_factory() as session:
            for hours_ago in range(0, 72, 3):  # 24 снимка за трое суток
                self._snapshot(session, pid, now - timedelta(hours=hours_ago), 16, 1)
            session.commit()
        _login(client)

        course = client.get("/growth/api/dashboard").json()["course"]
        assert course["weeks"] <= 2, "трое суток снимков — это максимум две календарные недели"
