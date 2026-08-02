"""
Вход для машины (задача D7): продукт сам сообщает о выкатке, без человека.

Отметку о выкатке до сих пор мог поставить только владелец руками — удобно
разово, но при частых релизах он либо забывает, либо перестаёт отмечать
вовсе, и сравнение «до/после» остаётся неполным.

Главное здесь: токен даёт право ТОЛЬКО поставить отметку, не более (не тот
же самый исходящий `internal_api_token`, которым платформа сама ходит в
продукт), хранится не сам токен, а его хэш, и отметки от машины видно
отдельно от отметок владельца.
"""

import pytest

from tests.test_platform_api import _client, _login, _register, _second_project


def _rotate_token(client, project_id=1):
    resp = client.post(f"/growth/api/projects/{project_id}/inbound-token")
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


class TestTokenIssuing:
    def test_token_is_shown_once(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        body = client.post("/growth/api/projects/1/inbound-token").json()
        assert body["ok"] is True
        assert len(body["token"]) > 20
        assert "второй раз" in body["hint"]

    def test_rotating_invalidates_the_old_token(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        old = _rotate_token(client)
        new = _rotate_token(client)
        assert old != new

        old_call = client.post("/growth/api/public/projects/1/changes",
                               headers={"Authorization": f"Bearer {old}"},
                               json={"title": "старым токеном"})
        assert old_call.status_code == 401

        new_call = client.post("/growth/api/public/projects/1/changes",
                               headers={"Authorization": f"Bearer {new}"},
                               json={"title": "новым токеном"})
        assert new_call.status_code == 200

    def test_issuing_is_logged_in_owner_journal(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        _rotate_token(client)
        actions = client.get("/growth/api/actions").json()["actions"]
        assert actions[0]["action"] == "inbound_token_rotated"

    def test_requires_owner_auth(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        assert client.post("/growth/api/projects/1/inbound-token").status_code == 401

    def test_cannot_issue_token_for_a_foreign_project(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _register(client, "ivan@example.com")
        petr_pid = _second_project(factory)
        assert client.post(f"/growth/api/projects/{petr_pid}/inbound-token").status_code == 404


class TestPublicEndpoint:
    def test_valid_token_creates_a_change_marked_as_automatic(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        token = _rotate_token(client)

        resp = client.post("/growth/api/public/projects/1/changes",
                           headers={"Authorization": f"Bearer {token}"},
                           json={"title": "Выкатил v2.3"})
        assert resp.status_code == 200

        changes = client.get("/growth/api/projects/1/changes").json()["changes"]
        assert changes[0]["title"] == "Выкатил v2.3"
        assert changes[0]["by"] == "продукт (автоматически)"

    def test_wrong_token_is_refused(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        _rotate_token(client)

        resp = client.post("/growth/api/public/projects/1/changes",
                           headers={"Authorization": "Bearer wrong-token-entirely"},
                           json={"title": "чужая выкатка"})
        assert resp.status_code == 401

    def test_missing_header_is_refused(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        _rotate_token(client)
        resp = client.post("/growth/api/public/projects/1/changes", json={"title": "x"})
        assert resp.status_code == 401

    def test_no_token_issued_yet_is_refused_not_500(self, monkeypatch, tmp_path):
        """Токен ещё не выпускали -- это отказ в доступе, а не поломка."""
        client, _ = _client(monkeypatch, tmp_path)
        resp = client.post("/growth/api/public/projects/1/changes",
                           headers={"Authorization": "Bearer whatever-token"},
                           json={"title": "x"})
        assert resp.status_code == 401

    def test_empty_title_is_refused(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        token = _rotate_token(client)
        resp = client.post("/growth/api/public/projects/1/changes",
                           headers={"Authorization": f"Bearer {token}"},
                           json={"title": "  "})
        assert resp.status_code == 400

    def test_token_of_one_project_does_not_work_for_another(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _register(client, "ivan@example.com")
        token = _rotate_token(client, project_id=1)
        petr_pid = _second_project(factory)

        resp = client.post(f"/growth/api/public/projects/{petr_pid}/changes",
                           headers={"Authorization": f"Bearer {token}"},
                           json={"title": "чужой проект"})
        assert resp.status_code == 401

    def test_unknown_project_is_404(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        resp = client.post("/growth/api/public/projects/999/changes",
                           headers={"Authorization": "Bearer x"},
                           json={"title": "x"})
        assert resp.status_code == 404

    def test_does_not_require_a_session_cookie(self, monkeypatch, tmp_path):
        """Смысл входа для машины -- не нужен человеческий вход вообще."""
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        token = _rotate_token(client)
        client.post("/growth/api/logout")

        resp = client.post("/growth/api/public/projects/1/changes",
                           headers={"Authorization": f"Bearer {token}"},
                           json={"title": "без сессии"})
        assert resp.status_code == 200
