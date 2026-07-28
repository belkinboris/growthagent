"""
Когда выводам можно будет верить (задача D4).

«Событий слишком мало» аналитик писал в трёх местах, а когда станет
достаточно — не писал нигде. Ожидание без срока выглядит бесконечным:
человек либо перестаёт верить экрану, либо принимает решение по числу,
про которое ему честно сказали «не верьте».

Главное, что здесь проверяется, — честность ответа. Окно наблюдения всегда
семь дней, и если за неделю набирается меньше нужного, ждать бесполезно:
через месяц в окне будет ровно такая же неделя. Обещать «скоро будет
видно» в этом случае — врать.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import MetricSnapshot
from app.readiness import (
    CHECKS,
    ENOUGH_FOR_A_CONCLUSION,
    MIN_FOR_A_TREND,
    assess,
)
from tests.test_platform_api import _client, _login

PRODUCT = next(c for c in CHECKS if c.key == "product")            # нужно 10 регистраций
WEEK_OVER_WEEK = next(c for c in CHECKS if c.key == "week_over_week")  # нужны две недели


class TestAssess:
    def test_enough_data_is_named(self):
        row = assess(PRODUCT, weekly_value=42, observed_days=30)
        assert row["ready"] is True
        assert "Данных достаточно" in row["verdict"]

    def test_waiting_does_not_help_when_the_week_is_too_thin(self):
        """Окно всегда семь дней: через месяц в нём будет такая же неделя.
        Это и есть честный ответ на «когда будет понятно»."""
        row = assess(PRODUCT, weekly_value=4, observed_days=30)
        assert row["ready"] is False
        assert "Ждать дольше не поможет" in row["verdict"]
        assert "в 3 раза больше" in row["verdict"]

    def test_multiplier_is_written_in_russian(self):
        """«В 5 раза» -- по таким мелочам текст читается машинным."""
        assert "в 5 раз больше" in assess(PRODUCT, weekly_value=2, observed_days=30)["verdict"]
        assert "в 4 раза больше" in assess(PRODUCT, weekly_value=3, observed_days=30)["verdict"]

    def test_partial_window_is_not_measured_against_the_threshold(self):
        """Проект подключили вчера: в недельном окне лежит день, и мерить
        его недельным порогом -- обман."""
        row = assess(PRODUCT, weekly_value=5, observed_days=2)
        assert row["ready"] is False
        assert "Наблюдаем 2 дня из 7" in row["verdict"]
        # 5 за два дня -- это ~18 за неделю, то есть темпа хватает.
        assert "хватит" in row["verdict"]

    def test_partial_window_with_weak_pace_says_so(self):
        """Ободрять «осталось подождать», когда темпа не хватает, нельзя."""
        row = assess(PRODUCT, weekly_value=1, observed_days=3)
        assert "наберётся около 2 из нужных 10" in row["verdict"]

    def test_second_week_is_required_for_comparison(self):
        """Сравнивать неделю с неделей на седьмой день не с чем."""
        row = assess(WEEK_OVER_WEEK, weekly_value=50, observed_days=8)
        assert row["ready"] is False
        assert "из 14" in row["verdict"]

    def test_zero_events_give_no_estimate(self):
        """Ноль в неделю -- не «подождите ещё немного», срок не из чего
        посчитать."""
        row = assess(PRODUCT, weekly_value=0, observed_days=30)
        assert "срок оценить не по чему" in row["verdict"]

    def test_missing_metric_is_not_a_zero(self):
        row = assess(PRODUCT, weekly_value=None, observed_days=30)
        assert row["ready"] is False
        assert "ещё не собрал" in row["verdict"]

    def test_every_check_explains_why_that_number(self):
        """Порог без объяснения -- просьба верить на слово."""
        for check in CHECKS:
            assert check.why and check.why[0].isupper()


class TestThresholdsAreShared:
    def test_comparison_uses_the_common_threshold(self):
        """Разные пороги на разных экранах означали бы, что про одни и те же
        события платформа говорит разное."""
        from app.platform_api import COMPARE_MIN_SAMPLE, SOURCE_MIN_SAMPLE

        assert COMPARE_MIN_SAMPLE == MIN_FOR_A_TREND
        assert SOURCE_MIN_SAMPLE == ENOUGH_FOR_A_CONCLUSION

    def test_confidence_uses_the_common_threshold(self):
        from app.confidence import _CONVERSION_THRESHOLDS

        assert _CONVERSION_THRESHOLDS["low"] == MIN_FOR_A_TREND
        assert _CONVERSION_THRESHOLDS["medium"] == ENOUGH_FOR_A_CONCLUSION


def _snapshot(session, created, product):
    session.add(MetricSnapshot(
        project_id=1, period_key="7d", source="combined",
        period_start=created - timedelta(days=7), period_end=created,
        as_of=created, created_at=created, metrics_json={"product": product},
    ))
    session.commit()


class TestEndpoint:
    def test_answers_every_question(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        now = datetime.now(timezone.utc)
        with factory() as session:
            _snapshot(session, now - timedelta(days=20), {"signup": 30})
            _snapshot(session, now, {"signup": 30, "payment_started": 1})

        body = client.get("/growth/api/readiness").json()
        assert body["ok"] is True
        rows = {c["key"]: c for c in body["checks"]}
        assert rows["product"]["ready"] is True
        assert rows["week_over_week"]["ready"] is True
        # Одной попытки оплаты мало -- и это сказано, а не скрыто.
        assert rows["payments"]["ready"] is False
        assert body["observed_days"] == 20.0

    def test_step_names_follow_project_names(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        with factory() as session:
            _snapshot(session, datetime.now(timezone.utc), {"signup": 30})
        client.put("/growth/api/projects/1/stages",
                   json={"titles": {"signup": "Завёл аккаунт"}})

        rows = {c["key"]: c for c in client.get("/growth/api/readiness").json()["checks"]}
        assert rows["product"]["metric_title"] == "Завёл аккаунт"

    def test_no_snapshots_is_explained(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)

        body = client.get("/growth/api/readiness").json()
        assert body["ok"] is False and body["checks"] == []
        assert "первого цикла" in body["hint"]

    def test_fresh_project_is_not_judged_by_a_full_week(self, monkeypatch, tmp_path):
        """Один снимок -- наблюдаем ноль дней, и все пороги ещё впереди."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        with factory() as session:
            _snapshot(session, datetime.now(timezone.utc), {"signup": 40})

        rows = {c["key"]: c for c in client.get("/growth/api/readiness").json()["checks"]}
        assert rows["product"]["ready"] is False
        assert "Наблюдаем" in rows["product"]["verdict"]

    def test_requires_auth(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        assert client.get("/growth/api/readiness").status_code == 401
