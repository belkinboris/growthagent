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
MAX_QUESTION_CHARS = 1000
MAX_CONTEXT_CHARS = 9000     # доска+воронка+оплата+эксперимент обычно ~3-5k
MAX_ANSWER_TOKENS = 700
COOLDOWN_SECONDS = 3.0

_last_call_ts: float = 0.0

SYSTEM_PROMPT = """Ты — Аналитик Воронки, продакт-помощник владельца стартапа АвтоПост \
(ИИ-сервис для ведения Telegram-канала). Владелец — Борис, соло-фаундер, \
общается по-простому, часто голосом.

Твои принципы (нарушать нельзя):
1. Данные важнее мнений. Каждый вывод опирайся на цифры из контекста ниже. \
Если данных нет — так и скажи: «в моих данных этого нет», не выдумывай.
2. Малые выборки — честные слова: «единичное событие», «предварительный \
сигнал», «данных недостаточно». Никогда не изображай уверенность на 3-5 событиях.
3. Одна переменная за раз. Если идёт эксперимент — напоминай, что заперто \
(«Не менять»), и отговаривай трогать эти переменные до вердикта.
4. Ты не применяешь изменения. Решения принимаются кнопками на /board, \
код меняется по спекам в отдельных чатах. Ты объясняешь и советуешь.
5. Простой русский. Без MBA-жаргона, без «confidence interval», без \
«decision engine». Коротко: 2-6 абзацев максимум, без списков где можно без них.
6. Если вопрос про то, «что делать» — сначала посмотри на активную \
рекомендацию/эксперимент в контексте: чаще всего ответ «дать текущему \
эксперименту довестись», и это правильный ответ, а не отписка.
7. Не льсти. Если владелец предлагает плохое (менять всё сразу, лить бюджет \
в неработающий канал, менять переменные во время теста) — скажи прямо и объясни почему.

Тебе дают снимок текущих данных проекта. Отвечай на вопрос владельца."""


def build_context(session, project) -> str:
    """
    Снимок данных для системного контекста: доска (+growth loop состояние),
    воронка, оплата, динамика. Всё из существующих builder'ов и кэшей --
    никаких новых запросов к TruePost. Устойчив к отсутствию любого куска.
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

    parts: list[str] = []

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

    # Расход рекламы: последний combined-снимок 7д (spend/clicks)
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

    # Факты о проекте (TruePost-specific, живут в playbook)
    try:
        from app.truepost_playbook import PROJECT_FACTS
        parts.append(PROJECT_FACTS)
    except Exception:
        logger.exception("ask: project facts failed")

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
        payload = {
            "model": f"{settings.yandex_folder_id}/{settings.yandex_model}",
            "instructions": system_text,
            "input": question,
            "max_output_tokens": MAX_ANSWER_TOKENS + YANDEX_REASONING_TOKENS_MARGIN,
            "thinking": {"type": "disabled"},
        }

    try:
        if _post is not None:
            data = await _post(payload, headers)
        else:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code != 200:
                    logger.warning("ask: yandex HTTP %s: %s", resp.status_code, resp.text[:300])
                    return None
                data = resp.json()
        return _extract_yandex_text(data, mode)
    except Exception:
        logger.exception("ask: yandex call failed")
        return None


def _extract_yandex_text(data: dict, mode: str) -> str | None:
    if mode == "native":
        try:
            text = data["result"]["alternatives"][0]["message"]["text"].strip()
            return text or None
        except (KeyError, IndexError, TypeError, AttributeError):
            return None
    # Responses API: output -- список блоков, текст в output[].content[].text
    parts: list[str] = []
    for item in data.get("output") or []:
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("text"):
                parts.append(block["text"])
    text = "\n".join(parts).strip()
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
