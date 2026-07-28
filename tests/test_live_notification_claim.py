"""
Живые уведомления: одно событие — одно сообщение (задача C7).

27.07.2026 владелец получил ДВА одинаковых утренних письма. Причина —
«проверил `was_notified`, отправил, записал»: между проверкой и записью
проходит запрос в Telegram, и второй процесс (два воркера, наложение
деплоя) успевает проскочить проверку. Для сводки это закрыли заявкой,
а живая лента, пути пользователей и дельты остались со старым окном.

Здесь проверяется, что окна больше нет: под гонкой уходит ровно одно
сообщение, а неудачная отправка не хоронит уведомление навсегда.
"""

import asyncio

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app import scheduler
from app.models import NotificationClaim, NotificationLog, Project


class _Settings:
    admin_chat_ids_list = ["42"]
    bot_token = "1:x"
    quiet_hours_enabled = False


@pytest.fixture()
def factory(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    f = lambda: Session(engine)  # noqa: E731
    monkeypatch.setattr(scheduler, "get_session", f)
    return f


@pytest.fixture()
def project(factory):
    with factory() as session:
        p = Project(name="Проект", type="telegram_saas", is_active=True,
                    settings_json={"notify_chat_ids": ["7"]})
        session.add(p)
        session.commit()
        session.refresh(p)
        return p


class TestNotifyOnce:
    def test_sends_and_records(self, factory, project, monkeypatch):
        sent = []

        async def fake_send(settings, text, chat_ids=None):
            sent.append(text)
            return True

        monkeypatch.setattr(scheduler, "_send_telegram_notification", fake_send)
        ok = asyncio.run(scheduler._notify_once(
            project, _Settings(), "ev:1", "user_registered", "Регистрация"))

        assert ok is True and sent == ["Регистрация"]
        with factory() as session:
            assert len(session.exec(select(NotificationLog)).all()) == 1

    def test_second_call_is_silent(self, factory, project, monkeypatch):
        sent = []

        async def fake_send(settings, text, chat_ids=None):
            sent.append(text)
            return True

        monkeypatch.setattr(scheduler, "_send_telegram_notification", fake_send)
        for _ in range(2):
            asyncio.run(scheduler._notify_once(
                project, _Settings(), "ev:1", "user_registered", "Регистрация"))
        assert len(sent) == 1

    def test_race_sends_exactly_one(self, factory, project, monkeypatch):
        """Два процесса одновременно -- одно сообщение. Пауза внутри
        отправки воспроизводит то самое окно, в которое проскакивал дубль."""
        sent = []

        async def slow_send(settings, text, chat_ids=None):
            await asyncio.sleep(0.05)
            sent.append(text)
            return True

        monkeypatch.setattr(scheduler, "_send_telegram_notification", slow_send)

        async def race():
            await asyncio.gather(
                scheduler._notify_once(project, _Settings(), "ev:1", "t", "Событие"),
                scheduler._notify_once(project, _Settings(), "ev:1", "t", "Событие"),
            )

        asyncio.run(race())
        assert len(sent) == 1, f"отправлено сообщений: {len(sent)}"

    def test_failed_send_can_be_retried(self, factory, project, monkeypatch):
        """Отправка упала -- заявка снимается, иначе уведомление не уйдёт
        уже никогда."""
        attempts = []

        async def failing_send(settings, text, chat_ids=None):
            attempts.append(text)
            return False

        monkeypatch.setattr(scheduler, "_send_telegram_notification", failing_send)
        assert asyncio.run(scheduler._notify_once(
            project, _Settings(), "ev:1", "t", "Событие")) is False

        with factory() as session:
            assert session.exec(select(NotificationClaim)).all() == []
            assert session.exec(select(NotificationLog)).all() == []

        async def ok_send(settings, text, chat_ids=None):
            attempts.append(text)
            return True

        monkeypatch.setattr(scheduler, "_send_telegram_notification", ok_send)
        assert asyncio.run(scheduler._notify_once(
            project, _Settings(), "ev:1", "t", "Событие")) is True
        assert len(attempts) == 2

    def test_no_recipients_means_no_claim_left_behind(self, factory, monkeypatch):
        """Слать некому -- заявка не должна остаться и заблокировать
        отправку навсегда, когда адресата наконец укажут."""
        from app import accounts

        with factory() as session:
            p = Project(name="Клиентский", type="telegram_saas", is_active=True, settings_json={})
            session.add(p)
            session.commit()
            session.refresh(p)
            project_id = p.id
            accounts.create_user(session, "boss@example.com", "qwerty12")
            client_user = accounts.create_user(session, "client@example.com", "qwerty12")
            accounts.grant_project(session, project_id, client_user.id)

        class _P:
            id = project_id

        sent = []

        async def fake_send(settings, text, chat_ids=None):
            sent.append(chat_ids)
            return True

        monkeypatch.setattr(scheduler, "_send_telegram_notification", fake_send)
        assert asyncio.run(scheduler._notify_once(_P(), _Settings(), "ev:1", "t", "Событие")) is False
        assert sent == []
        with factory() as session:
            assert session.exec(select(NotificationClaim)).all() == []


class TestClaimsAreCleanedUp:
    def test_old_claims_are_deleted_by_retention(self, factory, project):
        """Заявок создаётся столько же, сколько уведомлений. Без ретенции
        таблица росла бы вечно -- в списке очистки её не было."""
        from datetime import timedelta

        from app.service import cleanup_old_data, utcnow

        with factory() as session:
            old = NotificationClaim(project_id=project.id, event_key="старая",
                                    claimed_at=utcnow() - timedelta(days=60))
            fresh = NotificationClaim(project_id=project.id, event_key="свежая")
            session.add(old)
            session.add(fresh)
            session.commit()

            deleted = cleanup_old_data(session)
            assert deleted["NotificationClaim"] == 1
            left = session.exec(select(NotificationClaim.event_key)).all()
            assert list(left) == ["свежая"]
