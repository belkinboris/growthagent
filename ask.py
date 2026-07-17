"""
TruePost playbook -- проектно-специфичный контент рекомендаций Growth Loop.

Ядро (app/growth_loop.py) определяет ОБЛАСТЬ узкого места по порогам;
этот модуль превращает область в конкретную рекомендацию: действие,
change set, критерии успеха/провала, что запрещено менять.

Здесь и только здесь живут: названия шагов TruePost, тексты owner-facing
рекомендаций, конкретные change set'ы (в т.ч. «очередь постов на неделю»).
Другой продукт = другой playbook, ядро не трогается.

Простой русский язык. Без confidence interval, decision engine, PMF score.
Growth Loop только предлагает -- ничего не применяет автоматически.
"""

from __future__ import annotations

from typing import Optional


def _n(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def truepost_playbook(area: str, payment_path: dict, thresholds: dict) -> Optional[dict]:
    """
    Возвращает контент рекомендации для области или None (в этой области
    сейчас нечего предлагать -- например tracking чинится не рекомендацией).

    Обязательные ключи: title, action.
    Опциональные: hypothesis, change_set, expected_effect, risk, measure,
    primary_metric, sample_metric, target_sample, min/max_runtime_days,
    success_criterion, failure_criterion, locked_variables, confidence,
    extra_evidence.
    """
    builder = _AREA_BUILDERS.get(area)
    if builder is None:
        return None
    return builder(payment_path, thresholds)


# ---------------------------------------------------------------------------
# Области
# ---------------------------------------------------------------------------

def _collect_data(pp: dict, t: dict) -> dict:
    regs = _n(pp.get("registrations"))
    need = t["min_registrations"]
    return {
        "title": "Добрать регистрации для решения",
        "action": (
            "Продуктовый вывод по текущим данным делать нельзя. Добираем "
            "регистрации рекламой, продукт не трогаем: проверить, что все "
            "16 объявлений Telegram Ads активны и размечены utm_content; "
            "Директ оставить как есть."
        ),
        "hypothesis": "При текущих настройках рекламы наберём выборку для продуктового вывода за 7–14 дней.",
        "change_set": [
            "проверить активность всех объявлений Telegram Ads (8 сегментов × 2 текста)",
            "убедиться, что у каждого объявления свой utm_content",
            "Директ: не менять ставки, бюджет и группы",
            "TruePost: ничего не менять",
        ],
        "expected_effect": f"с {regs} до ≥ {need} регистраций в недельном окне",
        "risk": "низкий: продукт и ставки не меняются",
        "measure": "registrations и разбивка по источникам в /ads",
        "primary_metric": "channels_created",
        "sample_metric": "registrations",
        "target_sample": max(need - regs, 10),
        "min_runtime_days": 3,
        "max_runtime_days": 14,
        "success_criterion": f"registrations в окне ≥ {need}",
        "failure_criterion": "за 14 дней выборка не набрана — пересматриваем источники",
        "locked_variables": ["ставки Директа", "бюджет", "тарифы", "цены", "лендинг", "генератор"],
        "confidence": "данных мало — это шаг сбора, не продуктовый вывод",
    }


def _collect_feedback(pp: dict, t: dict) -> dict:
    fb = _n(pp.get("first_post_feedback_good")) + _n(pp.get("first_post_feedback_bad"))
    need = t["min_feedback"]
    return {
        "title": "Собрать отзывы о первом посте",
        "action": (
            "Каналы создают, но отзывов о первом посте мало — не видно, "
            "нравится ли результат. Ничего не менять, дать блоку отзыва "
            "собрать данные. Смотреть /journeys: доходят ли пользователи "
            "до первого поста вообще."
        ),
        "hypothesis": f"На следующих пользователях соберём ≥ {need} отзывов без изменений продукта.",
        "change_set": [
            "TruePost: ничего не менять",
            "рекламу не трогать",
            "ежедневно смотреть live feed: где пользователи останавливаются до отзыва",
        ],
        "expected_effect": f"с {fb} до ≥ {need} отзывов",
        "risk": "низкий",
        "measure": "first_post_feedback_good/bad в /board",
        "primary_metric": "pricing_viewed",
        "sample_metric": "registrations",
        "target_sample": 14,
        "min_runtime_days": 3,
        "max_runtime_days": 14,
        "success_criterion": f"отзывов ≥ {need}",
        "failure_criterion": "отзывы не собираются при росте регистраций — чинить сам блок отзыва",
        "locked_variables": ["генератор", "onboarding", "тарифы", "цены", "реклама"],
        "confidence": "данных недостаточно — шаг сбора",
    }


def _onboarding(pp: dict, t: dict) -> dict:
    regs = _n(pp.get("registrations"))
    channels = _n(pp.get("channels_created"))
    return {
        "title": "Чиним onboarding до создания канала",
        "action": (
            "Пройти путь нового пользователя от регистрации до создания "
            "канала самому и по /journeys найти шаг, где уходят. Затем — "
            "одно упрощение этого шага (не редизайн)."
        ),
        "hypothesis": "Одно упрощение первого шага поднимет долю создавших канал.",
        "change_set": [
            "пройти регистрацию → канал глазами нового пользователя",
            "по /journeys выписать, на каком шаге застревают",
            "упростить ровно один шаг (текст/поле/кнопку)",
            "рекламу, тарифы, генератор не трогать",
        ],
        "expected_effect": f"создание канала с {channels}/{regs} к ≥ {t['low_channel_rate']:.0%}",
        "risk": "средний: правка UX первого шага",
        "measure": "channels_created / registrations",
        "primary_metric": "channels_created",
        "sample_metric": "registrations",
        "target_sample": 14,
        "min_runtime_days": 3,
        "max_runtime_days": 14,
        "success_criterion": f"доля создавших канал ≥ {t['low_channel_rate']:.0%} на новых пользователях",
        "failure_criterion": "доля не выросла — возвращаем как было, ищем другой шаг",
        "locked_variables": ["генератор", "тарифы", "цены", "реклама", "лендинг"],
    }


def _first_post(pp: dict, t: dict) -> dict:
    good = _n(pp.get("first_post_feedback_good"))
    bad = _n(pp.get("first_post_feedback_bad"))
    reasons = pp.get("first_post_feedback_reasons") or {}
    top_reason = max(reasons, key=reasons.get) if reasons else "нет данных по причинам"
    return {
        "title": "Чиним качество первого поста",
        "action": (
            f"Отзывы подтверждают проблему первого результата (bad {bad} из {good + bad}). "
            f"Главная причина: «{top_reason}». Снимаем запрет с генератора и делаем "
            "ОДНУ итерацию промпта под эту причину. Больше ничего не менять."
        ),
        "hypothesis": "Одна адресная правка промпта под топ-причину снизит долю bad.",
        "change_set": [
            f"одна итерация промпта генератора под причину «{top_reason}»",
            "остальные причины не трогать в этой итерации",
            "onboarding, тарифы, рекламу не менять",
        ],
        "expected_effect": "доля bad ниже порога на следующих отзывах",
        "risk": "средний: правка генератора, откат = вернуть прежний промпт",
        "measure": "доля good среди НОВЫХ отзывов (после правки)",
        "primary_metric": "first_post_feedback_good",
        "sample_metric": "first_post_feedback_total",
        "target_sample": 10,
        "min_runtime_days": 3,
        "max_runtime_days": 14,
        "success_criterion": f"bad < {t['bad_feedback_share']:.0%} на новых отзывах",
        "failure_criterion": "bad не снизился — откатить промпт, разбирать причину глубже",
        "locked_variables": ["onboarding", "тарифы", "цены", "реклама", "лендинг"],
        "extra_evidence": [f"причины bad: {reasons}" if reasons else "причины bad не заполнены"],
    }


def _commercial_bridge(pp: dict, t: dict) -> dict:
    regs = _n(pp.get("registrations"))
    pricing = _n(pp.get("pricing_viewed"))
    good = _n(pp.get("first_post_feedback_good"))
    fb = good + _n(pp.get("first_post_feedback_bad"))
    base_rate = (pricing / regs) if regs else 0.0
    return {
        "title": "Тестируем очередь постов на неделю",
        "action": (
            "После положительного отзыва о первом посте показать предложение "
            "«Собрать очередь из 7 постов на неделю» с превью тем и переходом "
            "к тарифам. Спецификация правки TruePost — отдельным файлом, "
            "внедряется только после подтверждения."
        ),
        "hypothesis": (
            "Платная ценность — регулярное ведение канала, а не один пост. "
            "Мост «очередь на неделю» поднимет переход к тарифам."
        ),
        "change_set": [
            "TruePost: блок «Собрать очередь на неделю» после good feedback (по спецификации)",
            "генератор первого поста не менять",
            "тарифы и цены не менять",
            "рекламу не менять",
        ],
        "expected_effect": f"переход к тарифам с {base_rate:.0%} к ≥ {max(t['min_pricing_rate'], base_rate * 2):.0%}",
        "risk": "низкий: добавление блока, откат = скрыть блок",
        "measure": "pricing_viewed и payment_started на новых пользователях",
        "primary_metric": "pricing_viewed",
        "sample_metric": "registrations",
        "target_sample": 14,
        "min_runtime_days": 3,
        "max_runtime_days": 14,
        "success_criterion": f"pricing_viewed на новых пользователях заметно выше {base_rate:.0%}",
        "failure_criterion": "переход к тарифам не вырос — мост другой, блок убрать",
        "locked_variables": ["реклама", "цены", "тарифы", "генератор первого поста", "onboarding"],
        "extra_evidence": [f"отзывы: {good} из {fb} положительные"],
    }


def _pricing_screen(pp: dict, t: dict) -> dict:
    pricing = _n(pp.get("pricing_viewed"))
    return {
        "title": "Чиним тарифный экран",
        "action": (
            f"Тарифы открыли {pricing} раз, оплату не начал никто. Пройти "
            "тарифный экран самому с телефона; переписать заголовок ценности "
            "(«канал ведётся сам N постов в месяц», не «N постов»); проверить, "
            "что кнопка оплаты работает."
        ),
        "hypothesis": "Ценность на тарифном экране не считывается — правка формулировки даст первые payment_started.",
        "change_set": [
            "пройти тарифный экран вручную (телефон)",
            "переписать заголовок ценности одного экрана",
            "цены и состав тарифов НЕ менять",
            "рекламу и генератор не трогать",
        ],
        "expected_effect": "первые payment_started",
        "risk": "низкий: текстовая правка",
        "measure": "payment_started / pricing_viewed",
        "primary_metric": "payment_started",
        "sample_metric": "pricing_viewed",
        "target_sample": 10,
        "min_runtime_days": 3,
        "max_runtime_days": 14,
        "success_criterion": "payment_started ≥ 1 на следующих 10 открытиях тарифов",
        "failure_criterion": "0 стартов оплаты — проблема глубже формулировки (цена/доверие)",
        "locked_variables": ["цены", "состав тарифов", "реклама", "генератор", "onboarding"],
    }


def _payment_path(pp: dict, t: dict) -> dict:
    started = _n(pp.get("payment_started"))
    return {
        "title": "Чиним путь оплаты",
        "action": (
            f"Оплату начали {started} раз, успешных нет. Проверить логи YooKassa "
            "по этим попыткам и пройти оплату самому на минимальном тарифе."
        ),
        "hypothesis": "Потеря происходит внутри платёжного шага, а не в ценности.",
        "change_set": [
            "поднять логи YooKassa по каждой попытке",
            "пройти оплату самому",
            "исправить найденную техническую причину",
            "тарифы, цены, рекламу не менять",
        ],
        "expected_effect": "первая payment_success",
        "risk": "низкий",
        "measure": "payment_success / payment_started",
        "primary_metric": "payment_success",
        "sample_metric": "payment_started",
        "target_sample": 3,
        "min_runtime_days": 1,
        "max_runtime_days": 14,
        "success_criterion": "payment_success ≥ 1",
        "failure_criterion": "оплаты падают и после фикса — разбирать провайдера",
        "locked_variables": ["цены", "тарифы", "реклама", "генератор"],
    }


def _scale(pp: dict, t: dict) -> dict:
    success = _n(pp.get("payment_success"))
    return {
        "title": "Проверяем экономику привлечения",
        "action": (
            f"Оплат: {success}. Посчитать стоимость привлечения платящего по "
            "источникам (/ads) и удвоить тестовый бюджет лучшего источника, "
            "не трогая остальное."
        ),
        "hypothesis": "Путь до оплаты повторяется — можно осторожно масштабировать лучший источник.",
        "change_set": [
            "посчитать CPA платящего по источникам",
            "+тестовый бюджет только лучшему источнику",
            "продукт не менять во время теста",
        ],
        "expected_effect": "повторные оплаты при приемлемом CPA",
        "risk": "средний: рост расходов",
        "measure": "payment_success по источникам и CPA",
        "primary_metric": "payment_success",
        "sample_metric": "registrations",
        "target_sample": 30,
        "min_runtime_days": 7,
        "max_runtime_days": 21,
        "success_criterion": "новые оплаты из масштабируемого источника",
        "failure_criterion": "оплаты не повторяются — вернуть бюджет, смотреть путь платящих",
        "locked_variables": ["продукт целиком", "цены"],
    }


_AREA_BUILDERS = {
    "collect_data": _collect_data,
    "collect_feedback": _collect_feedback,
    "onboarding": _onboarding,
    "first_post": _first_post,
    "commercial_bridge": _commercial_bridge,
    "pricing_screen": _pricing_screen,
    "payment_path": _payment_path,
    "scale": _scale,
    # tracking: чинится не рекомендацией владельцу, а техработой -- None.
}


# ---------------------------------------------------------------------------
# Факты о проекте для разговорного слоя (app/ask.py). Здесь, а не в ask.py,
# потому что это TruePost-специфичное знание. Обновлять при изменениях.
# ---------------------------------------------------------------------------

PROJECT_FACTS = """ФАКТЫ О ПРОЕКТЕ (обновлено 2026-07-07):
— Продукт: АвтоПост — ИИ-сервис регулярного ведения Telegram-канала. \
Пользователь получает первый пост бесплатно ещё до подключения канала; \
платная ценность — регулярное ведение (очередь постов), не разовый текст.
— Монетизация: месячная подписка, тарифы Старт / Про / Бизнес / Агентство \
(различаются числом каналов и лимитом постов). ВАЖНО: актуальных цен в \
данных бота НЕТ — если владелец спрашивает про конкретные цены или их \
изменение, честно скажи, что цифр цен у тебя нет, и не выдумывай их.
— Источники трафика: Яндекс Директ (поиск, одна кампания, активен, \
недельный бюджет недавно поднят); Telegram Ads через eLama — ПОСТАВЛЕН НА \
ПАУЗУ 2026-07-07 из-за низкого качества (~1 регистрация при большом расходе).
— Публикация постов в канал требует подтверждения пользователя, если он \
сам не включил автопубликацию.
— Мост к тарифам: блок «Собрать очередь на неделю» показывается после \
хорошего отзыва о первом посте (queue_offer_shown/clicked в числах).
— Известная особенность данных: генераций у неподключённых каналов сильно \
больше, чем у подключённых — люди пробуют продукт до подключения канала."""
