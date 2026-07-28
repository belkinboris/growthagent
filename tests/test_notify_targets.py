"""
Кому уходят уведомления по проекту (задача B8).

Дефект, который здесь закрывается: адресат был один на всю платформу —
`BOT_ADMIN_CHAT_IDS` из окружения. С появлением аккаунтов это означало бы,
что утренняя сводка по проекту клиента уходит владельцу платформы. Это не
неудобство, а утечка: данные клиента у постороннего человека.

Правило, которое проверяется: свой список получателей у проекта важнее
всего; переменная окружения годится только для того, кто ставил платформу;
во всех остальных случаях не шлём никуда и говорим об этом словами.
"""

import asyncio

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app import accounts, scheduler
from app.models import Project
from app.notify_targets import project_chat_ids


class _Settings:
    admin_chat_ids_list = ["42"]
    daily_board_enabled = True
    bot_token = "1:x"
    quiet_hours_enabled = False


@pytest.fixture()
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return lambda: Session(engine)


def _project(session, name="Проект", chat_ids=None, active=True) -> Project:
    settings_json = {}
    if chat_ids is not None:
        settings_json["notify_chat_ids"] = chat_ids
    p = Project(name=name, type="telegram_saas", is_active=active, settings_json=settings_json)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


class TestRecipients:
    def test_own_list_wins(self, factory):
        with factory() as session:
            project = _project(session, chat_ids=["777"])
            ids, reason = project_chat_ids(session, project, _Settings())
        assert ids == ["777"] and reason is None

    def test_ownerless_project_falls_back_to_env(self, factory):
        """Проект, который никто не забрал, -- проект того, кто ставил
        платформу: слать в её переменную окружения честно."""
        with factory() as session:
            project = _project(session)
            ids, reason = project_chat_ids(session, project, _Settings())
        assert ids == ["42"] and reason is None

    def test_installer_project_falls_back_to_env(self, factory):
        with factory() as session:
            installer = accounts.create_user(session, "boss@example.com", "qwerty12")
            project = _project(session)
            accounts.grant_project(session, project.id, installer.id)
            ids, reason = project_chat_ids(session, project, _Settings())
        assert ids == ["42"] and reason is None

    def test_client_project_never_uses_env(self, factory):
        """Главная проверка: сводка клиента не должна уйти владельцу
        платформы, чей chat id лежит в переменной окружения."""
        with factory() as session:
            accounts.create_user(session, "boss@example.com", "qwerty12")
            client_user = accounts.create_user(session, "client@example.com", "qwerty12")
            project = _project(session, name="Магазин клиента")
            accounts.grant_project(session, project.id, client_user.id)

            ids, reason = project_chat_ids(session, project, _Settings())
        assert ids == []
        assert "не уходят" in reason

    def test_client_project_with_own_ids_works(self, factory):
        with factory() as session:
            accounts.create_user(session, "boss@example.com", "qwerty12")
            client_user = accounts.create_user(session, "client@example.com", "qwerty12")
            project = _project(session, chat_ids=["900", "901"])
            accounts.grant_project(session, project.id, client_user.id)
            ids, reason = project_chat_ids(session, project, _Settings())
        assert ids == ["900", "901"] and reason is None

    def test_blank_values_are_dropped(self, factory):
        with factory() as session:
            project = _project(session, chat_ids=["", "  ", "5"])
            ids, _ = project_chat_ids(session, project, _Settings())
        assert ids == ["5"]

    def test_no_env_and_no_own_says_what_is_missing(self, factory):
        class NoEnv(_Settings):
            admin_chat_ids_list = []

        with factory() as session:
            project = _project(session)
            ids, reason = project_chat_ids(session, project, NoEnv())
        assert ids == []
        assert "BOT_ADMIN_CHAT_IDS" in reason


class TestLiveAndGrowthLoopUseProjectChannel:
    """B10: живая лента и Growth Loop раньше слали в канал из окружения --
    для проекта клиента это та же утечка, что закрыта в B8 для сводки."""

    def test_live_notification_goes_to_project_channel(self, factory, monkeypatch):
        with factory() as session:
            project = _project(session, chat_ids=["777"])

        monkeypatch.setattr(scheduler, "get_session", factory)
        sent = []

        async def fake_send(settings, text, chat_ids=None):
            sent.append((chat_ids, text))
            return True

        monkeypatch.setattr(scheduler, "_send_telegram_notification", fake_send)
        ok = asyncio.run(scheduler._notify_project(project, _Settings(), "Событие"))
        assert ok is True
        assert sent == [(["777"], "Событие")]

    def test_live_notification_is_silent_without_recipients(self, factory, monkeypatch):
        with factory() as session:
            accounts.create_user(session, "boss@example.com", "qwerty12")
            client_user = accounts.create_user(session, "client@example.com", "qwerty12")
            project = _project(session, name="Магазин клиента")
            project_id = project.id
            accounts.grant_project(session, project_id, client_user.id)

        # grant_project коммитит и «протухает» объект проекта, поэтому
        # передаём в отправку такую же заглушку, как приходит из цикла:
        # внутри всё равно перечитывается свежая запись.
        class _Project:
            id = project_id

        project = _Project()
        monkeypatch.setattr(scheduler, "get_session", factory)
        sent = []

        async def fake_send(settings, text, chat_ids=None):
            sent.append(chat_ids)
            return True

        monkeypatch.setattr(scheduler, "_send_telegram_notification", fake_send)
        ok = asyncio.run(scheduler._notify_project(project, _Settings(), "Событие"))
        assert ok is False
        assert sent == [], "событие клиента ушло в чужой канал"

    def test_growth_loop_verdict_uses_project_channel(self, factory, monkeypatch):
        """Вердикт эксперимента -- такое же уведомление, как остальные."""
        from app import growth_loop
        from app.truepost_playbook import truepost_playbook

        with factory() as session:
            project = _project(session, chat_ids=["555"])
            project_id = project.id

        sent = []

        async def fake_send(settings, text, chat_ids=None):
            sent.append(chat_ids)
            return True

        payment_path = {
            "registrations": 20, "channels_created": 16,
            "first_post_feedback_good": 7, "first_post_feedback_bad": 3,
            "pricing_viewed": 2, "payment_started": 0, "payment_success": 0,
        }

        class FakeProject:
            id = project_id

        result = asyncio.run(scheduler.growth_loop_tick_and_notify(
            FakeProject(), _Settings(), payment_path,
            _send=fake_send, _session_factory=factory))
        if not (result["verdict_sent"] or result["proposal_sent"]):
            pytest.skip("движок в этом состоянии ничего не предлагает")
        assert sent == [["555"]]


class TestDailyBoardPerProject:
    def _run_board(self, factory, sent):
        async def fake_send(settings, text, chat_ids=None):
            sent.append((chat_ids, text))
            return True

        asyncio.run(scheduler.send_daily_board(
            _send=fake_send, _session_factory=factory, _settings=_Settings()))

    def test_every_active_project_gets_its_own_letter(self, factory):
        with factory() as session:
            _project(session, name="Первый", chat_ids=["1"])
            _project(session, name="Второй", chat_ids=["2"])
        sent = []
        self._run_board(factory, sent)
        assert sorted(c for c, _ in sent) == [["1"], ["2"]]

    def test_paused_project_gets_nothing(self, factory):
        with factory() as session:
            _project(session, name="Выключенный", chat_ids=["1"], active=False)
        sent = []
        self._run_board(factory, sent)
        assert sent == []

    def test_client_project_without_recipients_is_silent(self, factory):
        """Не шлём никуда -- лучше, чем отправить чужому."""
        with factory() as session:
            accounts.create_user(session, "boss@example.com", "qwerty12")
            client_user = accounts.create_user(session, "client@example.com", "qwerty12")
            project = _project(session, name="Магазин клиента")
            accounts.grant_project(session, project.id, client_user.id)
        sent = []
        self._run_board(factory, sent)
        assert sent == []

    def test_one_project_does_not_block_another(self, factory):
        with factory() as session:
            broken = _project(session, name="Сломанный", chat_ids=["1"])
            _project(session, name="Живой", chat_ids=["2"])

        sent = []

        async def fake_send(settings, text, chat_ids=None):
            if chat_ids == ["1"]:
                raise RuntimeError("Telegram недоступен для этого адресата")
            sent.append(chat_ids)
            return True

        asyncio.run(scheduler.send_daily_board(
            _send=fake_send, _session_factory=factory, _settings=_Settings()))
        assert sent == [["2"]], f"живой проект остался без сводки (broken={broken.id})"

    def test_still_one_letter_per_day_per_project(self, factory):
        with factory() as session:
            _project(session, name="Первый", chat_ids=["1"])
        sent = []
        self._run_board(factory, sent)
        self._run_board(factory, sent)
        assert len(sent) == 1
