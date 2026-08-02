"""
Тесты задачи F6 (уровни автономии).

Проверяется:
- правило payments_invisible_in_metrika (условие + порог выборки);
- уровень автономии -- дефолт, чтение/запись через API, честные границы;
- предложения Маркетолога (AgentAction): создаются при алерте, не дублируются,
  на уровне 3 без настроенной записи честно помечаются "не настроено";
- metrika_write.is_configured() -- true/false по наличию токена и счётчика;
- доска фаундера отдаёт agent_actions и уровень автономии;
- решения владельца по предложению (Сделаю сам / Не буду).
"""
import asyncio

import pytest

from tests.test_platform_api import _client, _login, _project_id


def _metrics(**kw):
    from app.rules import NormalizedMetrics

    defaults = dict(period_key="7d", sources_ok={"product", "metrika"})
    defaults.update(kw)
    return NormalizedMetrics(**defaults)


class TestPaymentsInvisibleRule:
    def test_fires_when_payments_exist_but_metrika_is_silent(self):
        from app.rules import RULES

        rule = next(r for r in RULES if r.rule_id == "payments_invisible_in_metrika")
        m = _metrics(payment_success=5, metrika_payment_success=0)
        result = rule.evaluate(m)
        assert result is not None
        assert result.payload["payment_success"] == 5

    def test_does_not_fire_below_sample_threshold(self):
        """Одна оплата -- может быть просто задержкой синхронизации Метрики,
        не поводом для алерта (в отличие от metrics_discrepancy про signup,
        у которого порога нет вовсе -- здесь цена ошибки выше)."""
        from app.rules import RULES

        rule = next(r for r in RULES if r.rule_id == "payments_invisible_in_metrika")
        m = _metrics(payment_success=1, metrika_payment_success=0)
        assert rule.evaluate(m) is None

    def test_does_not_fire_when_metrika_sees_the_payment(self):
        from app.rules import RULES

        rule = next(r for r in RULES if r.rule_id == "payments_invisible_in_metrika")
        m = _metrics(payment_success=5, metrika_payment_success=5)
        assert rule.evaluate(m) is None

    def test_not_checkable_without_metrika_source(self):
        from app.rules import RULES

        rule = next(r for r in RULES if r.rule_id == "payments_invisible_in_metrika")
        m = _metrics(payment_success=5, metrika_payment_success=0, sources_ok={"product"})
        assert rule.evaluate(m) is None


class TestMetrikaWriteConfigured:
    def test_not_configured_without_token(self):
        from app.connectors import metrika_write

        class P:
            settings_json = {"metrika_counter_id": "123"}

        assert metrika_write.is_configured(P()) is False

    def test_not_configured_without_counter(self):
        from app.connectors import metrika_write

        class P:
            settings_json = {"metrika_management_token": "tok"}

        assert metrika_write.is_configured(P()) is False

    def test_configured_with_both(self):
        from app.connectors import metrika_write

        class P:
            settings_json = {"metrika_management_token": "tok", "metrika_counter_id": "123"}

        assert metrika_write.is_configured(P()) is True

    def test_create_goal_raises_honest_error_when_not_configured(self):
        from app.connectors import metrika_write

        class P:
            settings_json = {}

        with pytest.raises(metrika_write.MetrikaWriteError):
            asyncio.run(metrika_write.create_goal(P(), name="x", goal_type="url", conditions=[]))

    def test_falls_back_to_env_token_when_project_has_none(self, monkeypatch):
        """Токен можно задать в окружении, а не только через настройку
        проекта -- секрет не должен ходить через интерфейс/чат."""
        import app.config as config
        from app.connectors import metrika_write

        monkeypatch.setenv("METRIKA_MANAGEMENT_TOKEN", "env-tok")
        config.get_settings.cache_clear()
        try:
            class P:
                settings_json = {"metrika_counter_id": "123"}

            assert metrika_write.is_configured(P()) is True
        finally:
            config.get_settings.cache_clear()

    def test_project_setting_wins_over_env(self, monkeypatch):
        import app.config as config
        from app.connectors import metrika_write

        monkeypatch.setenv("METRIKA_MANAGEMENT_TOKEN", "env-tok")
        config.get_settings.cache_clear()
        try:
            class P:
                settings_json = {"metrika_management_token": "project-tok", "metrika_counter_id": "123"}

            assert metrika_write._management_token(P()) == "project-tok"
        finally:
            config.get_settings.cache_clear()


class TestAutonomyLevelApi:
    def test_default_level_is_one(self, monkeypatch, tmp_path):
        client, session_factory = _client(monkeypatch, tmp_path)
        _login(client)
        projects = client.get("/growth/api/projects").json()
        assert projects[0]["autonomy_level"] == 1

    def test_owner_can_change_level(self, monkeypatch, tmp_path):
        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        _login(client)
        r = client.post(f"/growth/api/projects/{pid}/autonomy", json={"level": 3})
        assert r.status_code == 200, r.text
        projects = client.get("/growth/api/projects").json()
        assert projects[0]["autonomy_level"] == 3

    def test_invalid_level_is_rejected(self, monkeypatch, tmp_path):
        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        _login(client)
        r = client.post(f"/growth/api/projects/{pid}/autonomy", json={"level": 7})
        assert r.status_code == 422

    def test_dashboard_reports_level_and_options(self, monkeypatch, tmp_path):
        client, session_factory = _client(monkeypatch, tmp_path)
        _login(client)
        d = client.get("/growth/api/dashboard").json()
        assert d["autonomy_level"] == 1
        assert set(d["autonomy_levels"].keys()) == {"1", "2", "3"}


def _payment_alert(session, project_id, payment_success=5):
    from app.models import Alert, AlertCategory, AlertSeverity, AlertStatus, ConfidenceLevel

    alert = Alert(
        project_id=project_id,
        fingerprint=f"{project_id}/payments_invisible_in_metrika/7d/payment_success",
        category=AlertCategory.payments_invisible_in_metrika,
        severity=AlertSeverity.p1,
        confidence=ConfidenceLevel.medium,
        title="Оплаты не видны в Метрике", message="5 оплат, 0 в Метрике",
        status=AlertStatus.open,
        payload_json={"payment_success": payment_success},
    )
    session.add(alert)
    session.commit()
    session.refresh(alert)
    return alert


class TestMarketerProposals:
    def test_proposes_on_level_one_without_applying(self, monkeypatch, tmp_path):
        from app.marketer_actions import handle_payment_visibility_alert
        from app.models import AgentActionStatus

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            from app.models import Project

            project = session.get(Project, pid)
            alert = _payment_alert(session, pid)
            action = asyncio.run(handle_payment_visibility_alert(session, project, alert, level=1))
        assert action is not None
        assert action.status == AgentActionStatus.proposed.value
        assert action.agent == "marketer"
        assert action.domain == "metrika_goal"

    def test_does_not_duplicate_open_proposal(self, monkeypatch, tmp_path):
        from app.marketer_actions import handle_payment_visibility_alert
        from app.models import AgentAction, Project

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            project = session.get(Project, pid)
            alert = _payment_alert(session, pid)
            first = asyncio.run(handle_payment_visibility_alert(session, project, alert, level=1))
            second = asyncio.run(handle_payment_visibility_alert(session, project, alert, level=1))
        assert second is None
        with session_factory() as session:
            from sqlmodel import select

            rows = session.exec(select(AgentAction).where(AgentAction.project_id == pid)).all()
        assert len(rows) == 1

    def test_level_three_without_write_token_is_honestly_blocked(self, monkeypatch, tmp_path):
        from app.marketer_actions import handle_payment_visibility_alert
        from app.models import AgentActionStatus, Project

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            project = session.get(Project, pid)
            alert = _payment_alert(session, pid)
            action = asyncio.run(handle_payment_visibility_alert(session, project, alert, level=3))
        assert action.status == AgentActionStatus.blocked_not_configured.value
        assert "не настроено" in action.reasoning or "не могу" in action.reasoning

    def test_level_three_applies_when_configured(self, monkeypatch, tmp_path):
        from app.connectors import metrika_write
        from app.marketer_actions import handle_payment_visibility_alert
        from app.models import AgentActionStatus, Project

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)

        async def fake_create_goal(project, *, name, goal_type, conditions):
            return {"id": 999, "name": name}

        monkeypatch.setattr(metrika_write, "create_goal", fake_create_goal)

        with session_factory() as session:
            project = session.get(Project, pid)
            sj = dict(project.settings_json or {})
            sj["metrika_management_token"] = "tok"
            sj["metrika_counter_id"] = "123"
            sj["metrika_payment_success_goal_condition"] = {
                "type": "url", "conditions": [{"type": "contain", "url": "/success"}],
            }
            project.settings_json = sj
            session.add(project)
            session.commit()

            alert = _payment_alert(session, pid)
            action = asyncio.run(handle_payment_visibility_alert(session, project, alert, level=3))
        assert action.status == AgentActionStatus.applied.value
        assert action.applied_at is not None
        assert action.payload_json["after"]["id"] == 999


class TestAgentActionDecision:
    def test_owner_can_mark_proposal_as_done_or_rejected(self, monkeypatch, tmp_path):
        from app.marketer_actions import handle_payment_visibility_alert
        from app.models import Project

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            project = session.get(Project, pid)
            alert = _payment_alert(session, pid)
            action = asyncio.run(handle_payment_visibility_alert(session, project, alert, level=1))
        _login(client)

        d = client.get("/growth/api/dashboard").json()
        assert any(a["id"] == action.id and a["status"] == "proposed" for a in d["agent_actions"])

        r = client.post(f"/growth/api/agent-actions/{action.id}/apply")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "applied"

        r2 = client.post(f"/growth/api/agent-actions/{action.id}/apply")
        assert r2.status_code == 409

    def test_reject_marks_as_rejected(self, monkeypatch, tmp_path):
        from app.marketer_actions import handle_payment_visibility_alert
        from app.models import Project

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            project = session.get(Project, pid)
            alert = _payment_alert(session, pid)
            action = asyncio.run(handle_payment_visibility_alert(session, project, alert, level=1))
        _login(client)
        r = client.post(f"/growth/api/agent-actions/{action.id}/reject")
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"
