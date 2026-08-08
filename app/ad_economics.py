"""
Окупается ли реклама (задача R11).

Зачем этот модуль появился. Владелец АвтоПоста пришёл с живыми числами:
в Директе стоит цель по цене 300 ₽ за конверсию, набралось 65 регистраций
и 2 оплаты — и ощущение «где-то косяк, я теряю деньги». Платформа на тот
момент считала только цену РЕГИСТРАЦИИ и молчала: 300 ₽ за регистрацию
выглядят прилично, правило `spend_no_signups` не срабатывает (регистрации
же есть), а Growth Loop занимался качеством первого поста.

Косяк был не в цене регистрации, а в том, что её вообще меряли как цель.
Бизнес живёт с оплат, и главный вопрос — сколько стоит ОПЛАТА и сколько
она приносит. Модуль отвечает на него и на следующий, более полезный:
**сколько вообще можно платить за регистрацию, чтобы не терять деньги.**

Это чистая арифметика, без LLM и без обращений в сеть: на вход — уже
собранные числа, на выходе — вывод и текст для владельца.

Честность про выборку здесь критична вдвойне. Две оплаты — это две
оплаты, а не «конверсия 3.1%»: одна оплата туда-сюда меняет ответ в
полтора раза. Поэтому доля оплат считается не точкой, а интервалом
(Уилсон, см. `_wilson_interval`), и предельная цена регистрации выдаётся
диапазоном. Точное число здесь было бы враньём с тремя знаками после
запятой — ровно тот «уверенный неверный ответ», от которого продукт
отличается (см. принцип 1 в PRODUCT_ROADMAP.md).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from app.readiness import ENOUGH_FOR_A_CONCLUSION, MIN_FOR_A_TREND

# Ниже этого расхода разговор про окупаемость бессмысленен: на трёхстах
# рублях любая арифметика — шум, а не вывод.
MIN_SPEND_FOR_A_VERDICT = 1000.0

# z для 95% интервала. Не настройка -- фиксированный уровень доверия,
# одинаковый во всех выводах продукта.
_Z = 1.96


def _wilson_interval(successes: int, trials: int) -> tuple[float, float]:
    """
    Интервал Уилсона для доли. Взят вместо «обычной» формулы
    (p ± z·sqrt(p(1-p)/n)) сознательно: на малых выборках и долях у нуля
    обычная формула даёт отрицательную нижнюю границу и неприлично узкий
    интервал — то есть врёт именно там, где владелец на неё смотрит.
    Уилсон на 2 из 65 честно отвечает «где-то от 1% до 10%».
    """
    if trials <= 0:
        return 0.0, 1.0
    p = successes / trials
    denominator = 1 + _Z ** 2 / trials
    centre = (p + _Z ** 2 / (2 * trials)) / denominator
    half = (_Z / denominator) * math.sqrt(
        p * (1 - p) / trials + _Z ** 2 / (4 * trials ** 2)
    )
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass
class AdEconomics:
    """Ответ на вопрос «окупается ли реклама». `ok=False` -- считать нечем."""

    ok: bool = False
    hint: str = ""

    spend: float = 0.0
    registrations: int = 0
    payments: int = 0
    revenue: Optional[float] = None

    cost_per_registration: Optional[float] = None
    cost_per_payment: Optional[float] = None
    revenue_per_payment: Optional[float] = None

    # Сколько можно платить за регистрацию, чтобы выходить в ноль.
    # Диапазон, а не число -- см. docstring модуля.
    affordable_cpr_low: Optional[float] = None
    affordable_cpr_high: Optional[float] = None

    result: Optional[float] = None          # выручка минус расход
    headline: str = ""
    verdict: str = ""                       # "теряем" | "окупается" | "рано судить"
    evidence: list = field(default_factory=list)
    action: str = ""
    confidence_note: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "hint": self.hint,
            "spend": round(self.spend, 2),
            "registrations": self.registrations, "payments": self.payments,
            "revenue": None if self.revenue is None else round(self.revenue, 2),
            "cost_per_registration": _round_or_none(self.cost_per_registration),
            "cost_per_payment": _round_or_none(self.cost_per_payment),
            "revenue_per_payment": _round_or_none(self.revenue_per_payment),
            "affordable_cpr_low": _round_or_none(self.affordable_cpr_low),
            "affordable_cpr_high": _round_or_none(self.affordable_cpr_high),
            "result": _round_or_none(self.result),
            "headline": self.headline, "verdict": self.verdict,
            "evidence": list(self.evidence), "action": self.action,
            "confidence_note": self.confidence_note,
        }


def _round_or_none(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 2)


def _money(value: float) -> str:
    """Деньги владельцу — без копеек: «19 500 ₽», а не «19500.00 руб.»."""
    return f"{value:,.0f} ₽".replace(",", " ")


def _times(n: int) -> str:
    """
    «в 6 раз», «в 2 раза», «в 21 раз». Согласование числительного пишется
    руками, потому что автоматический вывод даёт «в 6 раза» — а владелец
    уже отдельно жаловался на корявые фразы в интерфейсе.
    """
    if 11 <= n % 100 <= 14:
        return f"{n} раз"
    last = n % 10
    if last == 1:
        return f"{n} раз"
    if last in (2, 3, 4):
        return f"{n} раза"
    return f"{n} раз"


def analyse(
    spend: Optional[float],
    registrations: Optional[int],
    payments: Optional[int],
    revenue: Optional[float] = None,
    target_cpa: Optional[float] = None,
) -> AdEconomics:
    """
    Считает экономику рекламы за окно наблюдения.

    `target_cpa` -- цена конверсии, выставленная в стратегии Директа (если
    её удалось прочитать). Именно её сравнение с предельной ценой
    регистрации и объясняет владельцу, откуда берётся убыток.
    """
    spend = float(spend or 0)
    registrations = int(registrations or 0)
    payments = int(payments or 0)

    if spend < MIN_SPEND_FOR_A_VERDICT:
        return AdEconomics(ok=False, hint=(
            f"Пока потрачено меньше {_money(MIN_SPEND_FOR_A_VERDICT)} — на такой сумме "
            "считать окупаемость рано, любая цифра будет случайной."
        ))
    if registrations <= 0:
        return AdEconomics(ok=False, hint=(
            "Расход есть, а регистраций нет ни одной — это отдельная проблема, "
            "она разбирается в сигналах, а не в окупаемости."
        ))

    eco = AdEconomics(
        ok=True, spend=spend, registrations=registrations, payments=payments,
        revenue=revenue,
    )
    eco.cost_per_registration = spend / registrations
    if payments > 0:
        eco.cost_per_payment = spend / payments
    if revenue is not None and payments > 0:
        eco.revenue_per_payment = revenue / payments
    if revenue is not None:
        eco.result = revenue - spend

    low, high = _wilson_interval(payments, registrations)

    # Предельная цена регистрации: сколько регистрация СТОИТ для бизнеса.
    # Считается только если известно, сколько приносит оплата -- без этого
    # числа любая «предельная цена» была бы выдумкой.
    if eco.revenue_per_payment is not None:
        eco.affordable_cpr_low = eco.revenue_per_payment * low
        eco.affordable_cpr_high = eco.revenue_per_payment * high

    _fill_verdict(eco, low, high, target_cpa)
    return eco


def _fill_verdict(eco: AdEconomics, low: float, high: float, target_cpa: Optional[float]) -> None:
    cpr = eco.cost_per_registration or 0

    eco.evidence.append(
        f"Потрачено {_money(eco.spend)}, получено {eco.registrations} регистраций — "
        f"по {_money(cpr)} за регистрацию."
    )

    if eco.payments == 0:
        eco.verdict = "теряем"
        eco.headline = "Реклама не принесла ни одной оплаты"
        eco.evidence.append(
            f"Из {eco.registrations} зарегистрировавшихся не заплатил никто. "
            f"Значит, все {_money(eco.spend)} пока потрачены впустую."
        )
        eco.action = (
            "Пока не появилась хотя бы одна оплата, увеличивать расход нельзя — "
            "вы платите за людей, которые не доходят до денег. Разберитесь "
            "сначала, почему из регистрации не получается оплата."
        )
        eco.confidence_note = (
            f"{eco.registrations} регистраций — этого уже достаточно, чтобы "
            "заметить отсутствие оплат, но не чтобы назвать причину."
            if eco.registrations >= ENOUGH_FOR_A_CONCLUSION else
            f"Регистраций всего {eco.registrations} — вывод предварительный."
        )
        return

    eco.evidence.append(
        f"Оплат — {eco.payments}, то есть одна оплата обошлась в "
        f"{_money(eco.cost_per_payment or 0)}."
    )

    if eco.revenue_per_payment is None:
        eco.verdict = "рано судить"
        eco.headline = f"Оплата обходится в {_money(eco.cost_per_payment or 0)}"
        eco.evidence.append(
            "Сколько приносит одна оплата, продукт не сообщает — без этого "
            "нельзя сказать, окупается реклама или нет."
        )
        eco.action = (
            "Передайте платформе сумму оплат (поле выручки в отчёте продукта) — "
            f"тогда станет видно, {_money(eco.cost_per_payment or 0)} за оплату это "
            "прибыль или убыток."
        )
        eco.confidence_note = _sample_note(eco.payments)
        return

    # Дальше — самое полезное: сравнение того, что регистрация стоит, с тем,
    # сколько за неё платят.
    eco.evidence.append(
        f"Одна оплата приносит {_money(eco.revenue_per_payment)}."
    )
    eco.evidence.append(
        f"Платит {_share_phrase(low, high)} — поэтому регистрация стоит для бизнеса "
        f"{_money(eco.affordable_cpr_low or 0)}–{_money(eco.affordable_cpr_high or 0)}, "
        f"а покупаете вы её за {_money(cpr)}."
    )

    losing = (eco.result or 0) < 0
    overpaying = eco.affordable_cpr_high is not None and cpr > eco.affordable_cpr_high

    if losing:
        eco.verdict = "теряем"
        eco.headline = (
            f"Реклама забирает больше, чем приносит: минус {_money(abs(eco.result or 0))}"
        )
    elif overpaying:
        eco.verdict = "теряем"
        eco.headline = "За регистрацию платите больше, чем она приносит"
    else:
        eco.verdict = "окупается"
        eco.headline = f"Реклама окупается: плюс {_money(eco.result or 0)}"

    if overpaying:
        times = cpr / eco.affordable_cpr_high if eco.affordable_cpr_high else 0
        eco.evidence.append(
            f"Разрыв примерно в {_times(int(round(times)))} — это и есть источник "
            "убытка, а не отдельная ошибка в настройках."
        )

    eco.action = _build_action(eco, cpr, target_cpa, overpaying or losing)
    eco.confidence_note = _sample_note(eco.payments)


def _build_action(eco: AdEconomics, cpr: float, target_cpa: Optional[float],
                  bad: bool) -> str:
    if not bad:
        return (
            "Реклама себя окупает — можно осторожно увеличивать расход, "
            "проверяя после каждого шага, что цена оплаты не выросла."
        )

    ceiling = eco.affordable_cpr_high
    parts = []
    if ceiling is not None:
        parts.append(
            f"Снизьте цену, по которой покупаете регистрацию, минимум до "
            f"{_money(ceiling)} — сейчас {_money(cpr)}."
        )
    if target_cpa is not None:
        if ceiling is not None and target_cpa > ceiling:
            parts.append(
                f"В стратегии Директа стоит цель {_money(target_cpa)} за конверсию — "
                f"это выше потолка, значит система честно выполняет заведомо "
                f"убыточную задачу. Менять надо саму цель, а не ставки."
            )
        else:
            parts.append(
                f"Цель в стратегии Директа ({_money(target_cpa)}) в потолок укладывается — "
                "значит дело не в ней, а в том, сколько регистраций доходит до оплаты."
            )
    else:
        parts.append(
            "Проверьте, на какую цель и на какую цену настроена стратегия в Директе: "
            "если она оптимизируется под регистрацию, она будет исправно приводить "
            "тех, кто регистрируется и не платит."
        )
    parts.append(
        "Второй путь к тому же результату — поднять долю платящих: тогда "
        "регистрация станет дороже стоить, и нынешняя цена окажется приемлемой."
    )
    return " ".join(parts)


def _share_phrase(low: float, high: float) -> str:
    """
    «каждый 15-й–100-й» вместо «конверсия 3.1%». Доля в процентах на двух
    оплатах выглядит как измерение, хотя это оценка с огромным разбросом;
    «каждый N-й» читается как оценка и не создаёт ложной точности.
    """
    if high <= 0:
        return "никто"
    best = int(round(1 / high))   # оптимистичный край: платящих больше всего
    worst = int(round(1 / low)) if low > 0 else 0
    if worst <= 0 or worst > 500:
        return f"{best}-й в лучшем случае, а может и никто"
    return f"от каждого {best}-го до каждого {worst}-го"


@dataclass
class StrategyCheck:
    """Ответ на вопрос «на те ли деньги настроена стратегия Директа»."""

    ok: bool = False
    hint: str = ""
    findings: list = field(default_factory=list)   # по кампании: текст для владельца
    worst_target_price: Optional[float] = None     # самая высокая цель среди кампаний

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "hint": self.hint, "findings": list(self.findings),
            "worst_target_price": _round_or_none(self.worst_target_price),
        }


def check_strategy_goal(campaigns: Optional[list], goal_ids: Optional[dict],
                        affordable_ceiling: Optional[float] = None) -> StrategyCheck:
    """
    Сравнивает цель, под которую Директ оптимизирует кампанию, с целью,
    которая приносит деньги.

    `goal_ids` -- уже настроенный маппинг {ключ воронки: goal_id}, тот же,
    что используется для Метрики. Ключ `payment_success` -- это и есть
    «деньги»; всё остальное (регистрация, активация) -- промежуточные шаги.

    Почему это отдельная проверка, а не часть analyse(): она отвечает не
    «сколько мы теряем», а «почему»: стратегия может исправно выполнять
    поставленную задачу, и при этом задача поставлена на шаг, который
    денег не приносит. Без этого владелец ищет ошибку в ставках, хотя
    ошибка в постановке.
    """
    if not campaigns:
        return StrategyCheck(ok=False, hint=(
            "Настройки кампаний Директа пока не прочитаны — без них нельзя "
            "сказать, на какую цель работает реклама."
        ))
    if not goal_ids:
        return StrategyCheck(ok=False, hint=(
            "Не задано, какая цель Метрики означает оплату — без этого нельзя "
            "проверить, на те ли деньги настроена реклама."
        ))

    money_goal = goal_ids.get("payment_success")
    money_goal = str(money_goal) if money_goal is not None else None
    # Обратный словарь goal_id -> человеческое название шага, чтобы в тексте
    # для владельца стояло «регистрация», а не «цель 342157».
    step_by_goal = {str(v): k for k, v in goal_ids.items()}
    step_titles = {
        "signup": "регистрацию", "registrations": "регистрацию",
        "activation_1": "создание канала", "activation_2": "генерацию поста",
        "payment_started": "начало оплаты", "payment_success": "оплату",
    }

    check = StrategyCheck(ok=True)
    for campaign in campaigns:
        if not campaign.get("optimizes_for_goal"):
            continue
        goal_id = campaign.get("goal_id")
        price = campaign.get("target_price")
        name = campaign.get("name") or campaign.get("campaign_id") or "кампания"

        if price is not None:
            if check.worst_target_price is None or price > check.worst_target_price:
                check.worst_target_price = price

        if goal_id is None:
            check.findings.append(
                f"«{name}»: стратегия оптимизируется под цель, но какую именно — "
                "Директ не сообщил. Проверьте в кабинете вручную."
            )
            continue

        if money_goal is not None and goal_id == money_goal:
            check.findings.append(
                f"«{name}»: оптимизируется под оплату — это правильная цель."
                + (f" Цена цели {_money(price)}." if price is not None else "")
            )
            continue

        step = step_titles.get(step_by_goal.get(goal_id, ""), "промежуточный шаг")
        message = (
            f"«{name}»: реклама оптимизируется под {step}, а не под оплату. "
            "Директ честно приводит тех, кто делает этот шаг — платят они или "
            "нет, для него неважно."
        )
        if price is not None:
            message += f" Цена цели — {_money(price)}."
            if affordable_ceiling is not None and price > affordable_ceiling:
                message += (
                    f" Это выше того, что шаг приносит бизнесу "
                    f"({_money(affordable_ceiling)}), поэтому каждая такая "
                    "конверсия покупается в убыток."
                )
        check.findings.append(message)

    if not check.findings:
        return StrategyCheck(ok=False, hint=(
            "Ни одна кампания не работает на автостратегии с целью — "
            "проверять нечего: ставки задаются вручную."
        ))
    return check


def _sample_note(payments: int) -> str:
    if payments >= ENOUGH_FOR_A_CONCLUSION:
        return "Оплат достаточно, чтобы считать это измерением, а не догадкой."
    if payments >= MIN_FOR_A_TREND:
        return (
            f"Оплат всего {payments} — направление видно, точные числа будут "
            "гулять. Решение принимать можно, ждать «точной цифры» бессмысленно."
        )
    return (
        f"Оплат всего {payments} — это не статистика. Одна оплата в ту или "
        "другую сторону заметно меняет расчёт, поэтому вывод про убыток "
        "надёжен, а точная величина — нет."
    )
