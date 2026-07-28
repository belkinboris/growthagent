"""
Сравнение недели с предыдущей (задача D2).

Одно число «за 7 дней» не отвечает на вопрос, ради которого владелец
открывает экран: стало лучше или хуже. Раньше это можно было понять
только по графику динамики глазами, а глаз на четырёх точках ошибается.

Главное, что здесь проверяется, — честность: на маленьких числах разница
случайна, и показывать «рост вдвое» как факт нельзя.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import MetricSnapshot
from tests.test_platform_api import _client, _login


def _snapshot(session, project_id, created, product):
    session.add(MetricSnapshot(
        project_id=project_id, period_key="7d", source="combined",
        period_start=created - timedelta(days=7), period_end=created,
        as_of=created, created_at=created, metrics_json={"product": product},
    ))
    session.commit()


def _two_weeks(session_factory, now_values, was_values, gap_days=9):
    now = datetime.now(timezone.utc)
    with session_factory() as session:
        _snapshot(session, 1, now - timedelta(days=gap_days), was_values)
        _snapshot(session, 1, now, now_values)


BIG_NOW = {"signup": 52, "activation_1": 25, "activation_2": 91,
           "payment_started": 6, "payment_success": 2}
BIG_WAS = {"signup": 40, "activation_1": 25, "activation_2": 60,
           "payment_started": 4, "payment_success": 1}


def _rows(client):
    return {r["key"]: r for r in client.get("/growth/api/compare").json()["rows"]}


class TestComparison:
    def test_growth_is_named(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _two_weeks(factory, BIG_NOW, BIG_WAS)

        row = _rows(client)["signup"]
        assert (row["was"], row["now"], row["delta"]) == (40, 52, 12)
        assert row["percent"] == 30
        assert row["verdict"] == "Стало больше."
        assert row["reliable"] is True

    def test_drop_is_named(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _two_weeks(factory, {**BIG_NOW, "signup": 20}, BIG_WAS)

        row = _rows(client)["signup"]
        assert row["delta"] == -20 and row["percent"] == -50
        assert row["verdict"] == "Стало меньше."

    def test_no_change_is_named(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _two_weeks(factory, BIG_NOW, BIG_WAS)
        assert _rows(client)["activation_1"]["verdict"] == "Без изменений."

    def test_titles_follow_project_names(self, monkeypatch, tmp_path):
        """Владелец переименовал шаг -- сравнение зовёт его так же, иначе
        два экрана говорят о разном разными словами."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _two_weeks(factory, BIG_NOW, BIG_WAS)
        client.put("/growth/api/projects/1/stages",
                   json={"titles": {"activation_1": "Создал канал"}})

        assert _rows(client)["activation_1"]["title"] == "Создал канал"


class TestHonestyAboutSmallNumbers:
    def test_two_events_are_not_a_trend(self, monkeypatch, tmp_path):
        """1 → 2 -- это «плюс 100%», и показывать так нельзя."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _two_weeks(factory, BIG_NOW, BIG_WAS)

        row = _rows(client)["payment_success"]
        assert row["reliable"] is False
        assert "слишком мало" in row["verdict"]

    def test_missing_side_is_not_compared(self, monkeypatch, tmp_path):
        """Данных за одну из недель нет -- это не «падение до нуля»."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _two_weeks(factory, BIG_NOW, {k: v for k, v in BIG_WAS.items() if k != "signup"})

        row = _rows(client)["signup"]
        assert row["reliable"] is False
        assert "Не с чем сравнить" in row["verdict"]

    def test_percent_is_absent_when_previous_is_zero(self, monkeypatch, tmp_path):
        """Деление на ноль -- не «бесконечный рост», а отсутствие процента."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _two_weeks(factory, BIG_NOW, {**BIG_WAS, "signup": 0})

        row = _rows(client)["signup"]
        assert row["percent"] is None
        assert row["verdict"] == "Стало больше."


class TestEmptyStates:
    def test_no_snapshots_says_so(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        body = client.get("/growth/api/compare").json()
        assert body["ok"] is False
        assert "Снимков ещё нет" in body["hint"]

    def test_less_than_two_weeks_is_explained(self, monkeypatch, tmp_path):
        """Наблюдаем неделю -- сравнивать не с чем, и это надо сказать,
        а не показать нули за прошлую неделю."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        now = datetime.now(timezone.utc)
        with factory() as session:
            _snapshot(session, 1, now - timedelta(days=2), BIG_WAS)
            _snapshot(session, 1, now, BIG_NOW)

        body = client.get("/growth/api/compare").json()
        assert body["ok"] is False
        assert "меньше двух недель" in body["hint"]

    def test_requires_auth(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        assert client.get("/growth/api/compare").status_code == 401
