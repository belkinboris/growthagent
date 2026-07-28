"""
Готовность выводов: когда уже будет понятно.

Владелец видит «событий слишком мало» в сравнении недель, «доля оплат
случайна» в источниках и «данных мало, вывод осторожный» в сигналах — и
у него законный вопрос: а когда станет достаточно? Раньше ответа не было
нигде, и ожидание выглядело бесконечным.

Второе, ради чего этот модуль: пороги уверенности разъехались по трём
файлам. Продукт обязан говорить об уверенности одинаково во всех местах,
иначе «мало данных» на одном экране и «данных достаточно» на другом —
про одни и те же числа.

Честность здесь важнее полезности. Окно наблюдения всегда семь дней, и
если за неделю набирается меньше нужного, ждать бесполезно: через месяц
в окне будет ровно та же неделя. Так и написано — вместо ободряющего
«скоро будет видно».
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Ниже этого числа событий разница почти наверняка случайна: одно событие
# туда-сюда переворачивает картину. Порог сравнения недель и нижняя
# граница confidence -- это он.
MIN_FOR_A_TREND = 3

# С этого числа вывод считается опирающимся на данные, а не на догадку:
# «10 из 10» и «1 из 1» -- это не одна и та же стопроцентная конверсия.
ENOUGH_FOR_A_CONCLUSION = 10

# Окно анализа. Всё, что платформа считает «за неделю», считается ровно
# по нему -- отсюда и вывод про бесполезность ожидания.
WINDOW_DAYS = 7


@dataclass(frozen=True)
class Check:
    """Один вопрос, на который аналитик отвечает или пока не может."""

    key: str
    question: str
    metric: str          # ключ воронки, по которому считается выборка
    needed: int
    weeks_needed: int    # сколько недель наблюдений нужно самому методу
    why: str             # почему именно столько -- человеку, а не разработчику


CHECKS: tuple[Check, ...] = (
    Check(
        key="product", question="Где именно теряются люди", metric="signup",
        needed=ENOUGH_FOR_A_CONCLUSION, weeks_needed=1,
        why="Доля от трёх-четырёх человек скачет на десятки процентов "
            "от одного случайного отказа.",
    ),
    Check(
        key="week_over_week", question="Стало лучше или хуже за неделю", metric="signup",
        needed=MIN_FOR_A_TREND, weeks_needed=2,
        why="Сравнивать надо с чем-то: нужна вторая неделя, посчитанная "
            "тем же способом.",
    ),
    Check(
        key="payments", question="Что мешает доходить до оплаты", metric="payment_started",
        needed=MIN_FOR_A_TREND, weeks_needed=1,
        why="По одной попытке оплаты нельзя отличить поломку от того, "
            "что человек передумал.",
    ),
)


def assess(check: Check, weekly_value: int | None, observed_days: float | None) -> dict:
    """Готов ли вывод и, если нет, чего ждать.

    weekly_value -- сколько событий в недельном окне сейчас,
    observed_days -- сколько дней аналитик вообще наблюдает проект.
    """
    row = {
        "key": check.key, "question": check.question, "metric": check.metric,
        "needed": check.needed, "have": weekly_value, "why": check.why,
        "ready": False,
    }

    if weekly_value is None or observed_days is None:
        return {**row, "verdict": "Аналитик ещё не собрал данные по этому шагу."}

    days_needed = check.weeks_needed * WINDOW_DAYS
    # Окно ещё не заполнилось: то, что в нём лежит, -- это не недельный
    # результат, и сравнивать его с порогом напрямую нельзя.
    if observed_days < days_needed:
        left = max(1, math.ceil(days_needed - observed_days))
        expected = _projected(weekly_value, observed_days)
        if expected >= check.needed:
            return {**row, "verdict": f"Наблюдаем {_days(observed_days)} из {days_needed}. "
                                      f"Если темп сохранится, данных хватит — "
                                      f"осталось ещё {_days(left)}."}
        return {**row, "verdict": f"Наблюдаем {_days(observed_days)} из {days_needed}. "
                                  f"При нынешнем темпе за неделю наберётся около "
                                  f"{expected} из нужных {check.needed}."}

    if weekly_value >= check.needed:
        return {**row, "ready": True,
                "verdict": f"Данных достаточно: {weekly_value} за неделю "
                           f"при нужных {check.needed}."}

    if weekly_value == 0:
        return {**row, "verdict": "За неделю не случилось ни одного такого события — "
                                  "срок оценить не по чему."}

    # Главная честность модуля: окно всегда семь дней, и «подождать ещё»
    # тут не работает -- через месяц в окне будет ровно такая же неделя.
    times = math.ceil(check.needed / weekly_value)
    return {**row, "verdict": f"За неделю набирается {weekly_value} из нужных {check.needed}. "
                              f"Ждать дольше не поможет: окно всегда семь дней. Нужно, чтобы "
                              f"таких событий стало примерно в {_times(times)} больше."}


def _times(n: int) -> str:
    """«в 5 раз», а не «в 5 раза»."""
    tail, last = n % 100, n % 10
    if 11 <= tail <= 14 or last == 0 or last >= 5 or last == 1:
        return f"{n} раз"
    return f"{n} раза"


def _projected(weekly_value: int, observed_days: float) -> int:
    """Сколько наберётся за полную неделю, если темп сохранится."""
    return int(round(weekly_value / max(observed_days, 1.0) * WINDOW_DAYS))


def _days(count) -> str:
    """«3 дня», а не «3 дней»: по таким мелочам текст читается машинным."""
    n = int(count)
    tail = n % 100
    last = n % 10
    if 11 <= tail <= 14 or last == 0 or last >= 5:
        word = "дней"
    elif last == 1:
        word = "день"
    else:
        word = "дня"
    return f"{n} {word}"
