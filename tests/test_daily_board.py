"""
Тесты ежедневной утренней сводки (daily board):
1. sparkline: пусто / одна точка / рост / None-разрывы / равные значения
2. build_dynamics_block: <2 точек, стрелки роста/падения, пропуск пустых метрик
3. build_daily_board_message: доска + динамика вместе
4. save_daily_counters: идемпотентность на день, feedback_total = good + bad
5. load_daily_counters_history: порядок от старых к новым, окно days
6. send_daily_board: дедуп по дню, force, mark только после успешной отправки,
   выключенный флаг, отсутствие конфигурации
"""
import asyncio
from datetime import date

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.commercial_report import (
    build_daily_board_message,
    build_dynamics_block,
    sparkline,
)
from app.models import Project
from app.service import (
    DAILY_COUNTERS_KEY_PREFIX,
    get_cached_diagnostics,
    load_daily_counters_history,
    save_daily_counters,
    was_notified,
)


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------

def _make_session_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)

    def factory():
        return Session(engine)

    return factory


def _make_project(session: Session) -> Project:
    project = Project(name="TruePost", type="telegram_saas", is_active=True)
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


class _FakeSettings:
    daily_board_enabled = True
    bot_token = "fake-token"
    admin_chat_ids_list = ["123"]


# ---------------------------------------------------------------------------
# sparkline
# ---------------------------------------------------------------------------

class TestSparkline:

    def test_empty(self):
        assert sparkline([]) == ""
        assert sparkline([None, None]) == ""

    def test_growth_is_monotonic(self):
        s = sparkline([1, 2, 3, 4])
        assert len(s) == 4
        # символы должны идти по возрастанию высоты
        order = "▁▂▃▄▅▆▇█"
        idxs = [order.index(c) for c in s]
        assert idxs == sorted(idxs)
        assert idxs[0] < idxs[-1]

    def test_all_equal_not_empty(self):
        s = sparkline([5, 5, 5])
        assert len(s) == 3
        assert s[0] == s[1] == s[2]
        assert s[0] != " "

    def test_none_becomes_space(self):
        s = sparkline([1, None, 3])
        assert len(s) == 3
        assert s[1] == " "
        assert s[0] != " " and s[2] != " "


# ---------------------------------------------------------------------------
# build_dynamics_block
# ---------------------------------------------------------------------------

class TestDynamicsBlock:

    def test_less_than_two_points(self):
        text = build_dynamics_block([])
        assert "после 2 дней" in text
        text = build_dynamics_block([{"registrations": 5}])
        assert "после 2 дней" in text

    def test_growth_arrow(self):
        history = [
            {"registrations": 3, "feedback_total": 1, "pricing_viewed": 0, "payment_success": 0},
            {"registrations": 8, "feedback_total": 1, "pricing_viewed": 0, "payment_success": 0},
        ]
        text = build_dynamics_block(history)
        assert "Регистрации" in text
        assert "3→8 ↗" in text

    def test_decline_and_flat_arrows(self):
        history = [
            {"registrations": 8, "feedback_total": 2, "pricing_viewed": 1, "payment_success": 0},
            {"registrations": 5, "feedback_total": 2, "pricing_viewed": 1, "payment_success": 0},
        ]
        text = build_dynamics_block(history)
        assert "8→5 ↘" in text
        assert "2→2 →" in text

    def test_metric_with_no_data_skipped(self):
        history = [
            {"registrations": 1, "feedback_total": None, "pricing_viewed": None, "payment_success": None},
            {"registrations": 2, "feedback_total": None, "pricing_viewed": None, "payment_success": None},
        ]
        text = build_dynamics_block(history)
        assert "Регистрации" in text
        assert "Отзывы" not in text
        assert "Тарифы" not in text


# ---------------------------------------------------------------------------
# build_daily_board_message
# ---------------------------------------------------------------------------

class TestDailyBoardMessage:

    def test_contains_board_and_dynamics(self):
        history = [
            {"registrations": 1, "feedback_total": 0, "pricing_viewed": 0, "payment_success": 0},
            {"registrations": 2, "feedback_total": 0, "pricing_viewed": 0, "payment_success": 0},
        ]
        text = build_daily_board_message("ДОСКА-ТЕКСТ", history)
        assert "Ежедневная сводка" in text
        assert "ДОСКА-ТЕКСТ" in text
        assert "ДИНАМИКА" in text
        # доска раньше динамики
        assert text.index("ДОСКА-ТЕКСТ") < text.index("ДИНАМИКА")


# ---------------------------------------------------------------------------
# save_daily_counters / load_daily_counters_history
# ---------------------------------------------------------------------------

class TestDailyCounters:

    def test_save_and_load_roundtrip(self):
        factory = _make_session_factory()
        with factory() as session:
            project = _make_project(session)
            pp = {
                "registrations": 7,
                "first_post_feedback_good": 2,
                "first_post_feedback_bad": 1,
                "pricing_viewed": 3,
                "payment_success": 0,
            }
            save_daily_counters(session, project.id, pp, "2026-07-02")
            history = load_daily_counters_history(
                session, project.id, days=7, today=date(2026, 7, 2)
            )
            assert len(history) == 1
            point = history[0]
            assert point["registrations"] == 7
            assert point["feedback_total"] == 3  # good + bad
            assert point["pricing_viewed"] == 3
            assert point["date"] == "07-02"

    def test_idempotent_per_day(self):
        factory = _make_session_factory()
        with factory() as session:
            project = _make_project(session)
            save_daily_counters(session, project.id, {"registrations": 5}, "2026-07-02")
            save_daily_counters(session, project.id, {"registrations": 99}, "2026-07-02")
            history = load_daily_counters_history(
                session, project.id, days=7, today=date(2026, 7, 2)
            )
            assert len(history) == 1
            assert history[0]["registrations"] == 5  # вторая запись не перезаписала

    def test_history_ordered_old_to_new_and_windowed(self):
        factory = _make_session_factory()
        with factory() as session:
            project = _make_project(session)
            save_daily_counters(session, project.id, {"registrations": 1}, "2026-06-20")  # вне окна 7д
            save_daily_counters(session, project.id, {"registrations": 2}, "2026-06-30")
            save_daily_counters(session, project.id, {"registrations": 3}, "2026-07-02")
            history = load_daily_counters_history(
                session, project.id, days=7, today=date(2026, 7, 2)
            )
            assert [h["registrations"] for h in history] == [2, 3]

    def test_none_payment_path(self):
        factory = _make_session_factory()
        with factory() as session:
            project = _make_project(session)
            save_daily_counters(session, project.id, None, "2026-07-02")
            history = load_daily_counters_history(
                session, project.id, days=7, today=date(2026, 7, 2)
            )
            assert len(history) == 1
            assert history[0]["registrations"] is None
            assert history[0]["feedback_total"] is None


# ---------------------------------------------------------------------------
# send_daily_board
# ---------------------------------------------------------------------------

class TestSendDailyBoard:

    def _run(self, coro):
        return asyncio.run(coro)

    def test_sends_and_dedups_same_day(self):
        from app.scheduler import send_daily_board

        factory = _make_session_factory()
        with factory() as session:
            _make_project(session)

        sent_texts = []

        async def fake_send(settings, text):
            sent_texts.append(text)
            return True

        ok1 = self._run(send_daily_board(
            _send=fake_send, _session_factory=factory, _settings=_FakeSettings()))
        ok2 = self._run(send_daily_board(
            _send=fake_send, _session_factory=factory, _settings=_FakeSettings()))

        assert ok1 is True
        assert ok2 is False, "второй вызов в тот же день должен быть съеден дедупом"
        assert len(sent_texts) == 1
        assert "Ежедневная сводка" in sent_texts[0]
        assert "ДИНАМИКА" in sent_texts[0]

    def test_force_bypasses_dedup(self):
        from app.scheduler import send_daily_board

        factory = _make_session_factory()
        with factory() as session:
            _make_project(session)

        sent = []

        async def fake_send(settings, text):
            sent.append(text)
            return True

        self._run(send_daily_board(_send=fake_send, _session_factory=factory, _settings=_FakeSettings()))
        ok = self._run(send_daily_board(
            force=True, _send=fake_send, _session_factory=factory, _settings=_FakeSettings()))
        assert ok is True
        assert len(sent) == 2

    def test_failed_send_not_marked(self):
        from app.scheduler import send_daily_board

        factory = _make_session_factory()
        with factory() as session:
            project = _make_project(session)
            project_id = project.id

        async def failing_send(settings, text):
            return False

        ok = self._run(send_daily_board(
            _send=failing_send, _session_factory=factory, _settings=_FakeSettings()))
        assert ok is False

        # Дедуп-записи нет -- следующий цикл попробует снова
        day = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).date().isoformat()
        with factory() as session:
            assert was_notified(session, project_id, f"daily_board:{day}") is False

        # Теперь успешная отправка проходит
        async def ok_send(settings, text):
            return True

        ok = self._run(send_daily_board(
            _send=ok_send, _session_factory=factory, _settings=_FakeSettings()))
        assert ok is True

    def test_disabled_flag(self):
        from app.scheduler import send_daily_board

        class Disabled(_FakeSettings):
            daily_board_enabled = False

        called = []

        async def fake_send(settings, text):
            called.append(text)
            return True

        factory = _make_session_factory()
        ok = self._run(send_daily_board(
            _send=fake_send, _session_factory=factory, _settings=Disabled()))
        assert ok is False and not called

    def test_no_admin_chats(self):
        from app.scheduler import send_daily_board

        class NoAdmins(_FakeSettings):
            admin_chat_ids_list = []

        factory = _make_session_factory()
        ok = self._run(send_daily_board(
            _send=None, _session_factory=factory, _settings=NoAdmins()))
        assert ok is False

    def test_no_active_project(self):
        from app.scheduler import send_daily_board

        factory = _make_session_factory()  # проект не создаём

        async def fake_send(settings, text):
            return True

        ok = self._run(send_daily_board(
            _send=fake_send, _session_factory=factory, _settings=_FakeSettings()))
        assert ok is False

    def test_daily_point_saved_even_without_payment_path_cache(self):
        from app.scheduler import send_daily_board

        factory = _make_session_factory()
        with factory() as session:
            project = _make_project(session)
            project_id = project.id

        async def fake_send(settings, text):
            return True

        self._run(send_daily_board(
            _send=fake_send, _session_factory=factory, _settings=_FakeSettings()))

        day = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).date().isoformat()
        with factory() as session:
            cached = get_cached_diagnostics(
                session, project_id, f"{DAILY_COUNTERS_KEY_PREFIX}{day}")
            assert cached is not None, "точка динамики должна сохраняться каждый день"
