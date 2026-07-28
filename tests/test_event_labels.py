"""
Названия событий живой ленты (задача B5).

Шаги воронки у аналитика фиксированные, а типы событий продукт присылает
свои. Незнакомый код показывался владельцу как есть — английский
snake_case в русском интерфейсе. Придумывать перевод самим нельзя: код
может значить что угодно, и выдуманное название врало бы про продукт.

Поэтому проверяется ровно это: список типов берётся из того, что продукт
ПРИСЛАЛ, названия задаёт владелец, а предложение ИИ ничего не сохраняет
само и не может добавить событие, которого не было.
"""

import pytest

from tests.test_platform_api import _client, _login


def _fake_events(monkeypatch, types, ok=True, status=None):
    """Подменяем коннектор: тест про названия, а не про поход в чужой продукт."""
    import app.connectors.user_events as ue

    async def fake_fetch(base_url, token, period_minutes=120, limit=200, **kw):
        if not ok:
            return {"ok": False, "status": status or "not_found"}
        return {"ok": True, "events": [
            {"event_id": str(i), "event_type": t, "user_key": "u_1",
             "created_at": "2026-07-28T00:00:00Z"}
            for i, t in enumerate(types)
        ]}

    monkeypatch.setattr(ue, "fetch_user_events", fake_fetch)


class TestObservedTypes:
    def test_types_come_from_the_product(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        _fake_events(monkeypatch, ["user_registered", "trial_extended"])

        body = client.get("/growth/api/projects/1/events").json()
        assert [t["key"] for t in body["types"]] == ["trial_extended", "user_registered"]
        assert all(t["observed"] for t in body["types"])
        assert all(t["label"] == "" for t in body["types"])

    def test_named_type_survives_quiet_week(self, monkeypatch, tmp_path):
        """Событие не приходило неделю -- имя, данное владельцем, всё равно
        видно в настройках, иначе оно пропадало бы вместе с затишьем."""
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        _fake_events(monkeypatch, ["user_registered"])
        client.put("/growth/api/projects/1/stages",
                   json={"event_labels": {"warehouse_synced": "Синхронизировал склад"}})

        body = client.get("/growth/api/projects/1/events").json()
        rows = {t["key"]: t for t in body["types"]}
        assert rows["warehouse_synced"]["label"] == "Синхронизировал склад"
        assert rows["warehouse_synced"]["observed"] is False

    def test_empty_is_explained_not_silent(self, monkeypatch, tmp_path):
        """«Событий нет» и «продукт не отдаёт ленту» -- разные вещи, и делать
        с ними надо разное."""
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        _fake_events(monkeypatch, [], ok=False, status="not_found")

        body = client.get("/growth/api/projects/1/events").json()
        assert body["types"] == []
        assert "user-events" in body["hint"]

    def test_no_events_at_all_says_so(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        _fake_events(monkeypatch, [])

        body = client.get("/growth/api/projects/1/events").json()
        assert body["types"] == []
        assert "не прислал" in body["hint"]


class TestLabelsReachTheFeed:
    def test_saved_label_is_returned_by_live(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        _fake_events(monkeypatch, ["warehouse_synced"])
        client.put("/growth/api/projects/1/stages",
                   json={"event_labels": {"warehouse_synced": "Синхронизировал склад"}})

        live = client.get("/growth/api/live?period_minutes=60").json()
        assert live["event_labels"]["warehouse_synced"] == "Синхронизировал склад"

    def test_labels_merge_and_do_not_wipe_each_other(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        _fake_events(monkeypatch, ["a", "b"])
        client.put("/growth/api/projects/1/stages", json={"event_labels": {"a": "Первое"}})
        client.put("/growth/api/projects/1/stages", json={"event_labels": {"b": "Второе"}})

        labels = client.get("/growth/api/live?period_minutes=60").json()["event_labels"]
        assert labels == {"a": "Первое", "b": "Второе"}


class TestAutoname:
    def test_without_llm_says_what_is_missing(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        _fake_events(monkeypatch, ["user_registered"])

        resp = client.post("/growth/api/projects/1/events/autoname")
        assert resp.status_code == 503
        assert "LLM" in resp.json()["detail"]

    def test_nothing_to_name_is_422_with_reason(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        _fake_events(monkeypatch, [])
        monkeypatch.setattr("app.ask.is_configured", lambda settings: True)

        resp = client.post("/growth/api/projects/1/events/autoname")
        assert resp.status_code == 422
        assert "не прислал" in resp.json()["detail"]

    def test_proposal_is_limited_to_observed_types(self, monkeypatch, tmp_path):
        """ИИ не должен добавить событие, которого продукт не присылал:
        владелец решит, что оно есть."""
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        _fake_events(monkeypatch, ["trial_extended"])
        monkeypatch.setattr("app.ask.is_configured", lambda settings: True)

        async def fake_answer(question, context, settings, **kw):
            return '{"trial_extended": "Продлил пробный", "выдуманное": "Чепуха"}'

        monkeypatch.setattr("app.ask.answer_question", fake_answer)
        body = client.post("/growth/api/projects/1/events/autoname").json()
        assert body["proposed"] == {"trial_extended": "Продлил пробный"}

    def test_proposal_is_not_saved_by_itself(self, monkeypatch, tmp_path):
        """Аналитик предлагает, кнопку нажимает человек."""
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        _fake_events(monkeypatch, ["trial_extended"])
        monkeypatch.setattr("app.ask.is_configured", lambda settings: True)

        async def fake_answer(question, context, settings, **kw):
            return '{"trial_extended": "Продлил пробный"}'

        monkeypatch.setattr("app.ask.answer_question", fake_answer)
        client.post("/growth/api/projects/1/events/autoname")

        rows = {t["key"]: t for t in client.get("/growth/api/projects/1/events").json()["types"]}
        assert rows["trial_extended"]["label"] == ""

    @pytest.mark.parametrize("answer", ["не json", "", "{сломанный json"])
    def test_broken_llm_answer_is_502_not_500(self, monkeypatch, tmp_path, answer):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        _fake_events(monkeypatch, ["trial_extended"])
        monkeypatch.setattr("app.ask.is_configured", lambda settings: True)

        async def fake_answer(question, context, settings, **kw):
            return answer

        monkeypatch.setattr("app.ask.answer_question", fake_answer)
        assert client.post("/growth/api/projects/1/events/autoname").status_code == 502
