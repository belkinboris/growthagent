"""
Куда движется продукт (задача R12).

Зачем этот модуль. Владелец сказал прямо: «АвтоПост на месте стоит уже
месяц, product-market fit не найден, а аналитик не помогает». И он прав
в главном: до R12 все правила платформы смотрели на ОДИН срез данных.
Тарифный экран, качество поста, цена регистрации — это оптимизация
воронки, которая уже есть. Ни одно правило не отвечало на вопрос уровнем
выше: «а продукт вообще движется?» Доска могла месяц подряд писать
«Ничего не требуется — всё в норме», пока рост стоял на нуле, — и это
была ложь по умолчанию.

Хуже того: аналитик, который видит только воронку, ведёт в тупик. Можно
идеально отполировать путь к оплате продукта, который не нужен рынку, и
каждая «удачная» проверка будет создавать ощущение прогресса. Поэтому
здесь есть и второй вывод: если проверки идут, а рост за месяц не
сдвинулся — проблема не внутри воронки, и следующая полировка не поможет.

Всё детерминировано: недельные точки уже собираются (снимки окна 7d),
LLM в цикле анализа не участвует (правило репозитория).

Честность на малых данных важнее вывода: «за месяц 4 регистрации в
неделю без роста» — это не «стагнация с трендом −3%», это «спрос не
найден», и так и пишем.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.readiness import MIN_FOR_A_TREND

# Сколько недельных точек нужно, чтобы говорить о курсе. Меньше трёх --
# любые две точки образуют "тренд" в ту сторону, куда качнулась случайность.
MIN_WEEKS_FOR_A_COURSE = 3

# Порог «стоит на месте»: последняя неделя в пределах ±25% от первой.
# Не наука, а защита от самообмана: недельные числа малого продукта шумят,
# и ±10% ловили бы шум, а не курс.
FLAT_BAND = 0.25

# Сколько событий в неделю нужно, чтобы тренд по метрике вообще имел смысл.
TINY_WEEKLY = MIN_FOR_A_TREND


@dataclass
class Course:
    """Ответ на вопрос «куда движется продукт за последние недели»."""

    ok: bool = False
    hint: str = ""

    weeks: int = 0
    # По каждой метрике: "растёт" | "стоит" | "падает" | "мало данных"
    registrations_trend: str = ""
    payments_trend: str = ""
    registrations_now: Optional[int] = None
    payments_now: Optional[int] = None

    stalled: bool = False          # главный вывод: продукт стоит на месте
    headline: str = ""
    evidence: list = field(default_factory=list)
    action: str = ""               # следующий шаг к PMF, конкретный
    dead_end_note: str = ""        # анти-тупик: «воронка не поможет», когда это так

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "hint": self.hint, "weeks": self.weeks,
            "registrations_trend": self.registrations_trend,
            "payments_trend": self.payments_trend,
            "registrations_now": self.registrations_now,
            "payments_now": self.payments_now,
            "stalled": self.stalled, "headline": self.headline,
            "evidence": list(self.evidence), "action": self.action,
            "dead_end_note": self.dead_end_note,
        }


def _trend(values: list) -> str:
    """
    Курс одной метрики по недельным точкам (старые -> новые).

    Сравниваем крайние точки, а не соседние: вопрос владельца -- «сдвинулось
    ли за месяц», а не «дёрнулось ли за неделю». Средние двух половин окна
    устойчивее к одной шумной неделе, чем просто первая и последняя точки.
    """
    clean = [v for v in values if v is not None]
    if len(clean) < MIN_WEEKS_FOR_A_COURSE:
        return "мало данных"
    if max(clean) < TINY_WEEKLY:
        # На 0-2 событиях в неделю тренд не существует как понятие.
        return "мало данных"
    half = len(clean) // 2
    early = sum(clean[:half]) / half
    late = sum(clean[-half:]) / half
    if early == 0:
        return "растёт" if late >= TINY_WEEKLY else "мало данных"
    change = (late - early) / early
    if change > FLAT_BAND:
        return "растёт"
    if change < -FLAT_BAND:
        return "падает"
    return "стоит"


def assess(weekly_points: list, finished_experiments: int = 0) -> Course:
    """
    weekly_points -- список недельных замеров, старые -> новые:
    [{"signup": int|None, "payment_success": int|None}, ...]
    Одна точка = одна неделя наблюдений (окно 7d, по одному снимку на неделю).

    finished_experiments -- сколько проверок Growth Loop завершилось за это
    же окно. Нужно для анти-тупикового вывода: проверки шли, а курс не
    сдвинулся -- значит, полировка воронки не то лекарство.
    """
    points = weekly_points or []
    course = Course(weeks=len(points))

    if len(points) < MIN_WEEKS_FOR_A_COURSE:
        course.hint = (
            f"Курс продукта станет виден после {MIN_WEEKS_FOR_A_COURSE} недель "
            f"наблюдений — сейчас {len(points)}. По одной-двум точкам любой "
            "«тренд» — случайность."
        )
        return course

    regs = [p.get("signup") for p in points]
    pays = [p.get("payment_success") for p in points]

    course.ok = True
    course.registrations_trend = _trend(regs)
    course.payments_trend = _trend(pays)
    course.registrations_now = regs[-1]
    course.payments_now = pays[-1]

    regs_clean = [v for v in regs if v is not None]
    pays_clean = [v for v in pays if v is not None]
    weeks_word = _weeks(len(points))

    # --- Главная развилка: движется или нет -------------------------------
    growing = "растёт" in (course.registrations_trend, course.payments_trend)
    if growing:
        course.headline = "Продукт движется"
        if course.payments_trend == "растёт":
            course.evidence.append(f"Оплаты растут: {_series(pays_clean)} по неделям.")
        if course.registrations_trend == "растёт":
            course.evidence.append(f"Регистрации растут: {_series(regs_clean)} по неделям.")
        course.action = (
            "Курс верный — не меняйте сейчас сразу несколько вещей, чтобы "
            "не потерять понимание, что именно работает."
        )
        return course

    course.stalled = True

    # --- Стоит на месте: разбираем, ГДЕ именно застой, и что это значит ---
    # Порядок веток важен: сначала самый тяжёлый диагноз (спроса нет),
    # потом «деньги не растут», потом общий застой.
    if course.registrations_trend == "мало данных" and max(regs_clean or [0]) < TINY_WEEKLY:
        course.headline = f"Продукт стоит на месте: новых людей почти нет"
        course.evidence.append(
            f"За {weeks_word} — {_series(regs_clean)} регистраций по неделям. "
            "Это не «плохая конверсия», это отсутствие входящего потока."
        )
        course.action = (
            "Пока в продукт не заходят люди, внутри него нечего улучшать. "
            "Следующий шаг — спрос: один новый канал за раз (другая площадка, "
            "другая формулировка боли в объявлении) и 5 разговоров с теми, "
            "кто по описанию должен был бы купить, но не пришёл. Вопрос им "
            "один: «как вы сейчас решаете эту задачу?»"
        )
    elif pays_clean and max(pays_clean) < TINY_WEEKLY and regs_clean and max(regs_clean) >= TINY_WEEKLY:
        course.headline = "Регистрации есть, деньги не появляются"
        course.evidence.append(
            f"За {weeks_word}: регистрации {_series(regs_clean)}, "
            f"оплаты {_series(pays_clean)} по неделям."
        )
        course.evidence.append(
            "Люди приходят и не доносят деньги — обычно это значит, что "
            "ценность до кошелька не дотягивает: продукт «интересный», "
            "но не «нужный»."
        )
        course.action = (
            "Самый быстрый путь к правде — не новая функция, а разговоры: "
            "5 человек, которые зарегистрировались и НЕ заплатили. Один вопрос: "
            "«что должно было случиться, чтобы вы заплатили?» И отдельно — "
            "те, кто заплатил (их мало, тем ценнее каждый): «за что именно "
            "вы платите?» Их слова — это и есть поиск product-market fit, "
            "никакая метрика их не заменит."
        )
    else:
        course.headline = f"Продукт стоит на месте {weeks_word}"
        course.evidence.append(
            f"Регистрации: {_series(regs_clean)} по неделям — {course.registrations_trend}."
        )
        if pays_clean:
            course.evidence.append(
                f"Оплаты: {_series(pays_clean)} по неделям — {course.payments_trend}."
            )
        course.action = (
            "Застой — это не «подождать ещё»: то, что делалось до сих пор, "
            "рост не даёт. Выберите одну гипотезу уровня продукта (не кнопки): "
            "другая аудитория, другая цена или другое обещание на первом "
            "экране — и проверьте её за неделю."
        )

    # --- Анти-тупик: воронку полируем, а курс не сдвинулся -----------------
    if finished_experiments > 0:
        course.dead_end_note = (
            f"За это время внутри воронки — {_experiments(finished_experiments)}, "
            "а курс не сдвинулся. Это важный сигнал: проблема не в том, КАК "
            "люди проходят продукт, а в том, ЗАЧЕМ им это. Дальнейшая "
            "полировка воронки сейчас — путь в тупик."
        )

    return course


def _series(values: list) -> str:
    """«4 → 5 → 4 → 3» — сами числа убедительнее слова «стоит»."""
    return " → ".join(str(int(v)) for v in values)


def _weeks(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return f"{n} недель"
    last = n % 10
    if last == 1:
        return f"{n} неделю"
    if last in (2, 3, 4):
        return f"{n} недели"
    return f"{n} недель"


def _experiments(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return f"{n} проверок"
    last = n % 10
    if last == 1:
        return f"{n} проверка"
    if last in (2, 3, 4):
        return f"{n} проверки"
    return f"{n} проверок"
