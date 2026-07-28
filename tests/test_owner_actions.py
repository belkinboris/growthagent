"""
Журнал действий владельца (задача C5).

«История» показывала, что предлагал аналитик и чем кончились проверки, но
не показывала действий человека: кто включил сбор, кто переименовал шаги,
кто отклонил предложение. Через неделю уже не вспомнить, почему числа
изменились, — а с аккаунтами появился и второй вопрос: кто это сделал.

Проверяется: действие записывается, подпись честная, чужой журнал не
виден, и — важное — сбой записи не ломает само действие.
"""

import pytest

from tests.test_platform_api import _client, _login, _second_project, _register


def _actions(client):
    return client.get("/growth/api/actions").json()["actions"]


class TestActionsAreRecorded:
    def test_collection_switch_is_logged(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        client.post("/growth/api/projects/1/pause")
        client.post("/growth/api/projects/1/activate")

        actions = _actions(client)
        assert [a["action"] for a in actions] == ["collection_on", "collection_off"]
        assert "Включил сбор" in actions[0]["summary"]

    def test_settings_change_names_what_changed(self, monkeypatch, tmp_path):
        """«Изменил настройки» через неделю ничего не объясняет -- нужно
        знать, что именно поменяли."""
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        client.patch("/growth/api/projects/1",
                     json={"notify_chat_ids": ["1"], "metrika_counter_id": "42"})

        summary = _actions(client)[0]["summary"]
        assert "адресатов уведомлений" in summary
        assert "счётчик Метрики" in summary
        assert "токен" not in summary, "не меняли токен -- не должно быть в записи"

    def test_renaming_is_logged(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        client.put("/growth/api/projects/1/stages",
                   json={"titles": {"activation_1": "Создал канал"}})
        assert _actions(client)[0]["action"] == "names_changed"

    def test_alert_decision_is_logged(self, monkeypatch, tmp_path):
        from app.models import (Alert, AlertCategory, AlertSeverity, AlertStatus,
                                ConfidenceLevel)

        client, session_factory = _client(monkeypatch, tmp_path)
        _login(client)
        with session_factory() as session:
            alert = Alert(project_id=1, fingerprint="f1", title="Оплаты встали",
                          message="0 из 5", severity=AlertSeverity.p1,
                          category=AlertCategory.payments_started_no_success,
                          confidence=ConfidenceLevel.medium, status=AlertStatus.open)
            session.add(alert)
            session.commit()
            session.refresh(alert)
            alert_id = alert.id

        client.post(f"/growth/api/alerts/{alert_id}/ack")
        record = _actions(client)[0]
        assert record["action"] == "alert_acknowledged"
        assert "Оплаты встали" in record["summary"]

    def test_nothing_logged_when_nothing_changed(self, monkeypatch, tmp_path):
        """Пустой PATCH -- не действие. Иначе журнал забьётся шумом."""
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        client.patch("/growth/api/projects/1", json={})
        assert _actions(client) == []


class TestActor:
    def test_env_owner_is_signed_honestly(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        client.post("/growth/api/projects/1/pause")
        assert _actions(client)[0]["actor"] == "владелец платформы"

    def test_account_is_signed_by_email(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _register(client, "ivan@example.com")  # усыновляет проект из окружения
        client.post("/growth/api/projects/1/pause")
        assert _actions(client)[0]["actor"] == "ivan@example.com"


class TestIsolation:
    def test_foreign_actions_are_not_visible(self, monkeypatch, tmp_path):
        """Журнал -- это данные проекта, и делятся они по тем же правилам."""
        from app import accounts
        from sqlmodel import select
        from app.models import PlatformUser

        client, session_factory = _client(monkeypatch, tmp_path)
        _register(client, "ivan@example.com")
        client.post("/growth/api/projects/1/pause")

        petr_pid = _second_project(session_factory)
        client.post("/growth/api/logout")
        _register(client, "petr@example.com")
        with session_factory() as session:
            petr = session.exec(
                select(PlatformUser).where(PlatformUser.email == "petr@example.com")).first()
            accounts.grant_project(session, petr_pid, petr.id)

        assert _actions(client) == [], "видны чужие действия"


class TestJournalNeverBreaksTheAction:
    def test_failed_write_does_not_fail_the_request(self, monkeypatch, tmp_path):
        """Журнал -- вспомогательная вещь. Если запись не удалась, человек
        всё равно сделал то, что хотел; отдать ему ошибку вместо результата
        было бы хуже, чем остаться без строчки в истории."""
        import app.models as models
        from app.models import Project
        from sqlmodel import select

        client, session_factory = _client(monkeypatch, tmp_path)
        _login(client)

        def explode(self, **kwargs):
            raise RuntimeError("база отказала на записи журнала")

        monkeypatch.setattr(models.OwnerAction, "__init__", explode)

        resp = client.post("/growth/api/projects/1/pause")
        assert resp.status_code == 200, "действие упало из-за журнала"
        with session_factory() as session:
            project = session.exec(select(Project)).first()
            assert project.is_active is False, "сбор не выключился, хотя ответ 200"
