"""Тесты разговорного слоя (app/ask.py)."""
import asyncio

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app import ask
from app.models import Project


def _factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return lambda: Session(engine)


class _Settings:
    anthropic_api_key = "sk-test"
    anthropic_model = "claude-sonnet-4-6"
    llm_provider = "anthropic"


class TestBuildContext:

    def test_context_survives_empty_project(self):
        f = _factory()
        with f() as s:
            p = Project(name="TruePost", type="t", is_active=True)
            s.add(p); s.commit(); s.refresh(p)
            ctx = ask.build_context(s, p)
        assert "Доска" in ctx           # доска строится даже без данных
        assert len(ctx) <= ask.MAX_CONTEXT_CHARS

    def test_context_includes_experiment_and_numbers(self):
        from app.growth_loop import accept_recommendation, propose_if_needed
        from app.truepost_playbook import truepost_playbook
        from app.service import save_diagnostics_cache, PAYMENT_PATH_CACHE_PERIOD_KEY
        f = _factory()
        pp = dict(registrations=20, channels_created=16, first_post_feedback_good=7,
                  first_post_feedback_bad=3, pricing_viewed=2, payment_started=0,
                  payment_success=0, queue_offer_shown=4, queue_offer_clicked=1)
        with f() as s:
            p = Project(name="TruePost", type="t", is_active=True)
            s.add(p); s.commit(); s.refresh(p)
            save_diagnostics_cache(s, p.id, PAYMENT_PATH_CACHE_PERIOD_KEY, "test", pp)
            rec = propose_if_needed(s, p.id, pp, truepost_playbook)
            accept_recommendation(s, rec, pp)
            ctx = ask.build_context(s, p)
        assert "АКТИВНЫЙ ЭКСПЕРИМЕНТ" in ctx
        assert "queue_offer_shown=4" in ctx
        assert "registrations=20" in ctx


class TestAnswerQuestion:

    def _run(self, coro):
        return asyncio.run(coro)

    def test_happy_path_and_context_in_system(self):
        ask._last_call_ts = 0.0
        captured = {}

        async def fake_post(payload, headers):
            captured["payload"] = payload
            return {"content": [{"type": "text", "text": "Ответ по данным."}]}

        out = self._run(ask.answer_question("почему нет оплат?", "КОНТЕКСТ-ДАННЫЕ", _Settings(), _post=fake_post))
        assert out == "Ответ по данным."
        assert "КОНТЕКСТ-ДАННЫЕ" in captured["payload"]["system"]
        assert "Данные важнее мнений" in captured["payload"]["system"]
        assert captured["payload"]["messages"][0]["content"] == "почему нет оплат?"

    def test_none_on_api_failure(self):
        ask._last_call_ts = 0.0

        async def failing_post(payload, headers):
            raise RuntimeError("boom")

        out = self._run(ask.answer_question("вопрос", "ctx", _Settings(), _post=failing_post))
        assert out is None

    def test_cooldown(self):
        ask._last_call_ts = 0.0

        async def fake_post(payload, headers):
            return {"content": [{"type": "text", "text": "ok"}]}

        first = self._run(ask.answer_question("q1", "ctx", _Settings(), _post=fake_post))
        second = self._run(ask.answer_question("q2", "ctx", _Settings(), _post=fake_post))
        assert first == "ok"
        assert "не чаще" in second

    def test_question_truncated(self):
        ask._last_call_ts = 0.0
        captured = {}

        async def fake_post(payload, headers):
            captured["payload"] = payload
            return {"content": [{"type": "text", "text": "ok"}]}

        self._run(ask.answer_question("х" * 5000, "ctx", _Settings(), _post=fake_post))
        assert len(captured["payload"]["messages"][0]["content"]) <= ask.MAX_QUESTION_CHARS


class TestIsConfigured:

    def test_configured(self):
        assert ask.is_configured(_Settings()) is True

    def test_not_configured(self):
        class NoKey:
            anthropic_api_key = None
            llm_provider = "anthropic"
        assert ask.is_configured(NoKey()) is False
        class WrongProvider:
            anthropic_api_key = "sk"
            llm_provider = "none"
        assert ask.is_configured(WrongProvider()) is False
