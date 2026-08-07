"""
Разбор воронки: связать шаги в цепочку и назвать корень (задача R7).

Зачем этот модуль. Раньше платформа показывала владельцу набор отдельных
находок: тут сигнал, там рекомендация, здесь проценты по шагам. Собирать
из этого вывод «где на самом деле теряются деньги» приходилось человеку
самому -- и он это делал, а платформа выглядела бесполезной. Задача
аналитика ровно обратная: принести готовый вывод, а сырьё оставить ниже
для тех, кто хочет проверить.

Ключевая мысль, которую модуль умеет выражать: **самый большой обрыв в
воронке и причина потерь -- часто разные места.** Классический случай на
АвтоПосте: формально люди отваливаются на тарифном экране (посмотрели
цены, но не нажали «выбрать»), а на деле им не понравился первый пост --
и на цену они смотрят уже без доверия к продукту. Красить кнопку в такой
ситуации бесполезно.

Поэтому разбор идёт в два шага:
1. Найти **видимый обрыв** -- шаг с худшей конверсией (арифметика).
2. Проверить, нет ли **выше по воронке качественного сигнала**, который
   этот обрыв объясняет: отзывы о результате. Если результат людям не
   нравится, дальнейшие шаги -- следствие, а не причина.

Никаких LLM: только детерминированные правила по данным, как и весь цикл
анализа (см. правила репозитория). Модуль ничего не решает за владельца --
он формулирует вывод и один следующий шаг, кнопку жмёт человек.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.readiness import ENOUGH_FOR_A_CONCLUSION, MIN_FOR_A_TREND
from app.vocabulary import feedback_reason_label

# Доля отрицательных отзывов, начиная с которой результат продукта считается
# проблемой, а не поводом для наблюдения. Не 50%: около половины -- это
# нормальный разброс мнений, а вот когда недовольных заметно больше
# довольных, дальше по воронке чинить нечего.
BAD_FEEDBACK_SHARE = 0.6

# Минимум отзывов, чтобы объявить качество продукта ПРИЧИНОЙ потерь.
# Именно порог вывода, а не порог тренда: «2 плохих против 1 хорошего» --
# это не 67% недовольных, это три человека, и строить на них разворот
# «чините продукт, а не кнопку» нельзя -- цена ошибки слишком велика.
MIN_FEEDBACK_FOR_CAUSE = ENOUGH_FOR_A_CONCLUSION

# Конверсия шага, ниже которой он считается кандидатом в обрыв. Выше --
# это рабочий шаг, даже если он худший из всех.
WEAK_STEP_CONVERSION = 0.5

# Порядок шагов воронки для разбора. Ключи -- нормализованные, как их
# отдаёт payment_path (см. app/connectors/payment_path.py).
CHAIN = [
    ("registrations", "зарегистрировались"),
    ("channels_created", "создали канал"),
    ("post_generations", "сгенерировали первый пост"),
    ("pricing_viewed", "посмотрели цены"),
    ("payment_cta_clicked", "нажали «выбрать тариф»"),
    ("payment_started", "начали оплату"),
    ("payment_success", "оплатили"),
]

# После этого шага человек уже видел результат продукта. Обрыв ПОСЛЕ него
# может объясняться качеством результата, обрыв ДО -- нет (человек ещё
# ничего не видел, и первый пост тут ни при чём).
QUALITY_JUDGED_AFTER = "post_generations"


@dataclass
class Step:
    key: str
    label: str
    value: int
    # Конверсия из предыдущего непустого шага. None -- предыдущего нет.
    conversion: Optional[float] = None
    is_worst: bool = False


@dataclass
class Diagnosis:
    """
    Готовый вывод для владельца. `headline` -- то единственное, что он
    обязан прочитать; всё остальное -- обоснование для тех, кто проверяет.
    """

    ok: bool
    headline: str = ""
    chain: list[Step] = field(default_factory=list)
    visible_break: Optional[str] = None      # человеческое название шага
    root_cause: Optional[str] = None         # None -- корень совпал с обрывом
    evidence: list[str] = field(default_factory=list)
    action: Optional[str] = None
    # «Почему этому можно верить» -- честно про размер выборки.
    confidence_note: str = ""
    hint: str = ""                            # заполняется, когда ok=False

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "headline": self.headline,
            "chain": [
                {"key": s.key, "label": s.label, "value": s.value,
                 "conversion": round(s.conversion * 100) if s.conversion is not None else None,
                 "is_worst": s.is_worst}
                for s in self.chain
            ],
            "visible_break": self.visible_break,
            "root_cause": self.root_cause,
            "evidence": self.evidence,
            "action": self.action,
            "confidence_note": self.confidence_note,
            "hint": self.hint,
        }


def _n(v) -> Optional[int]:
    """None остаётся None: «продукт не отдал этот шаг» и «шаг прошли ноль
    человек» -- разные вещи, и склеивать их в ноль нельзя."""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _top_reason(reasons: dict | None) -> Optional[tuple[str, int]]:
    """Самая частая причина недовольства: («не тот стиль», 26)."""
    if not isinstance(reasons, dict):
        return None
    merged: dict[str, int] = {}
    for key, count in reasons.items():
        try:
            count = int(count)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        label = feedback_reason_label(key)
        merged[label] = merged.get(label, 0) + count
    if not merged:
        return None
    return max(merged.items(), key=lambda kv: kv[1])


def _build_chain(pp: dict) -> list[Step]:
    steps: list[Step] = []
    prev_value: Optional[int] = None
    for key, label in CHAIN:
        value = _n(pp.get(key))
        if value is None:
            continue                      # продукт не отдал этот шаг
        step = Step(key=key, label=label, value=value)
        if prev_value:
            step.conversion = value / prev_value
        steps.append(step)
        prev_value = value
    return steps


def _worst_step(steps: list[Step]) -> Optional[Step]:
    """
    Худший переход воронки. Считаем только там, где предыдущий шаг набрал
    достаточно людей: «1 из 2» -- это не 50% обрыва, это два человека.
    Шаги с конверсией выше WEAK_STEP_CONVERSION вообще не считаем обрывом --
    иначе «худшим» объявляется вполне рабочий шаг просто потому, что он
    худший из хороших.
    """
    candidates = [
        s for s in steps
        if s.conversion is not None
        and s.conversion < WEAK_STEP_CONVERSION
    ]
    if not candidates:
        return None
    # Отбираем по абсолютной потере людей, а не по проценту: обрыв «80 -> 10»
    # важнее, чем «3 -> 0», даже если во втором случае процент хуже.
    def lost(s: Step) -> int:
        i = steps.index(s)
        return steps[i - 1].value - s.value if i > 0 else 0

    return max(candidates, key=lost)


def diagnose(payment_path: dict | None) -> Diagnosis:
    """
    Собирает разбор воронки из данных платёжного пути.

    Возвращает `ok=False` с человеческим `hint`, если данных не хватает --
    выдумывать вывод из пустоты нельзя, это прямо запрещено принципом
    честности данных.
    """
    if not payment_path:
        return Diagnosis(ok=False, hint=(
            "Разбор появится после первого удачного сбора: продукт пока не отдал "
            "данные о пути к оплате."
        ))

    steps = _build_chain(payment_path)
    if len(steps) < 2:
        return Diagnosis(ok=False, chain=steps, hint=(
            "Продукт отдаёт слишком мало шагов воронки, чтобы связать их в цепочку. "
            "Какие поля он присылает — видно на вкладке «Проекты»."
        ))

    first = steps[0]
    if first.value < MIN_FOR_A_TREND:
        return Diagnosis(ok=False, chain=steps, hint=(
            f"За неделю всего {first.value} — на таких числах любой вывод будет "
            "случайным. Разбор появится, когда людей станет больше."
        ))

    worst = _worst_step(steps)

    # Качественный сигнал: нравится ли людям результат продукта.
    fb_good = _n(payment_path.get("first_post_feedback_good")) or 0
    fb_bad = _n(payment_path.get("first_post_feedback_bad")) or 0
    fb_total = fb_good + fb_bad
    bad_share = (fb_bad / fb_total) if fb_total else 0.0
    quality_is_bad = fb_total >= MIN_FEEDBACK_FOR_CAUSE and bad_share >= BAD_FEEDBACK_SHARE
    top_reason = _top_reason(payment_path.get("first_post_feedback_reasons"))

    diag = Diagnosis(ok=True, chain=steps)
    for s in steps:
        s.is_worst = worst is not None and s.key == worst.key

    # Итог воронки в одну строку -- с него начинается любой разбор.
    last = steps[-1]
    diag.evidence.append(
        f"Из {first.value} на входе до шага «{last.label}» дошли {last.value}."
    )

    if worst is not None:
        idx = steps.index(worst)
        prev = steps[idx - 1]
        diag.visible_break = worst.label
        diag.evidence.append(
            f"Самый большой обрыв: {prev.value} {prev.label} → {worst.value} "
            f"{worst.label} ({round(worst.conversion * 100)}%)."
        )

    # Тот самый разворот: обрыв виден в одном месте, а причина выше.
    quality_upstream_of_break = (
        worst is not None
        and _index_of(steps, QUALITY_JUDGED_AFTER) is not None
        and steps.index(worst) > _index_of(steps, QUALITY_JUDGED_AFTER)
    )

    if quality_is_bad:
        reason_text = f", главная причина — «{top_reason[0]}»" if top_reason else ""
        diag.evidence.append(
            f"Первый пост людям не нравится: {fb_bad} «плохо» против {fb_good} «хорошо» "
            f"({round(bad_share * 100)}% недовольны){reason_text}."
        )
        if quality_upstream_of_break:
            diag.headline = (
                f"Деньги теряются не на шаге «{worst.label}», а раньше — "
                "людям не нравится первый пост."
            )
            diag.root_cause = (
                "Человек получает результат, который его не устраивает"
                + (f" ({top_reason[0]})" if top_reason else "")
                + f" — и дальше смотрит на цену уже без доверия. Поэтому обрыв "
                  f"виден на шаге «{worst.label}», а чинить надо то, что выше."
            )
            diag.action = (
                "Заняться качеством первого поста"
                + (f", начиная с причины «{top_reason[0]}»" if top_reason else "")
                + ". Тарифный экран и кнопку оплаты пока не трогать: пока результат "
                  "разочаровывает, их правка ничего не изменит."
            )
        else:
            diag.headline = "Главная проблема — качество первого поста."
            diag.root_cause = (
                "Большинству людей результат не нравится"
                + (f", чаще всего — «{top_reason[0]}»" if top_reason else "") + "."
            )
            diag.action = (
                "Разобраться, почему первый пост не нравится"
                + (f" — начните с «{top_reason[0]}»" if top_reason else "")
                + ", и починить это. Остальное подождёт."
            )
    elif worst is not None:
        idx = steps.index(worst)
        prev = steps[idx - 1]
        diag.headline = f"Главный обрыв — между «{prev.label}» и «{worst.label}»."
        diag.action = (
            f"Пройти этот шаг самому глазами нового человека и найти, что мешает "
            f"перейти от «{prev.label}» к «{worst.label}»."
        )
        if fb_total and fb_total < MIN_FEEDBACK_FOR_CAUSE:
            diag.evidence.append(
                f"Отзывов о результате пока {fb_total} — этого мало, чтобы понять, "
                "не в качестве ли продукта дело."
            )
    else:
        diag.headline = "Явного обрыва в воронке нет — люди доходят до конца."
        diag.action = (
            "Узкого места сейчас нет. Растить стоит вход: людей на первом шаге."
        )

    # Честность про выборку -- отдельной строкой, а не мелким шрифтом.
    if first.value < ENOUGH_FOR_A_CONCLUSION:
        diag.confidence_note = (
            f"Выборка мала ({first.value} на входе за неделю) — это направление, "
            "а не доказанный факт."
        )
    else:
        diag.confidence_note = f"Разбор по {first.value} людям за неделю."

    return diag


def _index_of(steps: list[Step], key: str) -> Optional[int]:
    for i, s in enumerate(steps):
        if s.key == key:
            return i
    return None
