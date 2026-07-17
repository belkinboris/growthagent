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


# ---------------------------------------------------------------------------
# Мост «очередь на неделю» в /funnel (queue_offer_shown/clicked)
# ---------------------------------------------------------------------------

class TestQueueOfferInFunnel:

    def _signals(self, **pp):
        from app.commercial_report import _format_new_product_signals as build_new_signals_block
        return build_new_signals_block(pp)

    def test_queue_counts_rendered(self):
        text = self._signals(first_post_feedback_good=4, first_post_feedback_bad=9,
                             queue_offer_shown=3, queue_offer_clicked=1)
        assert "Мост «очередь на неделю»: показан 3, кликнули 1" in text

    def test_warns_when_good_but_no_shows(self):
        text = self._signals(first_post_feedback_good=4, queue_offer_shown=0, queue_offer_clicked=0)
        assert "проверить кэш фронтенда" in text

    def test_no_warn_when_shows_present(self):
        text = self._signals(first_post_feedback_good=4, queue_offer_shown=2, queue_offer_clicked=0)
        assert "проверить кэш" not in text

    def test_absent_fields_no_queue_line(self):
        text = self._signals(first_post_feedback_good=4, first_post_feedback_bad=9)
        assert "Мост" not in text

    def test_connector_accepts_fields(self):
        from app.connectors.payment_path import _EXPECTED_FIELDS
        assert "queue_offer_shown" in _EXPECTED_FIELDS
        assert "queue_offer_clicked" in _EXPECTED_FIELDS


# ---------------------------------------------------------------------------
# Утренний рассказ и один мозг на доске
# ---------------------------------------------------------------------------

class TestMorningStory:

    def test_story_with_deltas_and_action(self):
        from app.commercial_report import build_morning_story
        history = [
            {"registrations": 16, "feedback_total": 12, "pricing_viewed": 5, "payment_success": 0},
            {"registrations": 19, "feedback_total": 14, "pricing_viewed": 6, "payment_success": 0},
        ]
        text = build_morning_story(history, "Эксперимент «X»: 2/10.", "ничего — копим данные.")
        assert "За сутки: +3 регистрации, +2 отзыва, +1 открытия тарифов." in text
        assert "Эксперимент «X»" in text
        assert "От тебя сегодня:</b> ничего" in text

    def test_story_quiet_day(self):
        from app.commercial_report import build_morning_story
        history = [
            {"registrations": 16, "feedback_total": 12, "pricing_viewed": 5, "payment_success": 0},
            {"registrations": 16, "feedback_total": 12, "pricing_viewed": 5, "payment_success": 0},
        ]
        text = build_morning_story(history, None, "ничего.")
        assert "новых событий не было" in text
        assert "это нормально" in text

    def test_experiment_one_liner_honest_on_small_n(self):
        from app.commercial_report import experiment_one_liner
        f = _make_session_factory()
        from app.models import GrowthExperiment
        exp = GrowthExperiment(
            project_id=1, recommendation_id=1, title="Чиним качество первого поста",
            area="first_post", primary_metric="first_post_feedback_good",
            sample_metric="first_post_feedback_total", target_sample=10,
        )
        line = experiment_one_liner(exp, {"current_sample": 2, "delta_metric": 1,
                                          "baseline_rate": 0.25, "current_rate": 0.5})
        assert "2/10" in line and "25% → 50%" in line
        assert "единичные события" in line

    def test_board_skip_decision_removes_legacy_brain(self):
        from app.commercial_report import build_board_report
        pp = dict(registrations=16, first_post_feedback_good=4, first_post_feedback_bad=9,
                  pricing_viewed=6, payment_started=0, payment_success=0)
        full = build_board_report("TruePost", None, payment_path=pp,
                                  new_registrations_since_deploy=16)
        compact = build_board_report("TruePost", None, payment_path=pp,
                                     new_registrations_since_deploy=16, skip_decision=True)
        assert "РЕШЕНИЕ" in full and "СЕГОДНЯ" in full
        assert "РЕШЕНИЕ" not in compact and "СЕГОДНЯ" not in compact and "ФОКУС" not in compact
        assert "НЕДЕЛЯ" in compact and "НЕ МЕНЯТЬ" in compact


class TestHtmlStyling:

    def test_board_has_bold_and_emoji(self):
        from app.commercial_report import build_board_report
        pp = dict(registrations=16, first_post_feedback_good=4, first_post_feedback_bad=9,
                  pricing_viewed=6, payment_started=0, payment_success=0)
        text = build_board_report("АвтоПост", None, payment_path=pp, new_registrations_since_deploy=16)
        assert "📊 <b>Доска — АвтоПост</b>" in text
        assert "<b>НЕДЕЛЯ</b>" in text
        # Незакрытых тегов нет
        assert text.count("<b>") == text.count("</b>")

    def test_gl_blocks_styled_and_balanced(self):
        from app.commercial_report import build_recommendation_block, build_verdict_block
        from app.models import GrowthRecommendation, GrowthExperiment
        rec = GrowthRecommendation(project_id=1, area="x", title="Т", action="Д",
                                   evidence_json=["e"], locked_variables_json=["цены"])
        t1 = build_recommendation_block(rec)
        assert "💡 <b>ПРЕДЛАГАЮ ЭКСПЕРИМЕНТ</b>" in t1
        exp = GrowthExperiment(project_id=1, recommendation_id=1, title="Т", area="x",
                               verdict="ЭКСПЕРИМЕНТ ВЫИГРАЛ", result_summary="итог")
        t2 = build_verdict_block(exp)
        assert "⚖️ <b>ВЕРДИКТ</b>" in t2
        for t in (t1, t2):
            assert t.count("<b>") == t.count("</b>")

    def test_strip_html_tags(self):
        from app.telegram_bot import _strip_html_tags
        assert _strip_html_tags("☀️ <b>Доброе утро.</b> <i>x</i>") == "☀️ Доброе утро. x"


class TestQuietHours:

    class _S:
        quiet_hours_enabled = True
        quiet_hours_start_utc = 20
        quiet_hours_end_utc = 5

    def test_quiet_at_night_msk(self):
        from app.scheduler import is_quiet_hour
        s = self._S()
        # 03:55 МСК = 00:55 UTC — тихо (тот самый ночной отчёт)
        assert is_quiet_hour(s, now_utc_hour=0) is True
        assert is_quiet_hour(s, now_utc_hour=23) is True
        assert is_quiet_hour(s, now_utc_hour=4) is True

    def test_loud_at_day(self):
        from app.scheduler import is_quiet_hour
        s = self._S()
        assert is_quiet_hour(s, now_utc_hour=6) is False   # 09:00 МСК — сводка
        assert is_quiet_hour(s, now_utc_hour=12) is False
        assert is_quiet_hour(s, now_utc_hour=19) is False

    def test_disabled(self):
        from app.scheduler import is_quiet_hour
        class Off(self._S):
            quiet_hours_enabled = False
        assert is_quiet_hour(Off(), now_utc_hour=0) is False

    def test_non_wrapping_interval(self):
        from app.scheduler import is_quiet_hour
        class Day(self._S):
            quiet_hours_start_utc = 2
            quiet_hours_end_utc = 6
        assert is_quiet_hour(Day(), now_utc_hour=3) is True
        assert is_quiet_hour(Day(), now_utc_hour=7) is False

    def test_sender_suppresses_and_does_not_mark(self):
        """Подавленный ночью пуш не помечается отправленным — дошлётся утром."""
        import asyncio
        from app.scheduler import send_daily_board
        factory = _make_session_factory()
        with factory() as session:
            _make_project(session)

        sent = []

        async def fake_send(settings, text):
            # реальный _send_telegram_notification при тихих часах вернул бы False;
            # здесь моделируем его контрактом
            return False

        class S(_FakeSettings):
            pass

        ok = asyncio.run(send_daily_board(_send=fake_send, _session_factory=factory, _settings=S()))
        assert ok is False
        # Повторная попытка с успешной отправкой проходит (дедуп не сработал)
        async def ok_send(settings, text):
            sent.append(text)
            return True
        ok = asyncio.run(send_daily_board(_send=ok_send, _session_factory=factory, _settings=S()))
        assert ok is True and len(sent) == 1
