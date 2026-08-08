"""
Тесты окупаемости рекламы (задача R11).

Случай владельца, с которого всё началось (АвтоПост, август 2026): в
стратегии Директа стоит цена конверсии 300 ₽, набралось 65 регистраций и
2 оплаты. Платформа тогда показывала «цена регистрации 300 ₽» и молчала,
хотя владелец терял деньги на каждой регистрации.

Главное, что здесь проверяется:
1. Вывод строится вокруг ОПЛАТЫ, а не регистрации.
2. Предельная цена регистрации выдаётся диапазоном, а не точкой -- на
   двух оплатах точное число было бы враньём.
3. Стратегия, оптимизирующая промежуточный шаг, названа причиной убытка.
"""
import pytest

from app.ad_economics import (
    MIN_SPEND_FOR_A_VERDICT, analyse, check_strategy_goal, _wilson_interval,
)

# Реальные числа владельца.
OWNER = {"spend": 19500.0, "registrations": 65, "payments": 2}


class TestOwnerCase:
    def test_verdict_is_loss_not_a_neutral_number(self):
        e = analyse(**OWNER, revenue=1000.0)
        assert e.ok
        assert e.verdict == "теряем"
        assert "минус" in e.headline.lower()

    def test_cost_per_payment_is_named_not_just_cost_per_registration(self):
        """Цена регистрации выглядела приемлемой -- убийственна цена оплаты."""
        e = analyse(**OWNER, revenue=1000.0)
        assert e.cost_per_payment == 9750.0
        assert any("9 750" in x for x in e.evidence)

    def test_affordable_price_is_a_range_not_a_point(self):
        """Точное число на двух оплатах -- ложная точность."""
        e = analyse(**OWNER, revenue=1000.0)
        assert e.affordable_cpr_low is not None
        assert e.affordable_cpr_high is not None
        assert e.affordable_cpr_low < e.affordable_cpr_high
        assert e.affordable_cpr_high < 300, "потолок обязан быть ниже уплаченной цены"

    def test_gap_between_paid_and_worth_is_stated(self):
        e = analyse(**OWNER, revenue=1000.0)
        assert any("300 ₽" in x and "стоит для бизнеса" in x for x in e.evidence)

    def test_action_names_the_ceiling_not_just_scolds(self):
        e = analyse(**OWNER, revenue=1000.0)
        assert "Снизьте" in e.action
        assert "₽" in e.action

    def test_target_cpa_above_ceiling_is_called_the_cause(self):
        e = analyse(**OWNER, revenue=1000.0, target_cpa=300)
        assert "убыточную" in e.action or "выше потолка" in e.action

    def test_two_payments_are_not_called_statistics(self):
        e = analyse(**OWNER, revenue=1000.0)
        assert "не статистика" in e.confidence_note


class TestHonestRefusal:
    def test_tiny_spend_refuses(self):
        e = analyse(spend=300, registrations=5, payments=1)
        assert not e.ok and "рано" in e.hint

    def test_no_registrations_is_a_different_problem(self):
        e = analyse(spend=5000, registrations=0, payments=0)
        assert not e.ok and "регистраций нет" in e.hint

    def test_without_revenue_no_profit_claim_is_made(self):
        """Не зная, сколько приносит оплата, нельзя объявлять убыток."""
        e = analyse(**OWNER)
        assert e.ok
        assert e.verdict == "рано судить"
        assert e.result is None
        assert "не сообщает" in " ".join(e.evidence)

    def test_zero_payments_is_stated_plainly(self):
        e = analyse(spend=5000, registrations=30, payments=0)
        assert e.verdict == "теряем"
        assert "ни одной оплаты" in e.headline.lower()
        assert "увеличивать расход нельзя" in e.action


class TestProfitableCase:
    def test_profit_is_recognised(self):
        e = analyse(spend=5000, registrations=100, payments=40, revenue=20000)
        assert e.verdict == "окупается"
        assert "плюс" in e.headline.lower()

    def test_profitable_advice_is_not_a_warning(self):
        e = analyse(spend=5000, registrations=100, payments=40, revenue=20000)
        assert "окупает" in e.action


class TestWilson:
    def test_interval_contains_the_observed_share(self):
        low, high = _wilson_interval(2, 65)
        assert low < 2 / 65 < high

    def test_lower_bound_never_negative(self):
        """Обычная формула на 0 из 40 даёт отрицательную границу."""
        low, _ = _wilson_interval(0, 40)
        assert low >= 0

    def test_smaller_sample_gives_wider_interval(self):
        narrow = _wilson_interval(20, 650)
        wide = _wilson_interval(2, 65)
        assert (wide[1] - wide[0]) > (narrow[1] - narrow[0])


class TestStrategyGoal:
    CAMPAIGNS = [
        {"name": "Поиск", "campaign_id": "1", "optimizes_for_goal": True,
         "goal_id": "111", "target_price": 300.0},
        {"name": "РСЯ", "campaign_id": "2", "optimizes_for_goal": True,
         "goal_id": "999", "target_price": 450.0},
    ]
    GOALS = {"signup": 111, "payment_success": 999}

    def test_wrong_goal_is_named(self):
        c = check_strategy_goal(self.CAMPAIGNS, self.GOALS)
        assert c.ok
        assert any("не под оплату" in f for f in c.findings)

    def test_right_goal_is_not_scolded(self):
        c = check_strategy_goal(self.CAMPAIGNS, self.GOALS)
        assert any("правильная цель" in f for f in c.findings)

    def test_goal_id_is_never_shown_raw_to_the_owner(self):
        """«цель 111» владельцу ничего не говорит -- нужен русский шаг."""
        c = check_strategy_goal(self.CAMPAIGNS, self.GOALS)
        joined = " ".join(c.findings)
        assert "111" not in joined
        assert "регистрацию" in joined

    def test_price_above_ceiling_is_explained(self):
        c = check_strategy_goal(self.CAMPAIGNS, self.GOALS, affordable_ceiling=53.0)
        assert any("в убыток" in f for f in c.findings)

    def test_manual_bidding_is_not_a_finding(self):
        c = check_strategy_goal(
            [{"name": "Ручная", "optimizes_for_goal": False, "goal_id": None,
              "target_price": None}], self.GOALS)
        assert not c.ok and "вручную" in c.hint

    def test_without_campaigns_refuses_honestly(self):
        assert not check_strategy_goal(None, self.GOALS).ok

    def test_without_goal_mapping_refuses_honestly(self):
        assert not check_strategy_goal(self.CAMPAIGNS, None).ok


class TestRussianNumerals:
    """Владелец отдельно жаловался на корявые фразы -- «в 6 раза» недопустимо."""

    @pytest.mark.parametrize("n,expected", [
        (1, "1 раз"), (2, "2 раза"), (5, "5 раз"), (6, "6 раз"),
        (11, "11 раз"), (12, "12 раз"), (21, "21 раз"), (22, "22 раза"),
    ])
    def test_times_agreement(self, n, expected):
        from app.ad_economics import _times
        assert _times(n) == expected
