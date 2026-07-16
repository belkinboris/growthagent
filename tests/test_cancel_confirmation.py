"""
Двухшаговое подтверждение отмены эксперимента (урок 2026-07-14): один тап
на «Отменить эксперимент» на /board стирал неделю прогресса без возможности
передумать. Теперь первый тап только предлагает подтвердить.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from sqlmodel import Session, SQLModel, create_engine

from app.growth_loop import accept_recommendation, propose_if_needed
from app.models import GrowthExperimentStatus, Project
from app.telegram_bot import _handle_growth_loop_button
from app.truepost_playbook import truepost_playbook


def _factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return lambda: Session(engine)


def _running_experiment(factory):
    pp = dict(registrations=20, channels_created=16, first_post_feedback_good=3,
              first_post_feedback_bad=9, pricing_viewed=6, payment_started=0, payment_success=0)
    with factory() as s:
        p = Project(name="TruePost", type="t", is_active=True)
        s.add(p); s.commit(); s.refresh(p)
        rec = propose_if_needed(s, p.id, pp, truepost_playbook)
        exp = accept_recommendation(s, rec, pp)
        return exp.id


def _fake_query():
    q = MagicMock()
    q.edit_message_text = AsyncMock()
    q.edit_message_reply_markup = AsyncMock()
    q.message = MagicMock()
    q.message.text = "какой-то текст доски"
    return q


class TestCancelConfirmation:

    def test_first_tap_only_asks_does_not_cancel(self, monkeypatch):
        factory = _factory()
        exp_id = _running_experiment(factory)
        monkeypatch.setattr("app.telegram_bot.get_session", factory)

        query = _fake_query()
        handled = asyncio.run(_handle_growth_loop_button(query, "gl_cancel_ask", exp_id))

        assert handled is True
        query.edit_message_reply_markup.assert_called_once()   # переспросил
        query.edit_message_text.assert_not_called()             # НЕ отменил

        with factory() as s:
            from app.models import GrowthExperiment
            exp = s.get(GrowthExperiment, exp_id)
            assert exp.status == GrowthExperimentStatus.running   # прогресс цел

    def test_confirm_actually_cancels(self, monkeypatch):
        factory = _factory()
        exp_id = _running_experiment(factory)
        monkeypatch.setattr("app.telegram_bot.get_session", factory)

        query = _fake_query()
        asyncio.run(_handle_growth_loop_button(query, "gl_cancel_ask", exp_id))
        handled = asyncio.run(_handle_growth_loop_button(query, "gl_cancel_do", exp_id))

        assert handled is True
        with factory() as s:
            from app.models import GrowthExperiment
            exp = s.get(GrowthExperiment, exp_id)
            assert exp.status == GrowthExperimentStatus.cancelled

    def test_no_keeps_experiment_running(self, monkeypatch):
        factory = _factory()
        exp_id = _running_experiment(factory)
        monkeypatch.setattr("app.telegram_bot.get_session", factory)

        query = _fake_query()
        asyncio.run(_handle_growth_loop_button(query, "gl_cancel_ask", exp_id))
        handled = asyncio.run(_handle_growth_loop_button(query, "gl_cancel_no", exp_id))

        assert handled is True
        with factory() as s:
            from app.models import GrowthExperiment
            exp = s.get(GrowthExperiment, exp_id)
            assert exp.status == GrowthExperimentStatus.running

    def test_missing_experiment_handled_gracefully(self, monkeypatch):
        factory = _factory()
        with factory() as s:
            p = Project(name="T", type="t", is_active=True)
            s.add(p); s.commit()
        monkeypatch.setattr("app.telegram_bot.get_session", factory)

        query = _fake_query()
        handled = asyncio.run(_handle_growth_loop_button(query, "gl_cancel_do", 9999))
        assert handled is True
        query.edit_message_text.assert_called_once()
        assert "не найден" in query.edit_message_text.call_args[0][0].lower()
