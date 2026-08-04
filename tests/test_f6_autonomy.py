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


class TestAlertModeIsNotACache:
    """
    Режим live-уведомлений хранился в кэше диагностик с TTL 6 часов: через
    шесть часов после того, как владелец выключил уведомления, они сами
    возвращались на «smart». Настройка человека не имеет срока годности.
    """

    def test_mode_survives_expired_cache(self, monkeypatch, tmp_path):
        from datetime import timedelta

        from app.models import DeepDiagnosticsCache, utcnow
        from app.service import ALERT_MODE_CACHE_KEY, get_alert_mode, set_alert_mode

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            set_alert_mode(session, pid, "off")

        # Кэш диагностик протух целиком -- выбор владельца обязан остаться.
        with session_factory() as session:
            for row in session.query(DeepDiagnosticsCache).all():
                row.expires_at = utcnow() - timedelta(hours=1)
                session.add(row)
            session.commit()

        with session_factory() as session:
            assert get_alert_mode(session, pid) == "off"

    def test_legacy_value_in_cache_is_still_honoured(self, monkeypatch, tmp_path):
        """Выбор, сделанный до переезда, не должен потеряться при обновлении."""
        from app.service import ALERT_MODE_CACHE_KEY, get_alert_mode, save_diagnostics_cache

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            save_diagnostics_cache(session, pid, ALERT_MODE_CACHE_KEY,
                                    "manual_set", {"mode": "founder"}, ok=True)
        with session_factory() as session:
            assert get_alert_mode(session, pid) == "founder"

    def test_default_is_smart(self, monkeypatch, tmp_path):
        from app.service import get_alert_mode

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            assert get_alert_mode(session, pid) == "smart"


class TestLegacyEndpointsRequireAuth:
    """
    Пять легаси-маршрутов в main.py отдавали сырую аналитику, сигналы и
    снимки метрик без единой проверки: кто угодно, знающий адрес, читал
    чужие бизнес-данные.
    """

    def _app(self, monkeypatch, tmp_path):
        import importlib

        import app.config as config
        db_path = tmp_path / "main_auth.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        monkeypatch.setenv("PLATFORM_ADMIN_PASSWORD", "secret")
        monkeypatch.setenv("BOT_TOKEN", "")
        config.get_settings.cache_clear()
        import app.db as db_module
        importlib.reload(db_module)
        import app.platform_api as platform_api
        importlib.reload(platform_api)
        import app.main as main_module
        importlib.reload(main_module)
        db_module.init_db()
        from fastapi.testclient import TestClient
        return TestClient(main_module.app)

    @pytest.mark.parametrize("method,path", [
        ("get", "/status"),
        ("get", "/api/alerts"),
        ("get", "/api/snapshots"),
        ("get", "/api/memory"),
        ("post", "/api/run"),
    ])
    def test_anonymous_is_refused(self, monkeypatch, tmp_path, method, path):
        client = self._app(monkeypatch, tmp_path)
        resp = getattr(client, method)(path)
        assert resp.status_code == 401, f"{path} отдаёт данные без авторизации"

    def test_health_stays_public_and_shows_build(self, monkeypatch, tmp_path):
        """Liveness-check и проверка деплоя обязаны работать без пароля."""
        client = self._app(monkeypatch, tmp_path)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json().get("build_marker")


# ---------------------------------------------------------------------------
# R4: минус-фразы в Директе -- первая настоящая автоматизация уровня 2/3
# ---------------------------------------------------------------------------

def _intelligence(ad_group_id="777", query="скачать бесплатно торрент", cost=340.0):
    return {"safe_negatives": [{
        "query": query, "reason": "семантика: халява", "cost": cost, "clicks": 12,
        "ad_group_id": ad_group_id, "ad_group_name": "Поиск / Основная",
        "campaign_id": "111", "campaign_name": "АвтоПост поиск",
    }]}


class TestDirectWriteClient:
    def test_not_configured_without_token(self, monkeypatch):
        import app.config as config
        from app.connectors import direct_write

        monkeypatch.delenv("YANDEX_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("DIRECT_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("DIRECT_WRITE_OAUTH_TOKEN", raising=False)
        config.get_settings.cache_clear()
        try:
            assert direct_write.is_configured(config.get_settings()) is False
        finally:
            config.get_settings.cache_clear()

    def test_falls_back_to_read_token(self, monkeypatch):
        """У Директа нет отдельного scope на запись -- если специальный
        токен не задан, пишем тем же, которым читаем отчёты."""
        import app.config as config
        from app.connectors import direct_write

        monkeypatch.setenv("YANDEX_OAUTH_TOKEN", "shared-tok")
        config.get_settings.cache_clear()
        try:
            assert direct_write.is_configured(config.get_settings()) is True
        finally:
            config.get_settings.cache_clear()

    def test_api_error_with_http_200_is_not_swallowed(self, monkeypatch):
        """Главная ловушка Директа: отказ приходит с кодом 200 в теле."""
        import httpx

        import app.config as config
        from app.connectors import direct_write

        monkeypatch.setenv("YANDEX_OAUTH_TOKEN", "tok")
        config.get_settings.cache_clear()

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw):
                return httpx.Response(200, json={"error": {
                    "error_code": 54, "error_string": "Нет прав на объект"}})

        monkeypatch.setattr(direct_write.httpx, "AsyncClient", lambda **kw: FakeClient())
        try:
            with pytest.raises(direct_write.DirectWriteError, match="Нет прав"):
                asyncio.run(direct_write._call(config.get_settings(), "adgroups", "get", {}))
        finally:
            config.get_settings.cache_clear()

    def test_existing_negatives_are_preserved(self, monkeypatch):
        """adgroups.update затирает список целиком -- то, что владелец
        добавил руками, обязано пережить автоматическую правку."""
        import app.config as config
        from app.connectors import direct_write

        monkeypatch.setenv("YANDEX_OAUTH_TOKEN", "tok")
        config.get_settings.cache_clear()
        sent = {}

        async def fake_call(settings, service, method, params, **kw):
            if method == "get":
                return {"AdGroups": [{"Id": 777, "NegativeKeywords": {"Items": ["моё старое"]}}]}
            sent["params"] = params
            return {"UpdateResults": [{"Id": 777}]}

        monkeypatch.setattr(direct_write, "_call", fake_call)
        try:
            res = asyncio.run(direct_write.add_negative_keywords(
                config.get_settings(), "777", ["новая фраза"]))
        finally:
            config.get_settings.cache_clear()

        written = sent["params"]["AdGroups"][0]["NegativeKeywords"]["Items"]
        assert "моё старое" in written, "автоправка стёрла минус-слова владельца"
        assert "новая фраза" in written
        assert res.applied == ["новая фраза"]

    def test_partial_failure_is_reported_not_hidden(self, monkeypatch):
        import app.config as config
        from app.connectors import direct_write

        monkeypatch.setenv("YANDEX_OAUTH_TOKEN", "tok")
        config.get_settings.cache_clear()

        async def fake_call(settings, service, method, params, **kw):
            if method == "get":
                return {"AdGroups": [{"Id": 777, "NegativeKeywords": {"Items": []}}]}
            return {"UpdateResults": [{"Errors": [{"Message": "Недопустимая фраза"}]}]}

        monkeypatch.setattr(direct_write, "_call", fake_call)
        try:
            res = asyncio.run(direct_write.add_negative_keywords(
                config.get_settings(), "777", ["плохая"]))
        finally:
            config.get_settings.cache_clear()
        assert res.ok is False
        assert res.skipped and "Недопустимая фраза" in res.skipped[0][1]


class TestMarketerNegatives:
    def test_level_one_only_proposes(self, monkeypatch, tmp_path):
        from app.marketer_actions import handle_safe_negatives
        from app.models import AgentActionStatus, Project
        from app.connectors import direct_write

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)

        def boom(*a, **kw):
            raise AssertionError("на уровне 1 агент не имеет права писать в Директ")

        monkeypatch.setattr(direct_write, "add_negative_keywords", boom)
        with session_factory() as session:
            project = session.get(Project, pid)
            actions = asyncio.run(handle_safe_negatives(session, project, _intelligence(), level=1))
        assert len(actions) == 1
        assert actions[0].status == AgentActionStatus.proposed.value
        assert actions[0].domain == "direct_negative_keywords"

    def test_level_two_applies(self, monkeypatch, tmp_path):
        from app.marketer_actions import handle_safe_negatives
        from app.models import AgentActionStatus, Project
        from app.connectors import direct_write

        client, session_factory = _client(monkeypatch, tmp_path, YANDEX_OAUTH_TOKEN="tok")
        pid = _project_id(session_factory)

        async def fake_add(settings, ad_group_id, phrases):
            res = direct_write.DirectWriteResult()
            res.applied = list(phrases)
            return res

        monkeypatch.setattr(direct_write, "add_negative_keywords", fake_add)
        with session_factory() as session:
            project = session.get(Project, pid)
            actions = asyncio.run(handle_safe_negatives(session, project, _intelligence(), level=2))
        assert actions[0].status == AgentActionStatus.applied.value
        assert actions[0].applied_at is not None
        assert "Применил сам" in actions[0].reasoning

    def test_without_ad_group_id_is_honestly_blocked(self, monkeypatch, tmp_path):
        """Знаем что минусовать, не знаем куда -- честно говорим, а не молчим."""
        from app.marketer_actions import handle_safe_negatives
        from app.models import AgentActionStatus, Project

        client, session_factory = _client(monkeypatch, tmp_path, YANDEX_OAUTH_TOKEN="tok")
        pid = _project_id(session_factory)
        with session_factory() as session:
            project = session.get(Project, pid)
            actions = asyncio.run(handle_safe_negatives(
                session, project, _intelligence(ad_group_id=None), level=3))
        assert actions[0].status == AgentActionStatus.blocked_not_configured.value
        assert "номер группы" in actions[0].reasoning

    def test_same_phrase_is_not_proposed_twice(self, monkeypatch, tmp_path):
        from app.marketer_actions import handle_safe_negatives
        from app.models import Project

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            project = session.get(Project, pid)
            first = asyncio.run(handle_safe_negatives(session, project, _intelligence(), level=1))
            second = asyncio.run(handle_safe_negatives(session, project, _intelligence(), level=1))
        assert len(first) == 1 and second == []

    def test_no_intelligence_is_not_an_error(self, monkeypatch, tmp_path):
        from app.marketer_actions import handle_safe_negatives
        from app.models import Project

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            project = session.get(Project, pid)
            assert asyncio.run(handle_safe_negatives(session, project, None, level=3)) == []
            assert asyncio.run(handle_safe_negatives(session, project, {}, level=3)) == []


class TestAutonomyDescriptionsAreTrue:
    """
    Описание уровней обещало «ставки и минус-слова Директа», хотя ни того,
    ни другого не существовало. Обещание в интерфейсе -- такой же факт,
    как число на экране.
    """

    def test_level_three_says_plainly_it_does_not_touch_bids(self, monkeypatch, tmp_path):
        client, session_factory = _client(monkeypatch, tmp_path)
        _login(client)
        text = client.get("/growth/api/dashboard").json()["autonomy_levels"]["3"]["description"].lower()
        # Ставки упоминаться могут -- но только как то, чего агент НЕ делает.
        assert "не трогает" in text
        assert "ставки" in text.split("не трогает")[0], "про ставки должно быть сказано прямо"
        assert "цели" in text, "то, что агент реально делает на уровне 3, названо"

    def test_level_two_promises_only_what_exists(self, monkeypatch, tmp_path):
        client, session_factory = _client(monkeypatch, tmp_path)
        _login(client)
        levels = client.get("/growth/api/dashboard").json()["autonomy_levels"]
        assert "минус-фраз" in levels["2"]["description"].lower()
