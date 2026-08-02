"""
Разговорный слой Аналитика Воронки.

Владелец пишет боту обычный текст ("почему упали регистрации?",
"что делать с тарифами?") -- бот отвечает через Anthropic API, подавая
в контекст СВОИ ЖЕ данные: доску, воронку, оплату, активный эксперимент,
динамику. Роль и принципы зашиты в системный промпт.

Границы (важно, это не изменение архитектуры принятия решений):
- LLM здесь ОТВЕЧАЕТ НА ВОПРОСЫ и объясняет данные. Он НЕ принимает
  решений, НЕ меняет эксперименты, НЕ трогает рекламу и продукт.
  Решения по-прежнему проходят только через Growth Loop и кнопки /board.
- Вызывается ТОЛЬКО на явное сообщение владельца (не на события,
  не в цикле) -- расход контролируем, принцип "no LLM per event" цел.
- Только admin chat_ids: вопросы стоят денег.
- При любой ошибке API -- честный fallback-текст, бот не падает.
"""

from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
# Yandex Cloud: два режима, как в generator.py АвтоПоста.
YANDEX_COMPLETION_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
YANDEX_RESPONSES_URL = "https://ai.api.cloud.yandex.net/v1/responses"
# Невидимые reasoning-токены DeepSeek съедают лимит вывода -- запас как в АвтоПосте.
YANDEX_REASONING_TOKENS_MARGIN = 8000
YANDEX_RETRIES = 3
MAX_QUESTION_CHARS = 1000
MAX_CONTEXT_CHARS = 9000     # доска+воронка+оплата+эксперимент обычно ~3-5k
MAX_ANSWER_TOKENS = 700
COOLDOWN_SECONDS = 3.0

_last_call_ts: float = 0.0

SYSTEM_PROMPT = """Ты — Аналитик Воронки, продакт-помощник владельца подключённого продукта. \
Платформа работает с любым продуктом, не только с тем, что описан в контексте ниже — \
не додумывай детали, которых там нет. Владелец общается по-простому, часто голосом.

Твои принципы (нарушать нельзя):
1. Данные важнее мнений. Каждый вывод опирайся на цифры из контекста ниже. \
Если данных нет — так и скажи: «в моих данных этого нет», не выдумывай.
2. Малые выборки — честные слова: «единичное событие», «предварительный \
сигнал», «данных недостаточно». Никогда не изображай уверенность на 3-5 событиях.
3. Одна переменная за раз. Если идёт эксперимент — напоминай, что заперто \
(«Не менять»), и отговаривай трогать эти переменные до вердикта.
4. Ты не применяешь изменения. Решения принимаются кнопками в интерфейсе, \
код меняется отдельно. Ты объясняешь и советуешь.
5. Простой русский. Без MBA-жаргона, без «confidence interval», без \
«decision engine». Коротко: 2-6 абзацев максимум, без списков где можно без них.
6. Если вопрос про то, «что делать» — сначала посмотри на активную \
рекомендацию/эксперимент в контексте: чаще всего ответ «дать текущему \
эксперименту довестись», и это правильный ответ, а не отписка.
7. Не льсти. Если владелец предлагает плохое (менять всё сразу, лить бюджет \
в неработающий канал, менять переменные во время теста) — скажи прямо и объясни почему.
8. Называй конкретный экран. Если цифра или вывод, о которых ты говоришь, есть на одной \
из карточек платформы (раздел «ЭКРАНЫ ПЛАТФОРМЫ» ниже) — назови вкладку и карточку \
(«на Обзоре, в карточке ...»), а не только само число: владелец должен уметь найти это глазами.

Тебе дают снимок текущих данных проекта. Отвечай на вопрос владельца."""

# Известный по имени продукт, для которого в playbook есть специфичные факты
# (см. app/truepost_playbook.PROJECT_FACTS). Ядро не знает продуктовой
# специфики -- поэтому эти факты нельзя подмешивать в контекст ЛЮБОГО
# подключённого проекта, только когда это действительно он.
_AUTOPOST_MARKERS = ("автопост", "truepost")


def _is_autopost_project(project) -> bool:
    haystack = f"{project.name or ''} {project.base_url or ''}".lower()
    return any(marker in haystack for marker in _AUTOPOST_MARKERS)


# Что где смотреть глазами -- статический список карточек интерфейса.
# Меняется вместе с интерфейсом (app/static/platform/index.html), поэтому
# лежит рядом с остальным разговорным контекстом, а не генерируется.
SCREENS_REFERENCE = """ЭКРАНЫ ПЛАТФОРМЫ (что где смотреть):
— Обзор: карточка «Неделя к неделе» — какой шаг воронки просел или вырос \
за последние 7 дней против предыдущих 7. «Откуда приходят те, кто платит» — \
доходят ли до оплаты люди из разных источников по-разному. «Когда выводам \
можно будет верить» — честный ответ, хватает ли данных для вывода или нужно ждать.
— Реклама: минус-фразы — список запросов, которые жгут бюджет и не приводят \
людей (человек решает сам, аналитик рекламу не трогает).
— История: «Что вы меняли в продукте» — сравнение чистой недели после отметки \
владельца о выкатке с неделей до неё. «Что делали руками» — журнал действий \
владельца. «История решений» — что предлагал аналитик и чем кончилось.
— Лента: путь конкретного (анонимного) человека, если обрыв не виден на средних числах.
— Отчёты: полные текстовые версии доски, воронки, пути к оплате."""


def _live_screens_summary(session, project, agent: str | None = None) -> str:
    """Те же числа и вердикты, что видит владелец на «Обзоре» сейчас.

    Считает теми же чистыми функциями, что и сами endpoint'ы (`_compare_row`,
    `_source_row`, `assess`) -- если поменяется порог малой выборки, чат
    и экран не разойдутся в оценке одних и тех же данных.

    `agent` сужает выдачу до карточек этого агента (чат конкретной вкладки
    не должен тонуть в чужих цифрах); без него -- общий чат, все карточки.
    """
    from datetime import timedelta

    from sqlmodel import select

    from app.models import MetricSnapshot, utcnow
    from app.platform_api import (
        _compare_row, _source_row, _sources_summary, load_stage_titles,
    )
    from app.connectors.traffic_sources import aggregate_by_label, parse_source_breakdown
    from app.readiness import CHECKS, assess
    from app.service import (
        PAYMENT_PATH_CACHE_PERIOD_KEY, extract_normalized_metrics_from_snapshot,
        get_cached_diagnostics,
    )

    lines: list[str] = []
    try:
        stage_titles = load_stage_titles(session, project.id)
        query = (
            select(MetricSnapshot)
            .where(MetricSnapshot.project_id == project.id)
            .where(MetricSnapshot.period_key == "7d")
            .where(MetricSnapshot.source.in_(("combined", "project_metrics_api")))
        )
        latest = session.exec(query.order_by(MetricSnapshot.created_at.desc())).first()
        first = session.exec(query.order_by(MetricSnapshot.created_at.asc())).first()
        previous = session.exec(
            query.where(MetricSnapshot.created_at <= utcnow() - timedelta(days=7))
                 .order_by(MetricSnapshot.created_at.desc())
        ).first()

        # Неделя к неделе и готовность выводов -- вкладка Диагноста.
        if agent in (None, "diagnostician") and latest is not None:
            now_values = extract_normalized_metrics_from_snapshot(latest)

            if previous is not None:
                was_values = extract_normalized_metrics_from_snapshot(previous)
                rows = [_compare_row(k, stage_titles.get(k, k), now_values.get(k), was_values.get(k))
                        for k in ("signup", "activation_1", "activation_2",
                                 "payment_started", "payment_success")
                        if now_values.get(k) is not None or was_values.get(k) is not None]
                if rows:
                    lines.append("КАРТОЧКА «НЕДЕЛЯ К НЕДЕЛЕ» (Обзор): " + "; ".join(
                        f"{r['title']}: {r['was']}→{r['now']} ({r['verdict']})" for r in rows))

            if first is not None:
                observed_days = (latest.created_at - first.created_at).total_seconds() / 86400.0
                readiness_rows = [assess(c, now_values.get(c.metric), observed_days) for c in CHECKS]
                lines.append("КАРТОЧКА «КОГДА ВЫВОДАМ МОЖНО БУДЕТ ВЕРИТЬ» (Обзор): " + "; ".join(
                    f"{r['question']} — {'готово' if r['ready'] else 'нет'}" for r in readiness_rows))

        # Источники трафика -- вкладка Маркетолога.
        if agent in (None, "marketer"):
            cached = get_cached_diagnostics(session, project.id, PAYMENT_PATH_CACHE_PERIOD_KEY)
            if cached is not None and cached.ok:
                breakdown = parse_source_breakdown(cached.result_json or {})
                if breakdown:
                    source_rows = [_source_row(label, data)
                                   for label, data in aggregate_by_label(breakdown).items()]
                    if source_rows:
                        lines.append('КАРТОЧКА «ОТКУДА ПРИХОДЯТ ТЕ, КТО ПЛАТИТ» (Обзор): '
                                    + _sources_summary(source_rows))
    except Exception:
        logger.exception("ask: live screens summary failed")

    return "\n".join(lines)


def build_context(session, project, agent: str | None = None) -> str:
    """
    Снимок данных для системного контекста: доска (+growth loop состояние),
    воронка, оплата, динамика. Всё из существующих builder'ов и кэшей --
    никаких новых запросов к TruePost. Устойчив к отсутствию любого куска.

    `agent` -- чат конкретной вкладки (diagnostician/marketer/product/tester).
    Общий контекст (доска, Growth Loop, как читать цифры) виден всем -- по
    нему принимаются решения, и урезать его ради экономии токенов означало
    бы, что чат вкладки не знает, что уже решено. Урезаются только большие
    предметные блоки (реклама, живые карточки источников/недели), которые
    другому агенту не нужны и только отвлекают модель.
    """
    from app import growth_loop
    from app.commercial_report import (
        build_board_report,
        build_dynamics_block,
        build_experiment_block,
        build_recommendation_details,
        build_verdict_block,
    )
    from app.service import (
        PAYMENT_PATH_CACHE_PERIOD_KEY,
        get_cached_diagnostics,
        load_daily_counters_history,
    )

    parts: list[str] = [f"ПРОЕКТ: {project.name}"]

    pp_dict = None
    try:
        pp_cached = get_cached_diagnostics(session, project.id, PAYMENT_PATH_CACHE_PERIOD_KEY)
        pp_dict = dict(pp_cached.result_json or {}) if (pp_cached and pp_cached.ok) else None
    except Exception:
        logger.exception("ask: payment_path cache read failed")

    # Доска (включает НЕДЕЛЯ/ФОКУС/НЕ МЕНЯТЬ)
    try:
        parts.append(build_board_report(
            project.name, None, payment_path=pp_dict,
            new_registrations_since_deploy=(pp_dict or {}).get("registrations"),
        ))
    except Exception:
        logger.exception("ask: board build failed")

    # Состояние Growth Loop
    try:
        running = growth_loop.get_running_experiment(session, project.id)
        if running is not None:
            progress = growth_loop.experiment_progress(running, pp_dict)
            parts.append("АКТИВНЫЙ ЭКСПЕРИМЕНТ:\n" + build_experiment_block(running, progress))
            # Легенда семантики — без неё LLM путает «10 отзывов» с «10 хороших»
            # и счётчик эксперимента с сырыми числами за 7 дней.
            parts.append(
                "КАК ЧИТАТЬ ЭКСПЕРИМЕНТ: прогресс N/M — это НОВЫЕ события выборки "
                f"({running.sample_metric}) с момента старта эксперимента, любые, не только успешные. "
                f"Вердикт выносится автоматически по ДОЛЕ {running.primary_metric} среди этих новых "
                "событий против baseline. СЫРЫЕ ЧИСЛА ниже — за 7 дней целиком и включают "
                "события ДО старта эксперимента; не смешивать со счётчиком прогресса."
            )
        rec = growth_loop.get_active_recommendation(session, project.id)
        if rec is not None:
            parts.append("ЖДЁТ РЕШЕНИЯ ВЛАДЕЛЬЦА:\n" + build_recommendation_details(rec))
        last = growth_loop.get_last_finished_experiment(session, project.id)
        if last is not None:
            parts.append("ПОСЛЕДНИЙ ВЕРДИКТ:\n" + build_verdict_block(last))
    except Exception:
        logger.exception("ask: growth loop context failed")

    # Сырые числа воронки за 7д (компактно, для точных ответов)
    if pp_dict:
        keys = ["registrations", "channels_created", "first_post_feedback_good",
                "first_post_feedback_bad", "pricing_viewed", "payment_cta_clicked",
                "payment_started", "payment_success",
                "queue_offer_shown", "queue_offer_clicked",
                "post_generations_verified", "post_generations_unverified"]
        nums = ", ".join(f"{k}={pp_dict.get(k)}" for k in keys if pp_dict.get(k) is not None)
        if nums:
            parts.append("СЫРЫЕ ЧИСЛА (7 дней): " + nums)
        sb = pp_dict.get("source_breakdown")
        if isinstance(sb, dict) and sb:
            parts.append("ПО ИСТОЧНИКАМ: " + str(sb))

    # Расход рекламы: последний combined-снимок 7д (spend/clicks) -- вкладка Маркетолога.
    if agent in (None, "marketer"):
        try:
            from sqlmodel import select as _select
            from app.models import MetricSnapshot
            from app.service import extract_normalized_metrics_from_snapshot
            snapshot = session.exec(
                _select(MetricSnapshot)
                .where(
                    MetricSnapshot.project_id == project.id,
                    MetricSnapshot.period_key == "7d",
                    MetricSnapshot.source == "combined",
                )
                .order_by(MetricSnapshot.created_at.desc())
                .limit(1)
            ).first()
            if snapshot is not None:
                raw = extract_normalized_metrics_from_snapshot(snapshot)
                spend, clicks = raw.get("spend"), raw.get("clicks")
                if spend is not None or clicks is not None:
                    cpa = None
                    regs = (pp_dict or {}).get("registrations")
                    if spend and regs:
                        cpa = round(float(spend) / int(regs))
                    parts.append(
                        f"РЕКЛАМА (7 дней, Яндекс Директ): расход {spend} ₽, кликов {clicks}"
                        + (f", цена регистрации ≈ {cpa} ₽" if cpa else "")
                    )
        except Exception:
            logger.exception("ask: ads spend context failed")

    # Как читать сырые числа (типовые вопросы владельца)
    parts.append(
        "КАК ЧИТАТЬ ЦИФРЫ: все сырые числа — за 7 дней. channels_created может "
        "превышать registrations: каналы создают и пользователи, зарегистрированные "
        "раньше этого окна. post_generations_* — НЕ действия пользователей (есть "
        "автогенерация), по ним выводов о вовлечённости не делать."
    )

    # Факты о проекте -- специфичны для АвтоПоста/TruePost, живут в playbook.
    # Подмешивать их в контекст ЧУЖОГО подключённого продукта нельзя: ядро
    # не знает продуктовой специфики, а факт «Telegram Ads на паузе»
    # применительно к другому бизнесу -- не честная неточность, а выдумка.
    if _is_autopost_project(project):
        try:
            from app.truepost_playbook import PROJECT_FACTS
            parts.append(PROJECT_FACTS)
        except Exception:
            logger.exception("ask: project facts failed")

    parts.append(SCREENS_REFERENCE)

    # Живые числа тех же карточек, что видит владелец на «Обзоре» -- чтобы
    # отвечать не общими словами, а теми же цифрами, что на экране, и
    # называть карточку, где их можно перепроверить (принцип 8 выше).
    live = _live_screens_summary(session, project, agent)
    if live:
        parts.append(live)

    # Динамика по дням
    try:
        history = load_daily_counters_history(session, project.id, days=7)
        if len(history) >= 2:
            parts.append(build_dynamics_block(history))
    except Exception:
        logger.exception("ask: dynamics context failed")

    context = "\n\n".join(p for p in parts if p)
    return context[:MAX_CONTEXT_CHARS]


async def answer_question(
    question: str,
    context_text: str,
    settings,
    *,
    _post=None,
) -> str | None:
    """
    Один вызов Anthropic API. None при любой ошибке (вызывающий покажет
    fallback). _post -- инъекция для тестов.
    """
    global _last_call_ts
    now = time.monotonic()
    if now - _last_call_ts < COOLDOWN_SECONDS:
        return "Секунду, отвечаю не чаще раза в несколько секунд — повтори вопрос."
    _last_call_ts = now

    question = (question or "").strip()[:MAX_QUESTION_CHARS]
    if not question:
        return None

    provider = getattr(settings, "llm_provider", "none")
    if provider == "yandex":
        return await _answer_yandex(question, context_text, settings, _post=_post)

    payload = {
        "model": settings.anthropic_model,
        "max_tokens": MAX_ANSWER_TOKENS,
        "system": SYSTEM_PROMPT + "\n\n=== ТЕКУЩИЕ ДАННЫЕ ПРОЕКТА ===\n" + context_text,
        "messages": [{"role": "user", "content": question}],
    }
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        if _post is not None:
            data = await _post(payload, headers)
        else:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(ANTHROPIC_URL, json=payload, headers=headers)
                if resp.status_code != 200:
                    logger.warning("ask: anthropic HTTP %s: %s", resp.status_code, resp.text[:200])
                    return None
                data = resp.json()
        blocks = data.get("content") or []
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        return text or None
    except Exception:
        logger.exception("ask: anthropic call failed")
        return None


async def _answer_yandex(
    question: str,
    context_text: str,
    settings,
    *,
    _post=None,
) -> str | None:
    """
    Вызов LLM через Yandex Cloud -- работает с серверов в РФ, в отличие от
    Anthropic API. Режимы (YANDEX_API_MODE):
      native -- YandexGPT, Foundation Models completion API;
      openai -- DeepSeek/Qwen и др. открытые модели, AI Studio Responses API.
    """
    system_text = SYSTEM_PROMPT + "\n\n=== ТЕКУЩИЕ ДАННЫЕ ПРОЕКТА ===\n" + context_text
    mode = getattr(settings, "yandex_api_mode", "openai")
    headers = {
        "Authorization": f"Api-Key {settings.yandex_api_key}",
        "content-type": "application/json",
    }

    if mode == "native":
        model_uri = settings.yandex_model_uri or f"gpt://{settings.yandex_folder_id}/yandexgpt/latest"
        url = YANDEX_COMPLETION_URL
        payload = {
            "modelUri": model_uri,
            "completionOptions": {"stream": False, "maxTokens": str(MAX_ANSWER_TOKENS)},
            "messages": [
                {"role": "system", "text": system_text},
                {"role": "user", "text": question},
            ],
        }
    else:
        url = YANDEX_RESPONSES_URL
        # Формат проверен в проде АвтоПоста и Компаса: модель ВСЕГДА с
        # префиксом gpt:// -- без него Responses API отвечает ошибкой.
        payload = {
            "model": settings.yandex_model_uri
                     or f"gpt://{settings.yandex_folder_id}/{settings.yandex_model}",
            "instructions": system_text,
            "input": question,
            "max_output_tokens": MAX_ANSWER_TOKENS + YANDEX_REASONING_TOKENS_MARGIN,
            # DeepSeek по умолчанию «размышляет» невидимыми токенами и может
            # израсходовать на них весь лимит, оставив пустой ответ.
            "thinking": {"type": "disabled"},
        }

    if _post is not None:
        try:
            return _extract_yandex_text(await _post(payload, headers), mode)
        except Exception:
            logger.exception("ask: yandex call failed")
            return None

    # Сеть до Яндекса иногда моргает -- три попытки, как в АвтоПосте.
    for attempt in range(YANDEX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                logger.warning(
                    "ask: yandex HTTP %s (попытка %s/%s): %s",
                    resp.status_code, attempt + 1, YANDEX_RETRIES, resp.text[:300],
                )
                continue
            text = _extract_yandex_text(resp.json(), mode)
            if text:
                return text
            logger.warning("ask: yandex вернул пустой текст (попытка %s)", attempt + 1)
        except Exception:
            logger.exception("ask: yandex call failed (попытка %s)", attempt + 1)
    return None


def _extract_yandex_text(data: dict, mode: str) -> str | None:
    if mode == "native":
        try:
            text = data["result"]["alternatives"][0]["message"]["text"].strip()
            return text or None
        except (KeyError, IndexError, TypeError, AttributeError):
            return None
    # Responses API: reasoning-блоки DeepSeek отбрасываем, берём только
    # message/output_text (разбор совпадает с рабочим кодом Компаса).
    parts: list[str] = []
    for item in data.get("output") or []:
        if item.get("type") == "reasoning":
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("type") in (None, "output_text", "text"):
                if block.get("text"):
                    parts.append(block["text"])
    if not parts and isinstance(data.get("output_text"), str):
        parts.append(data["output_text"])
    text = "".join(parts).strip()
    return text or None


def is_configured(settings) -> bool:
    provider = getattr(settings, "llm_provider", "none")
    if provider == "anthropic":
        return bool(getattr(settings, "anthropic_api_key", None))
    if provider == "yandex":
        return bool(
            getattr(settings, "yandex_api_key", None)
            and getattr(settings, "yandex_folder_id", None)
        )
    return False
