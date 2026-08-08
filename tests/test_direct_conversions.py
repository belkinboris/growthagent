"""
Конверсии в отчётах Директа и чтение стратегии кампании (задача R11).

До этой правки Директ запрашивал только показы/клики/расход. Из-за этого
платформа знала, СКОЛЬКО потрачено, но не знала, что эти деньги принесли —
и не могла сказать, какая кампания приводит платящих, а какая жжёт бюджет.

Здесь проверяется сама сборка запроса и разбор ответа, без похода в сеть.
"""
import pytest

from app.connectors import direct
from app.connectors.direct_campaigns import _extract_strategy


class TestGoalsInReportRequest:
    def test_goals_go_next_to_selection_criteria_not_inside(self):
        """
        Внутри SelectionCriteria API молча игнорирует Goals: отчёт приходит
        без конверсий и выглядит как «целей нет». Ошибку легко внести
        повторно, поэтому она закреплена тестом.
        """
        d = direct._build_report_definition([], "2026-08-01", "2026-08-07", goal_ids=[111])
        params = d["params"]
        assert params["Goals"] == ["111"]
        assert "Goals" not in params["SelectionCriteria"]
        assert params["AttributionModels"] == [direct.DEFAULT_ATTRIBUTION_MODEL]

    def test_conversions_field_is_requested(self):
        d = direct._build_report_definition([], "2026-08-01", "2026-08-07", goal_ids=[111])
        assert "Conversions" in d["params"]["FieldNames"]

    def test_without_goals_request_is_unchanged(self):
        """Старое поведение должно сохраниться дословно, без пустых ключей."""
        d = direct._build_report_definition([], "2026-08-01", "2026-08-07")
        assert "Goals" not in d["params"]
        assert "AttributionModels" not in d["params"]
        assert "Conversions" not in d["params"]["FieldNames"]

    @pytest.mark.parametrize("builder", [
        direct._build_ad_group_report_definition,
        direct._build_search_query_report_definition,
    ])
    def test_granular_reports_also_carry_goals(self, builder):
        """Без этого не узнать, какой запрос/группа приводит платящих."""
        d = builder([], "2026-08-01", "2026-08-07", goal_ids=[111, 222])
        assert d["params"]["Goals"] == ["111", "222"]


class TestConversionsParsing:
    def test_per_goal_columns_are_parsed(self):
        row = {"CampaignId": "1", "Conversions_111_LSCCD": "7",
               "Conversions_999_LSCCD": "2"}
        assert direct._conversions_from_row(row) == {"111": 7, "999": 2}

    def test_dash_means_zero_not_a_crash(self):
        """«--» -- это способ Директа сказать «нет данных», а не поломка."""
        assert direct._conversions_from_row({"Conversions_111_LSCCD": "--"}) == {"111": 0}

    def test_rows_without_conversions_give_empty_dict(self):
        assert direct._conversions_from_row({"CampaignId": "1", "Clicks": "10"}) == {}

    def test_report_aggregates_conversions_across_campaigns(self):
        tsv = (
            "CampaignId\tCampaignName\tImpressions\tClicks\tCost\tCtr\tAvgCpc\tConversions_111_LSCCD\n"
            "1\tПоиск\t1000\t100\t50000000\t10.0\t500000\t7\n"
            "2\tРСЯ\t2000\t50\t25000000\t2.5\t500000\t3\n"
        )
        result = direct._handle_success(tsv, [200], "2026-08-01", "2026-08-07")
        assert result["conversions"] == {"111": 10}
        assert len(result["campaigns"]) == 2
        assert result["campaigns"][0]["conversions"] == {"111": 7}

    def test_report_without_goals_has_no_conversions_key(self):
        """Пустой ключ выглядел бы как «конверсий ноль» -- это разные вещи."""
        tsv = (
            "CampaignId\tCampaignName\tImpressions\tClicks\tCost\tCtr\tAvgCpc\n"
            "1\tПоиск\t1000\t100\t50000000\t10.0\t500000\n"
        )
        result = direct._handle_success(tsv, [200], "2026-08-01", "2026-08-07")
        assert "conversions" not in result
        assert result["spend"] == 50.0


class TestStrategyExtraction:
    def test_average_cpa_goal_and_price_are_read(self):
        campaign = {"TextCampaign": {"BiddingStrategy": {"Search": {
            "BiddingStrategyType": "AVERAGE_CPA",
            "AverageCpa": {"GoalId": 111, "AverageCpa": 300_000_000},
        }, "Network": {}}}}
        s = _extract_strategy(campaign)
        assert s["strategy_type"] == "AVERAGE_CPA"
        assert s["goal_id"] == "111"
        assert s["target_price"] == 300.0, "цена приходит в микро-единицах"
        assert s["optimizes_for_goal"] is True

    def test_manual_bidding_is_not_goal_driven(self):
        campaign = {"TextCampaign": {"BiddingStrategy": {
            "Search": {"BiddingStrategyType": "HIGHEST_POSITION"}, "Network": {}}}}
        s = _extract_strategy(campaign)
        assert s["optimizes_for_goal"] is False
        assert s["goal_id"] is None

    def test_unknown_campaign_type_does_not_crash(self):
        """Новый тип кампании должен давать «не видно», а не падение."""
        s = _extract_strategy({"SomeFutureCampaign": {"BiddingStrategy": {}}})
        assert s["optimizes_for_goal"] is False
