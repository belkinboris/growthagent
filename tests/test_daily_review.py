"""
Тесты для Daily Business Review:
1. Query classifier: safe_negative / watch / winner / do_not_touch
2. Protected terms никогда не минусуются
3. Total conversions ≠ регистрации (нет GoalId)
4. Relevant low-spend query -> watch, не safe_negative
5. Obvious garbage -> safe_negative
6. Spend Gate: логика вердиктов
7. DirectIntelligenceResult.to_dict() + десериализация
8. /run не падает без direct_intelligence
9. build_owner_report backward compat без direct_intelligence
"""
import pytest

from app.query_classifier import (
    ActionType,
    DirectIntelligenceResult,
    QueryLabel,
    SpendGateVerdict,
    classify_query,
    classify_search_queries,
    evaluate_spend_gate,
    PROTECTED_TERMS,
    MIN_SPEND_FOR_NEGATIVE_RUB,
    MIN_CLICKS_FOR_NEGATIVE,
)
from app.owner_report import build_owner_report, _format_direct_intelligence_block
from app.rules import NormalizedMetrics


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------

def _metrics(**kw) -> NormalizedMetrics:
    defaults = dict(
        period_key="7d", signup=25, activation_1=20, activation_2=56,
        payment_started=1, payment_success=0, spend=4800, clicks=594,
        sources_ok={"product", "direct"},
    )
    defaults.update(kw)
    return NormalizedMetrics(**defaults)


def _row(
    query="автопост телеграм", clicks=10, cost=150.0, impressions=500,
    registrations=None, registration_attribution="none",
    campaign_name="Кампания 1", ad_group_name="Группа 1",
):
    return {
        "query": query,
        "clicks": clicks,
        "cost": cost,
        "impressions": impressions,
        "registrations": registrations,
        "registration_attribution": registration_attribution,
        "campaign_name": campaign_name,
        "ad_group_name": ad_group_name,
    }


# ---------------------------------------------------------------------------
# classify_query: основные ветки
# ---------------------------------------------------------------------------

class TestClassifyQuery:

    def test_winner_with_reliable_attribution(self):
        """Reliable attribution + регистрации -> WINNER."""
        result = classify_query(
            query="автопост telegram",
            clicks=50, cost=300.0,
            registrations=3, registration_attribution="reliable",
        )
        assert result.label == QueryLabel.WINNER
        assert result.action_item is not None
        assert result.action_item.action_type == ActionType.DO_NOT_TOUCH

    def test_winner_requires_reliable_attribution(self):
        """Без reliable attribution -- не winner, даже если registrations > 0."""
        result = classify_query(
            query="автопост telegram",
            clicks=50, cost=300.0,
            registrations=5, registration_attribution="none",
        )
        # Содержит protected term "telegram" -> DO_NOT_TOUCH, не winner
        assert result.label != QueryLabel.WINNER

    def test_winner_requires_min_spend(self):
        """winner только при cost >= MIN_SPEND_FOR_WINNER_RUB."""
        result = classify_query(
            query="автопост telegram",
            clicks=5, cost=10.0,  # мало
            registrations=1, registration_attribution="reliable",
        )
        assert result.label != QueryLabel.WINNER

    def test_protected_term_do_not_touch(self):
        """Запрос с protected term -> DO_NOT_TOUCH, независимо от метрик."""
        # Только специфичные для продукта термины защищены:
        # telegram/телеграм, нейросеть, автопостинг
        for term in ["телеграм", "нейросеть", "автопостинг"]:
            result = classify_query(query=f"создать {term} онлайн", clicks=100, cost=500.0)
            assert result.label == QueryLabel.DO_NOT_TOUCH, f"term={term} должен быть DO_NOT_TOUCH"

    def test_broad_non_product_terms_are_watch_not_do_not_touch(self):
        """Широкие нерелевантные термины (пост, бесплатно) не защищены — они WATCH."""
        # "пост" слишком широкий, но и не garbage -> WATCH
        result = classify_query(query="создать пост онлайн", clicks=10, cost=100.0)
        # Не должен быть DO_NOT_TOUCH (нет product-specific термина), но и не SAFE_NEGATIVE
        assert result.label in (QueryLabel.WATCH, QueryLabel.DO_NOT_TOUCH)
        assert result.label != QueryLabel.SAFE_NEGATIVE

    def test_obvious_garbage_safe_negative(self):
        """Явный мусор с достаточным расходом -> SAFE_NEGATIVE."""
        result = classify_query(
            query="шапка youtube оформление канала",
            clicks=15, cost=200.0,
            registrations=None, registration_attribution="none",
        )
        assert result.label == QueryLabel.SAFE_NEGATIVE
        assert result.action_item is not None
        assert result.action_item.action_type == ActionType.ADS_ACTION_SUGGESTED

    def test_garbage_low_spend_is_watch_not_negative(self):
        """Мусорный запрос, но мало данных -> WATCH, не SAFE_NEGATIVE."""
        result = classify_query(
            query="шапка youtube",
            clicks=2, cost=30.0,  # меньше MIN_SPEND_FOR_NEGATIVE_RUB
        )
        assert result.label == QueryLabel.WATCH

    def test_garbage_low_clicks_is_watch(self):
        """Мусор, но кликов меньше MIN_CLICKS_FOR_NEGATIVE -> WATCH."""
        result = classify_query(
            query="шапка ютуб оформление",
            clicks=2, cost=500.0,  # много денег, но мало кликов
        )
        assert result.label == QueryLabel.WATCH

    def test_relevant_low_spend_is_watch(self):
        """Релевантный запрос с малым расходом -> WATCH, не SAFE_NEGATIVE."""
        result = classify_query(
            query="как сделать контент для канала",
            clicks=3, cost=45.0,
        )
        assert result.label == QueryLabel.WATCH

    def test_unknown_query_is_watch(self):
        """Неопределённый запрос без registration data -> WATCH."""
        result = classify_query(
            query="сервис для работы с контентом",
            clicks=8, cost=120.0,
        )
        assert result.label == QueryLabel.WATCH

    def test_adult_content_safe_negative(self):
        """18+ контент -> SAFE_NEGATIVE при достаточном расходе."""
        result = classify_query(
            query="эротик контент онлайн",
            clicks=20, cost=300.0,
        )
        assert result.label == QueryLabel.SAFE_NEGATIVE
        assert result.garbage_category == "adult_18_plus"

    def test_academic_garbage_safe_negative(self):
        """Учёба/рефераты -> SAFE_NEGATIVE при достаточных данных."""
        result = classify_query(
            query="написать реферат онлайн",
            clicks=10, cost=150.0,
        )
        assert result.label == QueryLabel.SAFE_NEGATIVE

    def test_cross_platform_non_telegram_explicit_negative(self):
        """Кросспостинг в VK без упоминания автопоста -> SAFE_NEGATIVE."""
        result = classify_query(
            query="вконтакте постинг расписание",
            clicks=10, cost=150.0,
        )
        # Нет protected term, явный non-telegram cross-platform
        # Ожидаем SAFE_NEGATIVE или WATCH (зависит от паттерна)
        assert result.label in (QueryLabel.SAFE_NEGATIVE, QueryLabel.WATCH)
        assert result.label != QueryLabel.DO_NOT_TOUCH

    def test_cross_platform_with_autopost_is_do_not_touch(self):
        """'вконтакте автопост' -- содержит 'автопост' (protected) -> не минусуем."""
        result = classify_query(
            query="вконтакте автопост",
            clicks=10, cost=150.0,
        )
        # "автопост" — protected term: решение верное, не минусуем
        assert result.label == QueryLabel.DO_NOT_TOUCH

    def test_protected_overrides_garbage(self):
        """Protected term защищает даже при наличии garbage-паттерна."""
        # "автопостинг телеграм" -- содержит "телеграм" (protected)
        # Нельзя минусовать, даже если есть что-то похожее на мусор
        result = classify_query(
            query="автопостинг телеграм вконтакте",
            clicks=20, cost=300.0,
        )
        # "телеграм" защищает от минусования
        assert result.label != QueryLabel.SAFE_NEGATIVE


# ---------------------------------------------------------------------------
# classify_search_queries: интеграция
# ---------------------------------------------------------------------------

class TestClassifySearchQueries:

    def test_no_goal_id_no_registration_attribution(self):
        """Без registration_goal_id атрибуция регистраций недоступна."""
        rows = [_row(query="автопост telegram", registrations=5, registration_attribution="reliable")]
        result = classify_search_queries(rows, registration_goal_id=None)
        assert not result.has_registration_attribution
        assert "registration_goal_id" in result.missing_data
        # Winner невозможен без reliable attribution
        assert len(result.winners) == 0

    def test_with_goal_id_registration_attribution_works(self):
        """С registration_goal_id регистрации учитываются."""
        rows = [_row(
            query="автопост telegram",
            clicks=50, cost=300.0,
            registrations=3, registration_attribution="reliable",
        )]
        result = classify_search_queries(rows, registration_goal_id=12345)
        assert result.has_registration_attribution
        assert len(result.winners) == 1

    def test_total_conversions_not_registrations(self):
        """
        Direct API не даёт per-goal разбивку в SEARCH_QUERY_PERFORMANCE_REPORT.
        classify_search_queries всегда получает registration_goal_id=None из scheduler,
        поэтому attribution="none" и winner невозможен на основе конверсий из Direct.
        """
        rows = [_row(
            query="автопост телеграм",
            clicks=50, cost=300.0,
            # Даже если передать registrations и unreliable attribution --
            # classify_search_queries с goal_id=None выставит attribution="none"
            registrations=10,
            registration_attribution="unreliable",
        )]
        # С registration_goal_id=None - нет attribution, нет winners
        result = classify_search_queries(rows, registration_goal_id=None)
        assert result.has_registration_attribution is False
        # "телеграм" -> DO_NOT_TOUCH, не winner
        assert len(result.winners) == 0

    def test_garbage_with_spend_becomes_safe_negative(self):
        rows = [_row(query="шапка youtube", clicks=20, cost=300.0)]
        result = classify_search_queries(rows)
        assert len(result.safe_negatives) == 1
        assert result.safe_negatives[0].action_item is not None
        assert result.safe_negatives[0].action_item.action_type == ActionType.ADS_ACTION_SUGGESTED

    def test_protected_terms_not_in_negatives(self):
        """Protected terms не попадают в safe_negatives."""
        rows = [
            _row(query="бесплатный бот для телеграм", clicks=20, cost=300.0),
            _row(query="нейросеть для постов", clicks=15, cost=200.0),
            _row(query="автопостинг канала", clicks=10, cost=150.0),
        ]
        result = classify_search_queries(rows)
        for q in result.safe_negatives:
            for term in PROTECTED_TERMS:
                assert term not in q.query.lower(), (
                    f"Protected term '{term}' оказался в safe_negatives: {q.query}"
                )

    def test_to_dict_and_round_trip(self):
        """to_dict() возвращает сериализуемый dict."""
        import json
        rows = [
            _row(query="шапка youtube", clicks=20, cost=300.0),
            _row(query="автопост telegram", clicks=5, cost=50.0),
        ]
        result = classify_search_queries(rows)
        d = result.to_dict()
        # Должен быть JSON-сериализуемым
        serialized = json.dumps(d)
        assert serialized  # не пустой
        assert "safe_negatives" in d
        assert "watch" in d

    def test_action_items_generated(self):
        """Action items генерируются для safe_negatives."""
        rows = [_row(query="шапка youtube", clicks=20, cost=300.0)]
        result = classify_search_queries(rows)
        ads_actions = [a for a in result.action_items if a.action_type == ActionType.ADS_ACTION_SUGGESTED]
        assert len(ads_actions) >= 1


# ---------------------------------------------------------------------------
# Spend Gate
# ---------------------------------------------------------------------------

class TestSpendGate:

    def test_no_registrations_high_spend_pause(self):
        """Нет регистраций + значимый расход -> PAUSE_RECOMMENDED."""
        sg = evaluate_spend_gate(
            spend_rub=1000.0, registrations=0, channels_created=0,
            payment_started=0, payment_success=0, pricing_viewed=None,
        )
        assert sg.verdict == SpendGateVerdict.PAUSE_RECOMMENDED

    def test_no_registrations_low_spend_do_not_scale(self):
        """Нет регистраций, но расход мал -> DO_NOT_SCALE (не пауза)."""
        sg = evaluate_spend_gate(
            spend_rub=100.0, registrations=0, channels_created=0,
            payment_started=0, payment_success=0, pricing_viewed=None,
        )
        # Ниже порога 500 руб -- не PAUSE, возвращает DO_NOT_SCALE
        assert sg.verdict != SpendGateVerdict.PAUSE_RECOMMENDED

    def test_registrations_no_activation_do_not_scale(self):
        """Регистрации есть, активации нет -> DO_NOT_SCALE."""
        sg = evaluate_spend_gate(
            spend_rub=2000.0, registrations=20, channels_created=0,
            payment_started=0, payment_success=0, pricing_viewed=None,
        )
        assert sg.verdict == SpendGateVerdict.DO_NOT_SCALE
        assert not sg.has_activation

    def test_payment_success_controlled_spend_ok(self):
        """Есть успешные оплаты -> CONTROLLED_SPEND_OK."""
        sg = evaluate_spend_gate(
            spend_rub=4800.0, registrations=32, channels_created=27,
            payment_started=1, payment_success=1, pricing_viewed=5,
        )
        assert sg.verdict == SpendGateVerdict.CONTROLLED_SPEND_OK

    def test_many_registrations_no_payment_intent_monetization_warn(self):
        """Много регистраций + активация + нет payment intent -> MONETIZATION_NOT_PROVEN."""
        sg = evaluate_spend_gate(
            spend_rub=8000.0, registrations=60, channels_created=50,
            payment_started=0, payment_success=0, pricing_viewed=0,
        )
        assert sg.verdict == SpendGateVerdict.MONETIZATION_NOT_PROVEN

    def test_few_registrations_no_payment_do_not_scale(self):
        """Регистрации + активация есть, payment intent нет, мало регистраций -> DO_NOT_SCALE."""
        sg = evaluate_spend_gate(
            spend_rub=4800.0, registrations=32, channels_created=27,
            payment_started=0, payment_success=0, pricing_viewed=1,
        )
        assert sg.verdict == SpendGateVerdict.DO_NOT_SCALE

    def test_payment_intent_no_success_do_not_scale(self):
        """Есть payment intent, нет success -> DO_NOT_SCALE (ждём)."""
        sg = evaluate_spend_gate(
            spend_rub=4800.0, registrations=32, channels_created=27,
            payment_started=1, payment_success=0, pricing_viewed=5,
        )
        assert sg.verdict == SpendGateVerdict.DO_NOT_SCALE
        assert sg.has_payment_intent

    def test_one_payment_started_not_p1(self):
        """1 payment_started без success -- spend gate не говорит P1."""
        sg = evaluate_spend_gate(
            spend_rub=4800.0, registrations=32, channels_created=27,
            payment_started=1, payment_success=0, pricing_viewed=3,
        )
        # Вердикт не должен быть PAUSE_RECOMMENDED из-за одной попытки
        assert sg.verdict != SpendGateVerdict.PAUSE_RECOMMENDED

    def test_spend_gate_action_items_typed(self):
        """Action items Spend Gate имеют правильный ActionType."""
        sg = evaluate_spend_gate(
            spend_rub=4800.0, registrations=32, channels_created=27,
            payment_started=0, payment_success=0, pricing_viewed=0,
        )
        for ai in sg.action_items:
            assert ai.action_type in list(ActionType)


# ---------------------------------------------------------------------------
# build_owner_report: backward compat и direct_intelligence
# ---------------------------------------------------------------------------

class TestBuildOwnerReportWithDirectIntelligence:

    def test_backward_compat_no_direct_intelligence(self):
        """build_owner_report работает без direct_intelligence."""
        report = build_owner_report("АвтоПост", _metrics())
        assert report is not None

    def test_report_with_direct_intelligence_none_shows_placeholder(self):
        """Если direct_intelligence=None, в отчёте есть пометка о недоступности."""
        report = build_owner_report(
            "АвтоПост", _metrics(), direct_intelligence=None,
        )
        assert report is not None
        # Должна быть пометка что данные недоступны
        assert "не собраны" in report or "deep_direct" in report

    def test_report_with_direct_intelligence_shows_winners(self):
        """Если direct_intelligence содержит winners, они отражаются в отчёте."""
        from app.query_classifier import QueryClassification

        di = DirectIntelligenceResult(
            period_label="7д",
            winners=[QueryClassification(
                query="автопост telegram",
                label=QueryLabel.WINNER,
                reason="3 регистр.",
                clicks=50, cost=300.0,
                registrations=3,
                registration_attribution="reliable",
            )],
            has_registration_attribution=True,
        )
        report = build_owner_report(
            "АвтоПост", _metrics(), direct_intelligence=di,
        )
        assert report is not None
        assert "автопост telegram" in report or "Winners" in report

    def test_report_with_safe_negatives_shows_them(self):
        """Safe negatives отражаются в отчёте."""
        from app.query_classifier import QueryClassification, ActionItem

        di = DirectIntelligenceResult(
            period_label="7д",
            safe_negatives=[QueryClassification(
                query="шапка youtube",
                label=QueryLabel.SAFE_NEGATIVE,
                reason="youtube_decoration",
                clicks=15, cost=200.0,
                action_item=ActionItem(
                    ActionType.ADS_ACTION_SUGGESTED,
                    'Минус-фраза: "шапка youtube"',
                    "youtube decoration",
                ),
            )],
        )
        report = build_owner_report(
            "АвтоПост", _metrics(), direct_intelligence=di,
        )
        assert report is not None
        assert "шапка youtube" in report

    def test_format_direct_intelligence_block_none_shows_message(self):
        """Блок рекламы с None показывает сообщение о недоступности данных."""
        block = _format_direct_intelligence_block(None)
        assert block is not None
        assert "не собраны" in block or "deep_direct" in block

    def test_format_direct_intelligence_block_with_data(self):
        """Блок рекламы с данными показывает статистику."""
        from app.query_classifier import QueryClassification

        di = DirectIntelligenceResult(
            period_label="7д",
            watch=[QueryClassification(
                query="создание постов для канала",
                label=QueryLabel.WATCH,
                reason="данных мало",
                clicks=5, cost=75.0,
            )],
            total_queries_analyzed=10,
            total_spend=500.0,
            total_clicks=80,
        )
        block = _format_direct_intelligence_block(di)
        assert block is not None
        assert "Watch" in block or "наблюдать" in block


# ---------------------------------------------------------------------------
# Десериализация direct_intelligence в telegram_bot
# ---------------------------------------------------------------------------

class TestDeserializeDirectIntelligence:

    def test_none_returns_none(self):
        from app.telegram_bot import _deserialize_direct_intelligence
        assert _deserialize_direct_intelligence(None) is None

    def test_empty_dict_returns_none(self):
        from app.telegram_bot import _deserialize_direct_intelligence
        assert _deserialize_direct_intelligence({}) is None

    def test_valid_dict_round_trip(self):
        """to_dict() -> _deserialize -> поля совпадают."""
        from app.telegram_bot import _deserialize_direct_intelligence
        from app.query_classifier import QueryClassification

        di_orig = DirectIntelligenceResult(
            period_label="7д",
            safe_negatives=[QueryClassification(
                query="шапка youtube",
                label=QueryLabel.SAFE_NEGATIVE,
                reason="youtube_decoration",
                clicks=15, cost=200.0,
            )],
            total_queries_analyzed=1,
            total_spend=200.0,
            total_clicks=15,
        )
        serialized = di_orig.to_dict()
        restored = _deserialize_direct_intelligence(serialized)
        assert restored is not None
        assert len(restored.safe_negatives) == 1
        assert restored.safe_negatives[0].query == "шапка youtube"
        assert restored.total_spend == 200.0

    def test_invalid_dict_returns_none(self):
        """Невалидный dict не падает, возвращает None."""
        from app.telegram_bot import _deserialize_direct_intelligence
        assert _deserialize_direct_intelligence({"garbage": "data", "winners": "not_a_list"}) is None


# ---------------------------------------------------------------------------
# Тесты _format_intel_status_note и fallback path
# ---------------------------------------------------------------------------

class TestIntelStatusNote:
    """_format_intel_status_note всегда возвращает непустую строку в новом формате."""

    def test_ok_with_rows(self):
        from app.telegram_bot import _format_intel_status_note
        note = _format_intel_status_note("ok", None, 45)
        assert "45" in note
        assert note  # не пустая

    def test_ok_zero_rows(self):
        from app.telegram_bot import _format_intel_status_note
        note = _format_intel_status_note("ok", None, 0)
        assert note

    def test_not_configured(self):
        from app.telegram_bot import _format_intel_status_note
        note = _format_intel_status_note("not_configured", None, 0)
        assert note
        # Нет технического жаргона
        assert "Direct Intelligence" not in note

    def test_timeout(self):
        from app.telegram_bot import _format_intel_status_note
        note = _format_intel_status_note("timeout", ">60s", 0)
        assert note

    def test_error(self):
        from app.telegram_bot import _format_intel_status_note
        note = _format_intel_status_note("error", "HTTP 500", 0)
        assert note

    def test_exception(self):
        from app.telegram_bot import _format_intel_status_note
        note = _format_intel_status_note("exception", "NoneType error", 0)
        assert note

    def test_unknown_status_not_empty(self):
        from app.telegram_bot import _format_intel_status_note
        note = _format_intel_status_note("weird_status", None, 0)
        assert note


class TestDeepDirectFallbackPath:
    """
    Direct Intelligence запускается даже когда legacy fallback.
    """

    @pytest.mark.asyncio
    async def test_intel_runs_when_legacy_fails(self):
        """При legacy ok=False — Direct Intelligence всё равно вызывается."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        sent_messages = []
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(side_effect=lambda chat_id, text, **kw: sent_messages.append(text))

        mock_project = MagicMock()
        mock_project.id = 1
        mock_project.name = "Test"

        legacy_fail = {"ok": False, "error": "timeout", "timeout": True}
        intel_ok = {"status": "ok", "result": {"total_queries_analyzed": 10}}

        with patch("app.telegram_bot.get_session") as mock_gs, \
             patch("app.scheduler.run_direct_intelligence_for_project",
                   new_callable=AsyncMock, return_value=intel_ok), \
             patch("app.scheduler.force_refresh_deep_diagnostics_sync_with_timeout",
                   return_value=legacy_fail), \
             patch("app.telegram_bot._get_active_project", return_value=mock_project), \
             patch("app.telegram_bot._get_best_deep_direct_fallback_sync",
                   return_value=(None, "Test")):
            mock_session = MagicMock()
            mock_session.__enter__ = MagicMock(return_value=mock_session)
            mock_session.__exit__ = MagicMock(return_value=False)
            mock_session.get = MagicMock(return_value=mock_project)
            mock_gs.return_value = mock_session

            from app.telegram_bot import _deep_direct_background
            from datetime import datetime, timezone
            await _deep_direct_background(
                chat_id=123, bot=mock_bot, project_id=1,
                started_at=datetime.now(timezone.utc),
            )

        assert len(sent_messages) >= 1
        # Первое сообщение — статус обновления рекламных данных (без технического языка)
        intel_msg = sent_messages[0]
        # Должно упоминать запросы или Директ, но не "Direct Intelligence"
        assert any(kw in intel_msg for kw in ["запрос", "данных", "Директ", "реклам"]), \
            f"Ожидали понятное сообщение об обновлении, получили: {intel_msg!r}"

    @pytest.mark.asyncio
    async def test_intel_status_sent_when_not_configured(self):
        """При not_configured пользователь видит понятное сообщение без технических терминов."""
        from unittest.mock import AsyncMock, MagicMock, patch

        sent_messages = []
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(side_effect=lambda chat_id, text, **kw: sent_messages.append(text))

        mock_project = MagicMock()
        mock_project.id = 1
        mock_project.name = "Test"

        intel_nc = {"status": "not_configured", "result": None, "error": None}
        legacy_fail = {"ok": False, "error": "timeout"}

        with patch("app.telegram_bot.get_session") as mock_gs, \
             patch("app.scheduler.run_direct_intelligence_for_project",
                   new_callable=AsyncMock, return_value=intel_nc), \
             patch("app.scheduler.force_refresh_deep_diagnostics_sync_with_timeout",
                   return_value=legacy_fail), \
             patch("app.telegram_bot._get_active_project", return_value=mock_project), \
             patch("app.telegram_bot._get_best_deep_direct_fallback_sync",
                   return_value=(None, "Test")):
            mock_session = MagicMock()
            mock_session.__enter__ = MagicMock(return_value=mock_session)
            mock_session.__exit__ = MagicMock(return_value=False)
            mock_session.get = MagicMock(return_value=mock_project)
            mock_gs.return_value = mock_session

            from app.telegram_bot import _deep_direct_background
            from datetime import datetime, timezone
            await _deep_direct_background(
                chat_id=123, bot=mock_bot, project_id=1,
                started_at=datetime.now(timezone.utc),
            )

        assert len(sent_messages) >= 1
        intel_msg = sent_messages[0]
        # Не должно содержать "Direct Intelligence" в сообщении пользователю
        assert "Direct Intelligence" not in intel_msg, \
            f"Технический термин в пользовательском сообщении: {intel_msg!r}"


class TestCacheKeyConsistency:
    def test_save_key_equals_read_key(self):
        """Ключ сохранения и чтения Direct Intelligence кэша должен быть одинаковым."""
        from app.service import DIRECT_INTELLIGENCE_CACHE_PERIOD_KEY
        from app.telegram_bot import DIRECT_INTELLIGENCE_CACHE_PERIOD_KEY as tb_key

        assert DIRECT_INTELLIGENCE_CACHE_PERIOD_KEY == tb_key, (
            f"Ключ сохранения ({DIRECT_INTELLIGENCE_CACHE_PERIOD_KEY!r}) "
            f"не совпадает с ключом чтения в telegram_bot ({tb_key!r})"
        )

    def test_cache_key_value(self):
        """Значение ключа зафиксировано — защита от случайного переименования."""
        from app.service import DIRECT_INTELLIGENCE_CACHE_PERIOD_KEY
        assert DIRECT_INTELLIGENCE_CACHE_PERIOD_KEY == "direct_intelligence_24h"


# ---------------------------------------------------------------------------
# Тест: fallback /run читает DI кэш
# ---------------------------------------------------------------------------

class TestFallbackRunReadsDICache:
    def test_build_cached_cycle_response_reads_di(self):
        """
        _build_cached_cycle_response должен читать DI кэш и передавать
        его в _format_cached_business_report.
        Даже если live /run упал — рекламный блок должен присутствовать.
        """
        from unittest.mock import MagicMock, patch
        from app.telegram_bot import _build_cached_cycle_response
        from app.query_classifier import DirectIntelligenceResult, QueryClassification, QueryLabel

        # Мок DI cache объекта (ORM DeepDiagnosticsCache)
        di_result = DirectIntelligenceResult(
            period_label="7д",
            safe_negatives=[QueryClassification(
                query="шапка ютуб",
                label=QueryLabel.SAFE_NEGATIVE,
                reason="youtube_decoration",
                clicks=15, cost=200.0,
            )],
            total_queries_analyzed=5,
            total_spend=500.0,
            total_clicks=80,
        )
        mock_di_cache = MagicMock()
        mock_di_cache.ok = True
        mock_di_cache.result_json = di_result.to_dict()

        # Мок snapshot с нужными метриками
        from app.rules import NormalizedMetrics
        from unittest.mock import MagicMock
        mock_snapshot = MagicMock()
        mock_snapshot.created_at = None
        mock_snapshot.metrics_json = {
            "product": {
                "registrations": 32, "channels_created": 27,
                "post_generations": 80, "payments_success": 0,
            },
            "source_statuses": {},
        }

        mock_project = MagicMock()
        mock_project.id = 1
        mock_project.name = "АвтоПост"

        def mock_get_cached(session, project_id, period_key):
            if period_key == "direct_intelligence_24h":
                return mock_di_cache
            return None

        with patch("app.telegram_bot.get_session") as mock_gs, \
             patch("app.telegram_bot._get_active_project", return_value=mock_project), \
             patch("app.telegram_bot._get_latest_combined_snapshot", return_value=mock_snapshot), \
             patch("app.telegram_bot._snapshot_has_product_metrics", return_value=True), \
             patch("app.telegram_bot.get_cached_diagnostics", side_effect=mock_get_cached):

            mock_session = MagicMock()
            mock_session.__enter__ = MagicMock(return_value=mock_session)
            mock_session.__exit__ = MagicMock(return_value=False)
            mock_gs.return_value = mock_session

            result = _build_cached_cycle_response(reason="error")

        assert result is not None, "Fallback report не должен быть None при наличии snapshot"
        # Рекламный блок должен присутствовать
        assert "шапка ютуб" in result or "safe_negative" in result.lower() or "Реклама" in result, (
            f"Рекламный блок из DI cache не найден в fallback отчёте. "
            f"Начало: {result[:200]!r}"
        )

    def test_format_cached_business_report_with_di(self):
        """_format_cached_business_report с direct_intelligence показывает рекламный блок."""
        from unittest.mock import MagicMock
        from app.telegram_bot import _format_cached_business_report
        from app.query_classifier import DirectIntelligenceResult, QueryClassification, QueryLabel

        di = DirectIntelligenceResult(
            period_label="7д",
            safe_negatives=[QueryClassification(
                query="шапка ютуб",
                label=QueryLabel.SAFE_NEGATIVE,
                reason="youtube_decoration",
                clicks=15, cost=200.0,
            )],
            total_queries_analyzed=3,
        )

        mock_snapshot = MagicMock()
        mock_snapshot.created_at = None
        mock_snapshot.metrics_json = {
            "product": {
                "registrations": 32, "channels_created": 27,
                "post_generations": 80, "payments_success": 0,
            },
            "source_statuses": {},
        }

        result = _format_cached_business_report(
            "АвтоПост",
            mock_snapshot,
            reason="error",
            direct_intelligence_dict=di.to_dict(),
        )
        assert result is not None
        assert "Реклама" in result or "шапка ютуб" in result, (
            f"Рекламный блок не найден. Начало: {result[:300]!r}"
        )


# ---------------------------------------------------------------------------
# Тесты: garbage overrides protected (П4)
# ---------------------------------------------------------------------------

class TestGarbageOverridesProtected:
    """Garbage категории из UNCONDITIONAL_GARBAGE должны overriding protected terms."""

    def test_profile_decoration_overrides_telegram(self):
        """'шапка профиля в телеграм' — garbage despite 'телеграм'."""
        r = classify_query("шапка профиля в телеграм", clicks=10, cost=150.0)
        assert r.label == QueryLabel.SAFE_NEGATIVE, f"Expected safe_negative, got {r.label}: {r.reason}"

    def test_profile_decoration_genitive_case(self):
        """'шапку профиля в телеграмм через ии' — garbage despite 'ии'/'телеграм'."""
        r = classify_query("сгенерировать шапку профиля в телеграмм через ии", clicks=10, cost=150.0)
        assert r.label == QueryLabel.SAFE_NEGATIVE, f"Expected safe_negative, got {r.label}: {r.reason}"

    def test_youtube_decoration_overrides_protected(self):
        """'шапка канала ютуб' — garbage despite nothing."""
        r = classify_query("шапка канала ютуб", clicks=10, cost=150.0)
        assert r.label == QueryLabel.SAFE_NEGATIVE

    def test_relevant_telegram_queries_not_negative(self):
        """'нейросеть для telegram канала' — do_not_touch, не safe_negative."""
        r = classify_query("нейросеть для telegram канала", clicks=10, cost=150.0)
        assert r.label != QueryLabel.SAFE_NEGATIVE

    def test_autoposting_with_context_not_negative(self):
        """'автопостинг по группам без премиума тг' — массовый постинг/обход, должен быть watch или safe_negative."""
        r = classify_query("автопостинг по группам без премиума тг", clicks=10, cost=150.0)
        # Это интент обхода ограничений, а не core AI-постинг — не должен быть do_not_touch
        assert r.label != QueryLabel.DO_NOT_TOUCH, \
            "Запрос с обходом ограничений не должен быть в 'Что оставить'"

    def test_autoposting_bypass_is_watch_or_negative(self):
        """'постинг по группам без премиума' с достаточным расходом — safe_negative."""
        r = classify_query("постинг по группам без премиума", clicks=10, cost=150.0)
        assert r.label in (QueryLabel.SAFE_NEGATIVE, QueryLabel.WATCH), \
            f"Ожидали safe_negative или watch, получили {r.label}"

    def test_bot_for_posts_not_negative(self):
        """'бот для постов в телеграм' — do_not_touch."""
        r = classify_query("бот для постов в телеграм", clicks=10, cost=150.0)
        assert r.label == QueryLabel.DO_NOT_TOUCH

    def test_action_summary_includes_product(self):
        """Action summary содержит Product блок."""
        from app.owner_report import _format_action_items_block
        from app.rules import NormalizedMetrics
        m = NormalizedMetrics(
            period_key="7d", signup=32, activation_1=27, activation_2=80,
            payment_started=0, payment_success=0, spend=4800, clicks=594,
            sources_ok={"product"},
        )
        pp = {"pricing_viewed": 1, "payment_cta_clicked": 0,
              "payment_started": 0, "payment_success": 0}
        block = _format_action_items_block(None, metrics=m, payment_path=pp)
        assert block is not None
        assert "Продукт" in block or "🛠" in block, f"Product action не найден: {block}"

    def test_no_contradicting_direct_block_when_di_present(self):
        """Если DI есть — блок 'кэш не приложен' не должен быть в отчёте."""
        from app.owner_report import _format_direct_decision_layer
        # Без DI — старый текст
        block_no_di = _format_direct_decision_layer(None, has_direct_intelligence=False)
        assert "не приложен" in block_no_di
        # С DI — другой текст
        block_with_di = _format_direct_decision_layer(None, has_direct_intelligence=True)
        assert "не приложен" not in block_with_di
        assert "Direct Intelligence" in block_with_di

    def test_payment_path_shown_in_full_report(self):
        """build_owner_report показывает payment path блок если данные есть."""
        from app.owner_report import build_owner_report
        from app.rules import NormalizedMetrics
        m = NormalizedMetrics(
            period_key="7d", signup=32, activation_1=27, activation_2=80,
            payment_started=1, payment_success=0, spend=4800, clicks=594,
            sources_ok={"product"},
        )
        pp = {"registrations": 32, "channels_created": 27, "post_generations": 80,
              "pricing_viewed": 1, "payment_cta_clicked": 0,
              "payment_started": 1, "payment_success": 0,
              "payment_failed": 0, "payment_returned": 0, "missing_data": []}
        report = build_owner_report("АвтоПост", m, payment_path_diagnostics=pp)
        assert report is not None
        assert "Путь до оплаты" in report

    def test_pricing_viewed_1_no_tariff_conclusion(self):
        """pricing_viewed=1 не даёт вывод 'люди видят тарифы, но не кликают'."""
        from app.owner_report import _format_payment_path_block
        pp = {"registrations": 32, "channels_created": 27, "post_generations": 80,
              "pricing_viewed": 1, "payment_cta_clicked": 0,
              "payment_started": 0, "payment_success": 0,
              "payment_failed": 0, "payment_returned": 0, "missing_data": []}
        block = _format_payment_path_block(pp)
        bad = ["видят тарифы, но не кликают", "видели тарифы, но никто", "Люди открывают тарифы"]
        for phrase in bad:
            assert phrase not in block, f"Найдена запрещённая фраза: {phrase!r}"


# ---------------------------------------------------------------------------
# Snapshot тесты: запрещённые термины в пользовательских командах
# ---------------------------------------------------------------------------

BANNED_TERMS = [
    "legacy", "fallback", " watch", "winners", "protected",
    "payment flow", "per-query attribution", "GoalId",
    "Direct Intelligence", "cache", "live collection",
    "SEARCH_QUERY_PERFORMANCE_REPORT", "pricing_viewed",
    "payment_cta_clicked", "payment_started", "payment_success",
]


class TestCommercialReportBannedTerms:
    """Пользовательские тексты не содержат технического жаргона."""

    def _make_metrics(self, **kw):
        from app.rules import NormalizedMetrics
        defaults = dict(
            period_key="7d", signup=32, activation_1=27, activation_2=80,
            payment_started=0, payment_success=0, spend=4800, clicks=594,
            sources_ok=set(),
        )
        defaults.update(kw)
        return NormalizedMetrics(**defaults)

    def _make_pp(self, pricing_viewed=1):
        return {
            "registrations": 32, "channels_created": 27, "post_generations": 80,
            "pricing_viewed": pricing_viewed, "payment_cta_clicked": 0,
            "payment_started": 0, "payment_success": 0,
            "payment_failed": 0, "payment_returned": 0, "missing_data": [],
        }

    def test_run_report_no_banned_terms(self):
        from app.commercial_report import build_run_report
        report = build_run_report("АвтоПост", self._make_metrics(), payment_path=self._make_pp())
        report_lower = report.lower()
        for term in BANNED_TERMS:
            assert term.lower() not in report_lower, \
                f"Запрещённый термин {term!r} найден в /run отчёте"

    def test_ads_report_no_watch_winners_protected(self):
        from app.commercial_report import build_ads_report
        report = build_ads_report("АвтоПост", direct_intelligence=None)
        for term in [" watch", "winners", "protected", "Direct Intelligence"]:
            assert term.lower() not in report.lower(), \
                f"Запрещённый термин {term!r} найден в /ads отчёте"

    def test_funnel_report_no_banned_terms(self):
        from app.commercial_report import build_funnel_report
        report = build_funnel_report("АвтоПост", self._make_metrics(), payment_path=self._make_pp())
        for term in ["pricing_viewed", "payment_cta_clicked", "backend", "cache"]:
            assert term.lower() not in report.lower(), \
                f"Запрещённый термин {term!r} найден в /funnel отчёте"

    def test_pay_report_no_banned_terms(self):
        from app.commercial_report import build_pay_report
        report = build_pay_report("АвтоПост", payment_path=self._make_pp())
        for term in ["payment_started", "payment_success", "payment_cta_clicked", "cache"]:
            assert term.lower() not in report.lower(), \
                f"Запрещённый термин {term!r} найден в /pay отчёте"

    def test_run_pricing_viewed_1_no_tariff_conclusion(self):
        """При pricing_viewed=1 нет вывода 'люди видят тарифы, но не кликают'."""
        from app.commercial_report import build_run_report
        report = build_run_report(
            "АвтоПост", self._make_metrics(),
            payment_path=self._make_pp(pricing_viewed=1),
        )
        bad_phrases = [
            "видят тарифы, но не кликают",
            "видели тарифы, но никто",
            "Люди открывают тарифы",
        ]
        for phrase in bad_phrases:
            assert phrase not in report, f"Запрещённая фраза {phrase!r} в /run"

    def test_run_contains_product_action(self):
        """Product action присутствует когда есть активация, но мало просмотров тарифов."""
        from app.commercial_report import build_run_report
        report = build_run_report(
            "АвтоПост", self._make_metrics(activation_1=27),
            payment_path=self._make_pp(pricing_viewed=1),
        )
        assert "Продукт" in report or "путь от" in report.lower(), \
            "Product action не найден в /run"

    def test_run_contains_key_numbers(self):
        """Ключевые числа присутствуют в /run."""
        from app.commercial_report import build_run_report
        report = build_run_report("АвтоПост", self._make_metrics(), payment_path=self._make_pp())
        assert "32" in report  # signup
        assert "27" in report  # activation_1
        assert "594" in report  # clicks

    def test_deep_direct_status_no_legacy(self):
        """Статус /deep_direct не содержит слово 'legacy'."""
        from app.commercial_report import build_deep_direct_status
        status = build_deep_direct_status(
            intel_status="ok", intel_rows=3283, intel_error=None,
            legacy_ok=False, project_name="АвтоПост",
        )
        assert "legacy" not in status.lower(), \
            f"Слово 'legacy' найдено в статусе /deep_direct: {status!r}"

    def test_deep_direct_status_not_configured_no_technical(self):
        """not_configured статус понятен владельцу."""
        from app.commercial_report import build_deep_direct_status
        status = build_deep_direct_status(
            intel_status="not_configured", intel_rows=0, intel_error=None,
            legacy_ok=False, project_name="АвтоПост",
        )
        assert "Direct Intelligence" not in status

    def test_fallback_run_not_scary(self):
        """Fallback /run не начинается со страшного технического сообщения."""
        from app.commercial_report import build_run_report
        from datetime import datetime, timezone
        report = build_run_report(
            "АвтоПост", self._make_metrics(),
            payment_path=self._make_pp(),
            snapshot_dt=datetime.now(timezone.utc),
            is_fallback=True,
        )
        scary_phrases = [
            "Живой сбор данных завершился с ошибкой",
            "live collection failed",
        ]
        for phrase in scary_phrases:
            assert phrase not in report, \
                f"Страшная фраза {phrase!r} найдена в fallback /run"

    def test_msk_time_in_report(self):
        """Время в /run в русском формате МСК."""
        from app.commercial_report import build_run_report
        report = build_run_report("АвтоПост", self._make_metrics())
        assert "МСК" in report
        assert "UTC" not in report
