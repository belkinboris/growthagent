"""
Что изменилось после выкатки (задача D6).

Сравнение недель отвечает «стало лучше или хуже», но не отвечает на вопрос,
который владелец задаёт на самом деле: помогло ли то, что я сделал.
Календарь не знает, когда была выкатка, и изменение, случившееся в среду,
размазывается по двум неделям сразу.

Главное, что здесь проверяется: неделя «после» не начинается раньше самого
изменения (иначе в окне смешаны старая и новая версия), и экран сам говорит,
что совпадение во времени — не доказательство.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import MetricSnapshot
from tests.test_platform_api import _client, _login, _second_project, _register

BEFORE = {"signup": 40, "activation_1": 20, "payment_success": 4}
AFTER = {"signup": 60, "activation_1": 40, "payment_success": 9}


def _snapshot(session, created, product, project_id=1):
    session.add(MetricSnapshot(
        project_id=project_id, period_key="7d", source="combined",
        period_start=created - timedelta(days=7), period_end=created,
        as_of=created, created_at=created, metrics_json={"product": product},
    ))
    session.commit()


def _mark(client, days_ago, title="Переписал первый экран"):
    at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    resp = client.post("/growth/api/projects/1/changes", json={"title": title, "at": at})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _effect(client, change_id):
    return client.get(f"/growth/api/changes/{change_id}/effect").json()


class TestMarkingAChange:
    def test_change_is_saved_and_listed(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        _mark(client, days_ago=10)

        changes = client.get("/growth/api/projects/1/changes").json()["changes"]
        assert [c["title"] for c in changes] == ["Переписал первый экран"]

    def test_change_without_time_is_marked_now(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        client.post("/growth/api/projects/1/changes", json={"title": "Поднял цену"})

        at = client.get("/growth/api/projects/1/changes").json()["changes"][0]["at"]
        assert (datetime.now(timezone.utc)
                - datetime.fromisoformat(at)).total_seconds() < 60

    def test_empty_title_is_refused(self, monkeypatch, tmp_path):
        """Отметка «что-то поменял» через месяц не значит ничего."""
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        assert client.post("/growth/api/projects/1/changes",
                           json={"title": "   "}).status_code == 400

    def test_future_date_is_refused(self, monkeypatch, tmp_path):
        """Дата в будущем -- опечатка: сравнение по ней потом молча покажет
        пустоту вместо ошибки."""
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        at = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        assert client.post("/growth/api/projects/1/changes",
                           json={"title": "Выкачу завтра", "at": at}).status_code == 400

    def test_marking_is_written_to_the_owner_journal(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        _mark(client, days_ago=1)

        action = client.get("/growth/api/actions").json()["actions"][0]
        assert action["action"] == "change_marked"
        assert "Переписал первый экран" in action["summary"]


class TestEffect:
    def test_before_and_after_are_compared(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        now = datetime.now(timezone.utc)
        with factory() as session:
            _snapshot(session, now - timedelta(days=21), BEFORE)
            _snapshot(session, now, AFTER)
        change_id = _mark(client, days_ago=20)

        body = _effect(client, change_id)
        assert body["ok"] is True
        rows = {r["key"]: r for r in body["rows"]}
        assert (rows["signup"]["was"], rows["signup"]["now"]) == (40, 60)
        assert rows["signup"]["verdict"] == "Стало больше."

    def test_week_after_must_start_after_the_change(self, monkeypatch, tmp_path):
        """Снимок, чьё окно захватывает день выкатки, смешивает старую и
        новую версию: разница в нём показывает долю дней, а не изменение."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        now = datetime.now(timezone.utc)
        with factory() as session:
            _snapshot(session, now - timedelta(days=10), BEFORE)
            _snapshot(session, now, AFTER)  # окно началось до выкатки
        change_id = _mark(client, days_ago=4)

        body = _effect(client, change_id)
        assert body["ok"] is False
        assert "Чистой недели после изменения ещё не набралось" in body["hint"]

    def test_no_snapshot_before_is_explained(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        now = datetime.now(timezone.utc)
        with factory() as session:
            _snapshot(session, now, AFTER)
        change_id = _mark(client, days_ago=30)

        body = _effect(client, change_id)
        assert body["ok"] is False
        assert "начал наблюдать позже" in body["hint"]

    def test_small_numbers_are_still_not_a_trend(self, monkeypatch, tmp_path):
        """Порог тот же, что в сравнении недель: продукт говорит об
        уверенности одинаково во всех местах."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        now = datetime.now(timezone.utc)
        with factory() as session:
            _snapshot(session, now - timedelta(days=21), {"signup": 40, "payment_success": 1})
            _snapshot(session, now, {"signup": 60, "payment_success": 2})
        change_id = _mark(client, days_ago=20)

        row = {r["key"]: r for r in _effect(client, change_id)["rows"]}["payment_success"]
        assert row["reliable"] is False
        assert "слишком мало" in row["verdict"]

    def test_correlation_is_not_called_proof(self, monkeypatch, tmp_path):
        """Владелец припишет изменению чужой результат, если экран об этом
        не скажет сам."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        now = datetime.now(timezone.utc)
        with factory() as session:
            _snapshot(session, now - timedelta(days=21), BEFORE)
            _snapshot(session, now, AFTER)
        change_id = _mark(client, days_ago=20)

        caution = _effect(client, change_id)["caution"]
        assert "не доказательство" in caution
        assert "эксперимент" in caution

    def test_step_names_follow_project_names(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        now = datetime.now(timezone.utc)
        with factory() as session:
            _snapshot(session, now - timedelta(days=21), BEFORE)
            _snapshot(session, now, AFTER)
        change_id = _mark(client, days_ago=20)
        client.put("/growth/api/projects/1/stages",
                   json={"titles": {"activation_1": "Создал канал"}})

        rows = {r["key"]: r for r in _effect(client, change_id)["rows"]}
        assert rows["activation_1"]["title"] == "Создал канал"


class TestIsolation:
    def test_foreign_change_is_not_visible(self, monkeypatch, tmp_path):
        """Отметки изменений -- данные проекта, делятся по тем же правилам."""
        from sqlmodel import select

        from app import accounts
        from app.models import PlatformUser

        client, factory = _client(monkeypatch, tmp_path)
        _register(client, "ivan@example.com")  # усыновляет проект из окружения
        change_id = _mark(client, days_ago=10)

        petr_pid = _second_project(factory)
        client.post("/growth/api/logout")
        _register(client, "petr@example.com")
        with factory() as session:
            petr = session.exec(
                select(PlatformUser).where(PlatformUser.email == "petr@example.com")).first()
            accounts.grant_project(session, petr_pid, petr.id)

        assert client.get(f"/growth/api/changes/{change_id}/effect").status_code == 404
        assert client.post("/growth/api/projects/1/changes",
                           json={"title": "чужое"}).status_code == 404

    def test_requires_auth(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        assert client.get("/growth/api/projects/1/changes").status_code == 401
        assert client.post("/growth/api/projects/1/changes",
                           json={"title": "x"}).status_code == 401
