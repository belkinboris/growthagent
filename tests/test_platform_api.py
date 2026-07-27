"""
Тесты веб-платформы (`app/platform_api.py`, `app/platform_auth.py`).

Платформа несёт весь интерфейс владельца, но до этих тестов проверялась
только скриншотами. Здесь закреплено поведение, где уже находились ошибки
на живом прогоне:

- воронка читается из вложенной структуры combined-снэпшота
  (`{"product": {...}, "direct": {...}}`), а не из плоских ключей;
- одинаковые сигналы из разных окон схлопываются в один;
- цена регистрации делится на регистрации ИЗ ДИРЕКТА, а не на все;
- «всё в норме» не показывается, когда проверок не было;
- статусы telegram/llm считаются по конфигурации, а не по мёртвой строке в БД.

Каждый тест поднимает своё приложение со своей БД в памяти: платформа
читает настройки через lru_cache, поэтому окружение задаётся до импорта
и кэш сбрасывается явно.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

PASSWORD = "test-secret"


def _client(monkeypatch, tmp_path, *, with_project=True, password=PASSWORD, **env):
    """Приложение с чистой БД. Возвращает (client, session_factory)."""
    import app.config as config

    db_path = tmp_path / f"platform_{abs(hash(str(env) + str(with_project)))}.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("PLATFORM_COOKIE_SECURE", "false")
    if password is None:
        monkeypatch.delenv("PLATFORM_ADMIN_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("PLATFORM_ADMIN_PASSWORD", password)
    # Проект из env -- legacy-путь: без base_url заглушка не создаётся,
    # это и есть сценарий «платформа без подключённого проекта».
    if with_project:
        monkeypatch.setenv("PROJECT_NAME", "Тест")
        monkeypatch.setenv("PROJECT_BASE_URL", "https://example.test")
        monkeypatch.setenv("PROJECT_INTERNAL_API_TOKEN", "tok")
    else:
        monkeypatch.setenv("PROJECT_BASE_URL", "")
        monkeypatch.delenv("PROJECT_INTERNAL_API_TOKEN", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    config.get_settings.cache_clear()

    # db.py читает settings на импорте, поэтому модули перезагружаются
    import importlib

    import app.db as db_module
    importlib.reload(db_module)
    import app.platform_api as platform_api
    importlib.reload(platform_api)

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(platform_api.router, prefix="/growth")
    db_module.init_db()
    return TestClient(app), db_module.get_session


def _login(client):
    resp = client.post("/growth/api/login", json={"password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return client


def _snapshot(session, project_id, period_key="7d", **product):
    """combined-снэпшот в том виде, в каком его пишет scheduler."""
    from app.models import MetricSnapshot

    now = datetime.now(timezone.utc)
    metrics = {
        "signup": 12, "activation_1": 8, "activation_2": 40,
        "payment_started": 3, "payment_success": 0, "revenue": 0,
    }
    metrics.update(product)
    session.add(MetricSnapshot(
        project_id=project_id, period_key=period_key,
        period_start=now - timedelta(days=7), period_end=now,
        source="combined", as_of=now,
        metrics_json={
            "product": metrics,
            "direct": {"clicks": 240, "spend": 3600},
            "metrika": None, "yookassa": None,
        },
    ))
    session.commit()


def _project_id(session_factory):
    from sqlmodel import select

    from app.models import Project

    with session_factory() as session:
        return session.exec(select(Project)).first().id


# ---------------------------------------------------------------------------
# Доступ
# ---------------------------------------------------------------------------


class TestAuth:
    def test_no_password_configured_blocks_platform(self, monkeypatch, tmp_path):
        """Без PLATFORM_ADMIN_PASSWORD платформа закрыта целиком, а не
        открыта всем: пустой пароль не должен означать «вход свободный»."""
        client, _ = _client(monkeypatch, tmp_path, password=None)
        assert client.get("/growth/api/overview").status_code == 503
        assert client.post("/growth/api/login", json={"password": ""}).status_code == 503

    def test_wrong_password_rejected(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        assert client.post("/growth/api/login", json={"password": "нет"}).status_code == 401

    def test_session_cycle(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        assert client.get("/growth/api/overview").status_code == 401
        _login(client)
        assert client.get("/growth/api/overview").status_code == 200
        client.post("/growth/api/logout")
        assert client.get("/growth/api/overview").status_code == 401

    def test_bearer_token_works_without_cookie(self, monkeypatch, tmp_path):
        """Скрипты ходят с Bearer, браузер -- с cookie; путь один и тот же."""
        client, _ = _client(monkeypatch, tmp_path)
        token = client.post("/growth/api/login", json={"password": PASSWORD}).json()["token"]
        client.cookies.clear()
        assert client.get("/growth/api/overview").status_code == 401
        resp = client.get("/growth/api/overview", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200

    def test_forged_token_rejected(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        far_future = int((datetime.now(timezone.utc) + timedelta(days=999)).timestamp())
        resp = client.get("/growth/api/overview",
                          headers={"Authorization": f"Bearer {far_future}.deadbeef"})
        assert resp.status_code == 401

    def test_public_session_endpoint_leaks_nothing(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        body = client.get("/growth/api/session").json()
        assert body == {"configured": True}


# ---------------------------------------------------------------------------
# Аккаунты (B1, часть 1)
# ---------------------------------------------------------------------------


class TestAccountsAPI:
    def test_register_then_login_by_email(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        resp = client.post("/growth/api/register",
                           json={"email": "ivan@example.com", "password": "qwerty12"})
        assert resp.status_code == 200, resp.text
        assert client.get("/growth/api/overview").status_code == 200

        client.post("/growth/api/logout")
        assert client.get("/growth/api/overview").status_code == 401

        resp = client.post("/growth/api/login",
                           json={"email": "ivan@example.com", "password": "qwerty12"})
        assert resp.status_code == 200, resp.text
        assert client.get("/growth/api/overview").status_code == 200

    def test_duplicate_email_is_409_not_500(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        client.post("/growth/api/register",
                    json={"email": "ivan@example.com", "password": "qwerty12"})
        resp = client.post("/growth/api/register",
                           json={"email": "IVAN@example.com", "password": "другой123"})
        assert resp.status_code == 409

    def test_short_password_rejected_with_reason(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        resp = client.post("/growth/api/register",
                           json={"email": "ivan@example.com", "password": "123"})
        assert resp.status_code == 400
        assert "8" in resp.json()["detail"]

    def test_wrong_account_password_rejected(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        client.post("/growth/api/register",
                    json={"email": "ivan@example.com", "password": "qwerty12"})
        client.post("/growth/api/logout")
        resp = client.post("/growth/api/login",
                           json={"email": "ivan@example.com", "password": "неверный"})
        assert resp.status_code == 401

    def test_account_login_does_not_need_env_password(self, monkeypatch, tmp_path):
        """Клиент на своём сервере не обязан заводить PLATFORM_ADMIN_PASSWORD:
        зарегистрировался -- значит, платформа настроена."""
        client, _ = _client(monkeypatch, tmp_path)
        client.post("/growth/api/register",
                    json={"email": "ivan@example.com", "password": "qwerty12"})
        assert client.get("/growth/api/session").json()["configured"] is True

    def test_registered_user_adopts_existing_project(self, monkeypatch, tmp_path):
        """Живой проект был заведён до аккаунтов -- он должен достаться
        первому зарегистрировавшемуся, иначе исчезнет с изоляцией."""
        from app import accounts
        client, session_factory = _client(monkeypatch, tmp_path)
        resp = client.post("/growth/api/register",
                           json={"email": "ivan@example.com", "password": "qwerty12"})
        assert resp.json()["adopted_projects"] == 1
        with session_factory() as session:
            from app.models import PlatformUser
            from sqlmodel import select
            user = session.exec(select(PlatformUser)).first()
            assert len(accounts.user_project_ids(session, user.id)) == 1

    def test_me_tells_who_is_logged_in(self, monkeypatch, tmp_path):
        """Интерфейс после перезагрузки страницы знает о сессии только из
        cookie -- имя аккаунта приходится спрашивать у сервера."""
        client, _ = _client(monkeypatch, tmp_path)
        client.post("/growth/api/register",
                    json={"email": "Ivan@example.com", "password": "qwerty12",
                          "display_name": "Иван"})
        me = client.get("/growth/api/me").json()
        assert me == {"email": "ivan@example.com", "display_name": "Иван",
                      "is_owner": False, "kind": "account"}

        client.post("/growth/api/logout")
        _login(client)
        assert client.get("/growth/api/me").json()["kind"] == "platform_owner"

    def test_me_requires_session(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        assert client.get("/growth/api/me").status_code == 401

    def test_owner_env_login_still_works(self, monkeypatch, tmp_path):
        """Аккаунты не должны сломать вход владельца одним паролем."""
        client, _ = _client(monkeypatch, tmp_path)
        client.post("/growth/api/register",
                    json={"email": "ivan@example.com", "password": "qwerty12"})
        client.post("/growth/api/logout")
        assert client.post("/growth/api/login", json={"password": PASSWORD}).status_code == 200
        assert client.get("/growth/api/overview").status_code == 200


# ---------------------------------------------------------------------------
# Изоляция данных по владельцу (B1.2 / B2)
# ---------------------------------------------------------------------------


def _second_project(session_factory, name="Чужой проект"):
    """Второй проект в базе -- «соседский». Активным не делаем: активный
    в v1 ровно один, и это проект из окружения."""
    from app.models import Project

    with session_factory() as session:
        p = Project(name=name, type="telegram_saas", base_url="https://other.test",
                    connector_name="truepost", is_active=False,
                    settings_json={"internal_api_token": "tok2"})
        session.add(p)
        session.commit()
        session.refresh(p)
        return p.id


def _register(client, email, password="qwerty12"):
    resp = client.post("/growth/api/register", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestOwnerIsolation:
    """Клиент видит только свои проекты. До этих проверок вход был один на
    всех: любой вошедший читал чужую воронку, деньги и сигналы."""

    def _two_tenants(self, monkeypatch, tmp_path):
        from app import accounts

        client, session_factory = _client(monkeypatch, tmp_path)
        ivan = _register(client, "ivan@example.com")           # усыновляет проект из окружения
        ivan_pid = _project_id(session_factory)
        petr_pid = _second_project(session_factory)

        client.post("/growth/api/logout")
        petr = _register(client, "petr@example.com")           # усыновлять уже нечего
        with session_factory() as session:
            from sqlmodel import select
            from app.models import PlatformUser
            petr_user = session.exec(
                select(PlatformUser).where(PlatformUser.email == "petr@example.com")).first()
            accounts.grant_project(session, petr_pid, petr_user.id)
        return client, session_factory, ivan_pid, petr_pid

    def test_list_shows_only_own_projects(self, monkeypatch, tmp_path):
        client, _, ivan_pid, petr_pid = self._two_tenants(monkeypatch, tmp_path)
        # сейчас в сессии Пётр
        ids = [p["id"] for p in client.get("/growth/api/projects").json()]
        assert ids == [petr_pid]

        client.post("/growth/api/logout")
        client.post("/growth/api/login", json={"email": "ivan@example.com", "password": "qwerty12"})
        assert [p["id"] for p in client.get("/growth/api/projects").json()] == [ivan_pid]

    def test_foreign_project_is_not_found(self, monkeypatch, tmp_path):
        """404, а не 403: 403 подтверждает, что такой проект есть."""
        client, _, ivan_pid, _ = self._two_tenants(monkeypatch, tmp_path)
        assert client.get(f"/growth/api/projects/{ivan_pid}/stages").status_code == 404
        assert client.put(f"/growth/api/projects/{ivan_pid}/stages",
                          json={"titles": {"signup": "Взлом"}}).status_code == 404
        assert client.patch(f"/growth/api/projects/{ivan_pid}",
                            json={"name": "Взлом"}).status_code == 404
        assert client.post(f"/growth/api/projects/{ivan_pid}/retest").status_code == 404
        assert client.post(f"/growth/api/projects/{ivan_pid}/activate").status_code == 404

    def test_overview_shows_own_project(self, monkeypatch, tmp_path):
        """У Петра свой проект не активен -- но он у него единственный,
        и показать надо его, а не «нет проекта» и не чужой."""
        client, _, ivan_pid, petr_pid = self._two_tenants(monkeypatch, tmp_path)
        assert client.get("/growth/api/overview").json()["project"]["id"] == petr_pid

        client.post("/growth/api/logout")
        client.post("/growth/api/login", json={"email": "ivan@example.com", "password": "qwerty12"})
        assert client.get("/growth/api/overview").json()["project"]["id"] == ivan_pid

    def test_funnel_is_not_shared(self, monkeypatch, tmp_path):
        """Главная утечка: цифры чужой воронки на своём экране."""
        client, session_factory, ivan_pid, petr_pid = self._two_tenants(monkeypatch, tmp_path)
        with session_factory() as session:
            _snapshot(session, ivan_pid)

        # У Петра снимков нет -- окно пустое, а не с чужими числами.
        assert client.get("/growth/api/funnel").json()["windows"]["7d"] is None

        client.post("/growth/api/logout")
        client.post("/growth/api/login", json={"email": "ivan@example.com", "password": "qwerty12"})
        assert client.get("/growth/api/funnel").json()["windows"]["7d"]["funnel"]["signup"] == 12

    def test_foreign_alert_cannot_be_touched(self, monkeypatch, tmp_path):
        """Список чужого не покажет, а действие по угаданному номеру
        проходило бы -- это и есть дырявая изоляция."""
        from app.models import Alert, AlertCategory, AlertSeverity, AlertStatus, ConfidenceLevel

        client, session_factory, ivan_pid, _ = self._two_tenants(monkeypatch, tmp_path)
        with session_factory() as session:
            alert = Alert(project_id=ivan_pid, rule_id="r1", fingerprint="f1",
                          title="Чужой сигнал", severity=AlertSeverity.p2,
                          category=AlertCategory.activation_drop, status=AlertStatus.open,
                          confidence=ConfidenceLevel.medium, message="Чужие данные",
                          period_key="24h")
            session.add(alert)
            session.commit()
            session.refresh(alert)
            alert_id = alert.id

        assert client.post(f"/growth/api/alerts/{alert_id}/ack").status_code == 404
        assert client.get("/growth/api/alerts").json() == []

    def test_dev_task_is_owner_only(self, monkeypatch, tmp_path):
        """Задача уходит в репозиторий владельца платформы -- клиенту туда нельзя."""
        client, _, _, _ = self._two_tenants(monkeypatch, tmp_path)
        resp = client.post("/growth/api/dev-task", json={"title": "т", "body": "т"})
        assert resp.status_code == 403

    def test_env_owner_still_sees_everything(self, monkeypatch, tmp_path):
        """Владелец платформы входит паролем из окружения и админит всё."""
        client, _, ivan_pid, petr_pid = self._two_tenants(monkeypatch, tmp_path)
        client.post("/growth/api/logout")
        _login(client)
        ids = sorted(p["id"] for p in client.get("/growth/api/projects").json())
        assert ids == sorted([ivan_pid, petr_pid])
        assert client.get(f"/growth/api/projects/{ivan_pid}/stages").status_code == 200

    def test_activation_does_not_silently_stop_neighbour(self, monkeypatch, tmp_path):
        """Планировщик собирает один проект. Включение своего выключило бы
        сбор у соседа -- отказываем словами, а не молча ломаем."""
        client, session_factory, ivan_pid, petr_pid = self._two_tenants(monkeypatch, tmp_path)
        resp = client.post(f"/growth/api/projects/{petr_pid}/activate")
        assert resp.status_code == 409
        assert "другому пользователю" in resp.json()["detail"]

        from app.models import Project
        with session_factory() as session:
            assert session.get(Project, ivan_pid).is_active is True
            assert session.get(Project, petr_pid).is_active is False


# ---------------------------------------------------------------------------
# Обзор и воронка
# ---------------------------------------------------------------------------


class TestOverview:
    def test_without_project_returns_null_not_error(self, monkeypatch, tmp_path):
        """Первый экран нового пользователя: обзор обязан ответить 200,
        иначе интерфейс не может показать приглашение подключить проект."""
        client, _ = _client(monkeypatch, tmp_path, with_project=False)
        _login(client)
        body = client.get("/growth/api/overview").json()
        assert body["project"] is None
        assert body["build_marker"]

    def test_inactive_project_is_shown_and_marked(self, monkeypatch, tmp_path):
        """Выключенный проект -- это не «проект не подключён». Раньше обзор
        отвечал project: null, и человек шёл подключать проект второй раз."""
        from app.models import Project

        client, session_factory = _client(monkeypatch, tmp_path)
        _login(client)
        with session_factory() as session:
            from sqlmodel import select
            project = session.exec(select(Project)).first()
            project.is_active = False
            session.add(project)
            session.commit()

        body = client.get("/growth/api/overview").json()
        assert body["project"] is not None
        assert body["project"]["is_active"] is False

    def test_active_project_marked_active(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        assert client.get("/growth/api/overview").json()["project"]["is_active"] is True

    def test_telegram_and_llm_status_from_config(self, monkeypatch, tmp_path):
        """Эти интеграции не источники метрик, цикл сбора их не обновляет --
        в БД они навсегда not_configured. Статус считается по конфигурации."""
        client, _ = _client(monkeypatch, tmp_path, BOT_TOKEN="123:abc")
        _login(client)
        statuses = {i["type"]: i["status"] for i in client.get("/growth/api/overview").json()["integrations"]}
        assert statuses["telegram"] == "ok"
        assert statuses["llm"] == "not_configured"

    def test_llm_ok_when_yandex_configured(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path, LLM_PROVIDER="yandex",
                            YANDEX_API_KEY="k", YANDEX_FOLDER_ID="f")
        _login(client)
        statuses = {i["type"]: i["status"] for i in client.get("/growth/api/overview").json()["integrations"]}
        assert statuses["llm"] == "ok"


class TestFunnel:
    def test_reads_nested_combined_snapshot(self, monkeypatch, tmp_path):
        """Регрессия: снэпшот хранит {"product": {...}, "direct": {...}},
        а endpoint читал плоские ключи -- воронка показывала прочерки."""
        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            _snapshot(session, pid)
        _login(client)
        window = client.get("/growth/api/funnel").json()["windows"]["7d"]
        assert window["funnel"]["signup"] == 12
        assert window["funnel"]["activation_1"] == 8

    def test_traffic_falls_back_to_direct_clicks(self, monkeypatch, tmp_path):
        """Продукт не знает про рекламу: трафик приходит из Директа."""
        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            _snapshot(session, pid)
        _login(client)
        window = client.get("/growth/api/funnel").json()["windows"]["7d"]
        assert window["funnel"]["traffic"] == 240

    def test_window_without_snapshot_is_null(self, monkeypatch, tmp_path):
        """Нет снимка -- честный null, а не нули: ноль означал бы «мы
        измерили и там пусто»."""
        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            _snapshot(session, pid, period_key="7d")
        _login(client)
        windows = client.get("/growth/api/funnel").json()["windows"]
        assert windows["3h"] is None
        assert windows["7d"] is not None

    def test_stage_titles_travel_with_numbers(self, monkeypatch, tmp_path):
        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            _snapshot(session, pid)
        _login(client)
        body = client.get("/growth/api/funnel").json()
        assert body["stage_titles"]["signup"] == "Регистрация"
        assert body["stage_titles"]["activation_1"] == "Этап 1"


# ---------------------------------------------------------------------------
# Сигналы
# ---------------------------------------------------------------------------


def _alert(session, project_id, period, severity=None, title="Начатые оплаты без успешных"):
    from app.models import Alert, AlertCategory, AlertSeverity, AlertStatus, ConfidenceLevel, ConfidenceLevel

    session.add(Alert(
        project_id=project_id,
        fingerprint=f"{project_id}/payments_started_no_success/{period}/payment",
        category=AlertCategory.payments_started_no_success,
        severity=severity or AlertSeverity.p1,
        confidence=ConfidenceLevel.medium,
        title=title, message="3 начатых, 0 успешных", status=AlertStatus.open,
    ))
    session.commit()


class TestAlerts:
    def test_same_rule_in_three_windows_collapses(self, monkeypatch, tmp_path):
        """Правило проверяется отдельно в 3h/24h/7d -- это дизайн ядра.
        Владельцу нужен один сигнал с перечнем окон, а не три одинаковых."""
        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            for period in ("3h", "24h", "7d"):
                _alert(session, pid, period)
        _login(client)
        alerts = client.get("/growth/api/alerts").json()
        assert len(alerts) == 1
        assert sorted(alerts[0]["periods"]) == ["24h", "3h", "7d"]

    def test_worst_severity_wins(self, monkeypatch, tmp_path):
        from app.models import AlertSeverity

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            _alert(session, pid, "3h", AlertSeverity.p2)
            _alert(session, pid, "7d", AlertSeverity.p0)
        _login(client)
        assert client.get("/growth/api/alerts").json()[0]["severity"] == "P0"

    def test_ack_and_snooze(self, monkeypatch, tmp_path):
        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            _alert(session, pid, "7d")
        _login(client)
        alert_id = client.get("/growth/api/alerts").json()[0]["id"]
        assert client.post(f"/growth/api/alerts/{alert_id}/ack").json()["status"] == "acknowledged"
        assert client.post(f"/growth/api/alerts/{alert_id}/snooze").json()["status"] == "snoozed"
        assert client.post(f"/growth/api/alerts/{alert_id}/выдумка").status_code == 404


# ---------------------------------------------------------------------------
# Реклама
# ---------------------------------------------------------------------------


class TestAds:
    def _with_breakdown(self, session, project_id):
        from app.models import DeepDiagnosticsCache

        session.add(DeepDiagnosticsCache(
            project_id=project_id, period_key="payment_path_7d", ok=True,
            trigger_reason="test",
            result_json={
                "registrations": 12, "payment_success": 0,
                "source_breakdown": {
                    "yandex_direct": {"registrations": 9, "channels_created": 6,
                                      "pricing_viewed": 8, "payment_started": 3,
                                      "payment_success": 0},
                    "telegram_ads": {"registrations": 3, "channels_created": 2,
                                     "pricing_viewed": 3, "payment_started": 0,
                                     "payment_success": 0},
                },
            },
        ))
        session.commit()

    def test_cpa_divides_by_direct_registrations_only(self, monkeypatch, tmp_path):
        """Регрессия: расход Директа делился на ВСЕ регистрации, включая
        пришедшие бесплатно -- цена регистрации выходила заниженной.
        3600 / 9 = 400, а не 3600 / 12 = 300."""
        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            _snapshot(session, pid)
            self._with_breakdown(session, pid)
        _login(client)
        totals = client.get("/growth/api/ads").json()["totals"]
        assert totals["cpa"] == 400
        assert "Директ" in totals["cpa_basis"]

    def test_spend_not_attributed_to_other_sources(self, monkeypatch, tmp_path):
        """Прочерк в расходе у telegram_ads -- это «мы не знаем», а не ноль."""
        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            _snapshot(session, pid)
            self._with_breakdown(session, pid)
        _login(client)
        rows = {r["source"]: r for r in client.get("/growth/api/ads").json()["by_source"]}
        assert rows["yandex_direct"]["spend"] == 3600
        assert rows["telegram_ads"]["spend"] is None
        assert rows["telegram_ads"]["cpa"] is None

    def test_optional_source_statuses_present(self, monkeypatch, tmp_path):
        """Интерфейс объясняет необязательность источников по этим статусам."""
        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            _snapshot(session, pid)
        _login(client)
        sources = client.get("/growth/api/ads").json()["sources"]
        assert set(sources) == {"direct", "metrika", "yookassa"}


# ---------------------------------------------------------------------------
# Этапы воронки
# ---------------------------------------------------------------------------


class TestStages:
    def test_unknown_steps_are_numbered_not_invented(self, monkeypatch, tmp_path):
        """Ядро не знает продуктовой специфики: activation_1 у чужого
        проекта -- «Этап 1», а не «Канал создан»."""
        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        _login(client)
        stages = {s["key"]: s for s in client.get(f"/growth/api/projects/{pid}/stages").json()["stages"]}
        assert stages["activation_1"]["title"] == "Этап 1"
        assert stages["activation_1"]["is_custom"] is False
        assert stages["signup"]["title"] == "Регистрация"

    def test_rename_applies_everywhere(self, monkeypatch, tmp_path):
        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            _snapshot(session, pid)
        _login(client)
        resp = client.put(f"/growth/api/projects/{pid}/stages",
                          json={"titles": {"activation_1": "Создал канал"}})
        assert resp.status_code == 200
        assert client.get("/growth/api/funnel").json()["stage_titles"]["activation_1"] == "Создал канал"

    def test_autoname_requires_llm(self, monkeypatch, tmp_path):
        """Без настроенного LLM -- честный отказ, а не выдуманные названия."""
        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        _login(client)
        assert client.post(f"/growth/api/projects/{pid}/stages/autoname").status_code == 503


# ---------------------------------------------------------------------------
# Цикл роста и история
# ---------------------------------------------------------------------------


class TestGrowth:
    def _recommendation(self, session, project_id):
        from app.models import GrowthRecommendation

        rec = GrowthRecommendation(
            project_id=project_id, area="first_post", title="Чиним первый пост",
            action="Одна итерация промпта", hypothesis="Плохой пост убивает интерес",
            evidence_json=["bad 3 из 6"], locked_variables_json=["цены"],
            primary_metric="first_post_feedback_good", sample_metric="registrations",
        )
        session.add(rec)
        session.commit()
        session.refresh(rec)
        return rec.id

    def test_recommendation_exposed_with_locked_variables(self, monkeypatch, tmp_path):
        """«Что не менять» -- часть предложения: без этого владелец сломает
        собственную проверку."""
        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            self._recommendation(session, pid)
        _login(client)
        rec = client.get("/growth/api/growth").json()["recommendation"]
        assert rec["title"] == "Чиним первый пост"
        assert rec["locked_variables"] == ["цены"]

    def test_accept_starts_experiment(self, monkeypatch, tmp_path):
        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            rec_id = self._recommendation(session, pid)
        _login(client)
        assert client.post(f"/growth/api/growth/recommendation/{rec_id}/accept", json={}).json()["ok"]
        state = client.get("/growth/api/growth").json()
        assert state["recommendation"] is None
        assert state["experiment"]["title"] == "Чиним первый пост"

    def test_reject_leaves_no_active_recommendation(self, monkeypatch, tmp_path):
        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            rec_id = self._recommendation(session, pid)
        _login(client)
        client.post(f"/growth/api/growth/recommendation/{rec_id}/reject",
                    json={"reason": "не сейчас"})
        assert client.get("/growth/api/growth").json()["recommendation"] is None

    def test_unknown_action_is_404(self, monkeypatch, tmp_path):
        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            rec_id = self._recommendation(session, pid)
        _login(client)
        assert client.post(f"/growth/api/growth/recommendation/{rec_id}/удалить",
                           json={}).status_code == 404

    def test_history_shows_decision_and_verdict(self, monkeypatch, tmp_path):
        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            rec_id = self._recommendation(session, pid)
        _login(client)
        client.post(f"/growth/api/growth/recommendation/{rec_id}/accept", json={})
        items = client.get("/growth/api/history").json()["items"]
        assert items[0]["status"] == "accepted"
        assert items[0]["experiment"]["status"] == "running"


# ---------------------------------------------------------------------------
# Отчёты, лента, задачи в разработку
# ---------------------------------------------------------------------------


class TestReports:
    @pytest.mark.parametrize("kind", ["board", "funnel", "pay", "ads", "checks",
                                      "journeys", "experiments"])
    def test_every_report_renders(self, monkeypatch, tmp_path, kind):
        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            _snapshot(session, pid)
        _login(client)
        resp = client.get(f"/growth/api/reports/{kind}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["text"].strip()

    def test_unknown_report_is_404(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        assert client.get("/growth/api/reports/выдумка").status_code == 404


class TestLiveFeed:
    def test_reports_source_problem_instead_of_pretending_empty(self, monkeypatch, tmp_path):
        """Лента без связи с продуктом обязана отличаться от «событий не было»."""
        client, _ = _client(monkeypatch, tmp_path, with_project=False)
        _login(client)
        # проект создаётся вручную, чтобы не было ни base_url, ни токена
        from app.models import Project

        import app.db as db_module
        with db_module.get_session() as session:
            session.add(Project(name="Пустой", type="web_app", is_active=True))
            session.commit()
        body = client.get("/growth/api/live").json()
        assert body["ok"] is False
        assert body["events"] == []
        assert body["hint"]


class TestDevTask:
    def test_requires_configuration(self, monkeypatch, tmp_path):
        """Без репозитория и токена -- честный 503 с указанием, что задать."""
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        resp = client.post("/growth/api/dev-task", json={"title": "t", "body": "b"})
        assert resp.status_code == 503
        assert "GITHUB_TASK_TOKEN" in resp.json()["detail"]
