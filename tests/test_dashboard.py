"""
Доска фаундера: сигналы и рекомендация одним списком, с ярлыком «кто нашёл».

Раньше, чтобы понять, с чего начать день, приходилось обходить экраны по
кругу. Проверяется: /api/dashboard сводит то же самое (что уже отдают
/api/alerts и /api/growth) в один список и правильно подписывает, какой
агент отвечает за какую проблему -- без выдумывания новой логики поверх
уже работающих rules.py/growth_loop.py.
"""

import pytest

from tests.test_platform_api import _alert, _client, _login, _project_id


def _cards(client):
    return client.get("/growth/api/dashboard").json()["cards"]


class TestAlertCards:
    def test_alert_becomes_a_card_with_agent_label(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        pid = _project_id(factory)
        with factory() as session:
            _alert(session, pid, "7d")  # payments_started_no_success -> diagnostician
        _login(client)

        cards = _cards(client)
        assert len(cards) == 1
        assert cards[0]["agent"] == "diagnostician"
        assert cards[0]["agent_title"] == "Диагност"
        assert cards[0]["source"] == "alert"
        assert "Понял" in [a["label"] for a in cards[0]["actions"]]

    def test_traffic_category_maps_to_marketer(self, monkeypatch, tmp_path):
        from app.models import Alert, AlertCategory, AlertSeverity, AlertStatus, ConfidenceLevel

        client, factory = _client(monkeypatch, tmp_path)
        pid = _project_id(factory)
        with factory() as session:
            session.add(Alert(
                project_id=pid, fingerprint=f"{pid}/traffic_no_signups/7d/signup",
                category=AlertCategory.traffic_no_signups, severity=AlertSeverity.p1,
                confidence=ConfidenceLevel.medium, title="Клики есть, регистраций нет",
                message="120 кликов, 0 регистраций", status=AlertStatus.open,
            ))
            session.commit()
        _login(client)

        assert _cards(client)[0]["agent"] == "marketer"

    def test_acknowledged_alert_is_not_a_card(self, monkeypatch, tmp_path):
        """Доска фаундера не должна повторять баг, который только что
        починили в /api/alerts."""
        client, factory = _client(monkeypatch, tmp_path)
        pid = _project_id(factory)
        with factory() as session:
            _alert(session, pid, "7d")
        _login(client)
        alert_id = client.get("/growth/api/alerts").json()[0]["id"]
        client.post(f"/growth/api/alerts/{alert_id}/ack")

        assert _cards(client) == []

    def test_no_problems_is_a_normal_result(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        body = client.get("/growth/api/dashboard").json()
        assert body["cards"] == []
        assert "всё в норме" in body["hint"]


class TestRecommendationCards:
    def _propose(self, session, pid, pp):
        from app.growth_loop import propose_if_needed
        from app.truepost_playbook import truepost_playbook
        return propose_if_needed(session, pid, pp, truepost_playbook)

    def test_recommendation_becomes_a_card(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        pid = _project_id(factory)
        pp = dict(registrations=20, channels_created=6, first_post_feedback_good=2,
                  first_post_feedback_bad=2, pricing_viewed=1, payment_started=0, payment_success=0)
        with factory() as session:
            self._propose(session, pid, pp)
        _login(client)

        cards = _cards(client)
        assert len(cards) == 1
        assert cards[0]["source"] == "recommendation"
        assert cards[0]["agent"] == "diagnostician"  # low_channel_rate -> onboarding
        assert "Сделаю" in [a["label"] for a in cards[0]["actions"]]
        assert cards[0]["tested_by"] is None  # эксперимент ещё не запущен

    def test_running_experiment_is_attached_to_its_card(self, monkeypatch, tmp_path):
        from app.growth_loop import accept_recommendation

        client, factory = _client(monkeypatch, tmp_path)
        pid = _project_id(factory)
        pp = dict(registrations=20, channels_created=6, first_post_feedback_good=2,
                  first_post_feedback_bad=2, pricing_viewed=1, payment_started=0, payment_success=0)
        with factory() as session:
            rec = self._propose(session, pid, pp)
            accept_recommendation(session, rec, pp)
        _login(client)

        cards = _cards(client)
        assert len(cards) == 1
        assert cards[0]["source"] == "experiment"
        assert cards[0]["tested_by"] is not None
        assert "Тестировщик" in cards[0]["tested_by"]

    def test_requires_auth(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        assert client.get("/growth/api/dashboard").status_code == 401
