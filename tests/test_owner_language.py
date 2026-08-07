"""
Два дефекта, найденных владельцем на живом продукте 27.07.2026:

1. Ежедневная сводка приходила ДВАЖДЫ. Причина -- гонка «проверил, потом
   записал»: между `was_notified` и `mark_notified` проходит сборка отчёта
   и запрос в Telegram, а планировщик живёт внутри веб-процесса, поэтому
   при двух воркерах (или наложении деплоя) оба процесса успевали пройти
   проверку. Теперь заявка ставится ДО отправки, атомарно.

2. В тексте для владельца был английский: «bad 3 из 6», «Главная причина:
   wrong_style», «причины bad: {'too_generic': 0, ...}», «onboarding».
   Продукт присылает коды причин по-английски, и они попадали в письмо как
   есть -- вместе с сырым питоновским словарём, включая нулевые причины.

Тесты закрепляют оба исправления: язык интерфейса и отчётов -- русский,
одно уведомление -- одно письмо.
"""

import asyncio

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import NotificationClaim, Project
from app.service import claim_notification, release_notification_claim
from app.truepost_playbook import truepost_playbook
from app.vocabulary import feedback_reason_label, format_feedback_reasons

# Реальные пороги движка, а не выдуманные: иначе тест проверяет не тот текст,
# который увидит владелец.
from app.growth_loop import DEFAULT_THRESHOLDS

THRESHOLDS = dict(DEFAULT_THRESHOLDS)

PAYMENT_PATH = {
    "first_post_feedback_good": 3,
    "first_post_feedback_bad": 3,
    "first_post_feedback_reasons": {
        "too_generic": 0, "wrong_style": 1, "wrong_topic": 0,
        "too_dry": 0, "too_salesy": 0, "other": 1,
    },
    "registrations": 16, "channels_created": 12, "pricing_viewed": 28,
}


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return lambda: Session(engine)


def _project(session) -> Project:
    p = Project(name="Тест", type="telegram_saas", is_active=True)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


# ---------------------------------------------------------------------------
# Язык: владелец не должен видеть коды продукта
# ---------------------------------------------------------------------------


class TestOwnerLanguage:
    def test_reason_codes_translated(self):
        assert feedback_reason_label("wrong_style") == "не тот стиль"
        assert feedback_reason_label("too_salesy") == "слишком рекламно"

    def test_unknown_reason_not_invented(self):
        """Незнакомый код отдаём как есть: выдуманный перевод хуже честного кода."""
        assert feedback_reason_label("какой_то_новый_код") == "какой_то_новый_код"

    def test_reasons_formatted_without_raw_dict(self):
        line = format_feedback_reasons(PAYMENT_PATH["first_post_feedback_reasons"])
        assert line == "не тот стиль — 1, другое — 1"
        assert "{" not in line and "'" not in line

    def test_zero_reasons_hidden(self):
        """Причина с нулём ничего не сообщает -- в тексте её быть не должно."""
        assert "слишком общий" not in format_feedback_reasons(
            PAYMENT_PATH["first_post_feedback_reasons"])
        assert format_feedback_reasons({"too_dry": 0}) == ""

    @pytest.mark.parametrize("area", ["first_post", "onboarding", "commercial_bridge",
                                      "ads", "pricing_screen", "payment_path"])
    def test_recommendation_text_has_no_english_codes(self, area):
        """Ни один текст, который читает владелец, не содержит кодов продукта."""
        rec = truepost_playbook(area, PAYMENT_PATH, THRESHOLDS)
        if rec is None:
            pytest.skip(f"область {area} сейчас ничего не предлагает")

        forbidden = ["wrong_style", "too_generic", "too_salesy", "too_dry",
                     "onboarding", "feedback", "good ", " bad", "live feed"]
        texts = [rec.get("title", ""), rec.get("action", ""), rec.get("hypothesis", ""),
                 rec.get("expected_effect", ""), rec.get("measure", ""),
                 rec.get("success_criterion", ""), rec.get("failure_criterion", "")]
        texts += list(rec.get("change_set") or [])
        texts += list(rec.get("locked_variables") or [])
        texts += list(rec.get("extra_evidence") or [])

        for text in texts:
            low = str(text).lower()
            for word in forbidden:
                assert word not in low, f"английское «{word.strip()}» в тексте: {text}"

    def test_first_post_names_reason_in_russian(self):
        rec = truepost_playbook("first_post", PAYMENT_PATH, THRESHOLDS)
        assert "не тот стиль" in rec["action"]
        assert "3 плохих из 6" in rec["action"]

    def test_missing_reasons_stated_honestly(self):
        """Причин нет -- так и пишем, а не показываем пустоту или выдуманное."""
        pp = dict(PAYMENT_PATH, first_post_feedback_reasons={})
        rec = truepost_playbook("first_post", pp, THRESHOLDS)
        assert "причина не указана" in rec["action"]
        assert "не прислал" in rec["extra_evidence"][0]

    def test_generic_reason_is_not_named_the_main_cause(self):
        """Баг с живого продукта: «other» набрало больше голосов, чем
        конкретная причина, и текст писал «Главная причина — другое» --
        формулировка, которая ничего не говорит владельцу о том, что чинить.
        Конкретная причина должна побеждать «другое», даже если у него
        больше голосов, а если конкретных причин нет вовсе -- честно так и
        сказать, а не подставить бессмысленное «другое»."""
        pp = dict(PAYMENT_PATH, first_post_feedback_reasons={"other": 20, "wrong_style": 3})
        rec = truepost_playbook("first_post", pp, THRESHOLDS)
        assert "другое" not in rec["action"].lower()
        assert "не тот стиль" in rec["action"]

        pp_only_generic = dict(PAYMENT_PATH, first_post_feedback_reasons={"other": 5})
        rec2 = truepost_playbook("first_post", pp_only_generic, THRESHOLDS)
        assert "другое" not in rec2["action"].lower()
        assert "поставили «плохо»" in rec2["action"]

    def test_no_jargon_about_generator_lock(self):
        """«Снимаем запрет с генератора» -- фраза, непонятная владельцу без
        технического бэкграунда. Текст должен объяснять простыми словами,
        что вообще происходит: меняем, как ИИ пишет пост."""
        rec = truepost_playbook("first_post", PAYMENT_PATH, THRESHOLDS)
        assert "запрет" not in rec["action"].lower()
        assert "промпт" not in rec["action"].lower()
        assert "ии" in rec["action"].lower()


# ---------------------------------------------------------------------------
# Одно уведомление -- одно письмо
# ---------------------------------------------------------------------------


class TestNotificationClaim:
    def test_second_claim_loses(self):
        factory = _session_factory()
        with factory() as session:
            pid = _project(session).id
            assert claim_notification(session, pid, "daily_board:2026-07-27") is True
        with factory() as session:
            assert claim_notification(session, pid, "daily_board:2026-07-27") is False

    def test_different_keys_do_not_block_each_other(self):
        factory = _session_factory()
        with factory() as session:
            pid = _project(session).id
            assert claim_notification(session, pid, "daily_board:2026-07-27") is True
            assert claim_notification(session, pid, "daily_board:2026-07-28") is True

    def test_released_claim_can_be_retaken(self):
        """Отправка упала -- заявка снимается, иначе письмо не уйдёт никогда."""
        factory = _session_factory()
        with factory() as session:
            pid = _project(session).id
            claim_notification(session, pid, "daily_board:2026-07-27")
            release_notification_claim(session, pid, "daily_board:2026-07-27")
            assert claim_notification(session, pid, "daily_board:2026-07-27") is True

    def test_daily_board_sends_once_under_race(self):
        """Два процесса одновременно -- владелец получает одно письмо."""
        from app import scheduler

        factory = _session_factory()
        with factory() as session:
            _project(session)

        sent = []

        async def fake_send(settings, text, chat_ids=None):
            await asyncio.sleep(0.05)  # окно, в которое проскакивал второй процесс
            sent.append(text)
            return True

        class Settings:
            daily_board_enabled = True
            bot_token = "1:x"
            admin_chat_ids_list = ["42"]
            quiet_hours_enabled = False

        async def run():
            await asyncio.gather(
                scheduler.send_daily_board(_send=fake_send, _session_factory=factory,
                                           _settings=Settings()),
                scheduler.send_daily_board(_send=fake_send, _session_factory=factory,
                                           _settings=Settings()),
            )

        asyncio.run(run())
        assert len(sent) == 1, f"отправлено писем: {len(sent)}"

    def test_daily_board_does_not_repeat_same_day(self):
        from app import scheduler

        factory = _session_factory()
        with factory() as session:
            _project(session)
        sent = []

        async def fake_send(settings, text, chat_ids=None):
            sent.append(text)
            return True

        class Settings:
            daily_board_enabled = True
            bot_token = "1:x"
            admin_chat_ids_list = ["42"]
            quiet_hours_enabled = False

        async def run():
            await scheduler.send_daily_board(_send=fake_send, _session_factory=factory,
                                             _settings=Settings())
            await scheduler.send_daily_board(_send=fake_send, _session_factory=factory,
                                             _settings=Settings())

        asyncio.run(run())
        assert len(sent) == 1
