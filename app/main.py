"""
Автопост — FastAPI приложение.
Запускает API + раздаёт сайт + планировщик.
"""

import json
import logging
import secrets
import string
from contextlib import asynccontextmanager
from datetime import datetime

import uvicorn
from fastapi import FastAPI, Depends, HTTPException, Request, Header, BackgroundTasks
from fastapi.responses import PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import select, delete

import config
import security
import billing
import generator
import research
import telegram_api
import tasks
from database import (
    init_db, session,
    User, Channel, Source, Post, Payment, Referral, LandingEvent, IdempotencyKey, ProductEvent,
    TrafficAttribution,
)
from attribution import classify_utm
from pydantic import BaseModel as _BaseModel
from typing import Optional as _Opt

class _VerifyIn(_BaseModel):
    tg_chat: str

class _ConsultIn(_BaseModel):
    message: str
    history: list = []

class _RuleIn(_BaseModel):
    rule_text: str

class _MePatch(_BaseModel):
    notify_new_post: _Opt[bool] = None
    notify_published: _Opt[bool] = None
    notify_low_tokens: _Opt[bool] = None
    tg_chat_id: _Opt[int] = None

from schemas import (
    AuthIn, ChannelIn, ChannelPatch, SourceIn,
    AnalyzeIn, AnalyzeStyleOnly, GenerateFormatIn, PostIn,
    PostPatch, ScheduleIn, BuyIn,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("autopost")


# ── Планировщик ───────────────────────────────────────────────
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _HAS_SCHEDULER = True
except ImportError:
    _scheduler = None
    _HAS_SCHEDULER = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("БД готова")
    if _HAS_SCHEDULER and _scheduler:
        _scheduler.add_job(
            tasks.tick, "interval", seconds=config.TICK_SECONDS,
            id="master_tick", replace_existing=True, max_instances=1, coalesce=True,
        )
        # КРИТИЧНО (P1 fix): /start у @maintrpost_bot раньше ловился только
        # внутри tick() (раз в 60с) -- пользователь мог ждать ответа до
        # минуты. Отдельная, более частая задача специально для этого --
        # не трогаем общий TICK_SECONDS, который разумен для генерации/
        # публикации постов, но слишком редок для интерактивного /start.
        _scheduler.add_job(
            tasks.poll_main_bot, "interval", seconds=config.MAIN_BOT_POLL_SECONDS,
            id="main_bot_poll", replace_existing=True, max_instances=1, coalesce=True,
        )
        _scheduler.start()
        logger.info(f"Планировщик запущен, тик каждые {config.TICK_SECONDS}с, /start-поллинг каждые {config.MAIN_BOT_POLL_SECONDS}с")
    yield
    if _HAS_SCHEDULER and _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


app = FastAPI(title="Автопост", lifespan=lifespan)

from internal_metrics import router as internal_metrics_router
app.include_router(internal_metrics_router)

from internal_landing_funnel import router as landing_funnel_router
app.include_router(landing_funnel_router)

from internal_schema_diagnostics import router as schema_diag_router
app.include_router(schema_diag_router)

from internal_payment_path import router as payment_path_router
app.include_router(payment_path_router)

from internal_user_journeys import router as user_journeys_router
app.include_router(user_journeys_router)

# ── Авторизация ───────────────────────────────────────────────

def current_user(authorization: str = Header(default="")) -> User:
    if not authorization.startswith("Bearer "):
        logger.info("[auth] 401: no Bearer prefix in Authorization header")
        raise HTTPException(401, "Не авторизован")
    uid = security.verify_token(authorization[7:])
    if not uid:
        logger.info(f"[auth] 401: verify_token returned None (token invalid/expired), token_prefix={authorization[7:17]}...")
        raise HTTPException(401, "Сессия истекла, войдите снова")
    with session() as s:
        user = s.get(User, uid)
        if not user:
            logger.warning(f"[auth] 401: uid={uid} from valid token, but no such User in DB")
            raise HTTPException(401, "Пользователь не найден")
        s.expunge(user)
        return user


def _gen_ref_code() -> str:
    chars = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(8))


def _own_channel(s, channel_id: int, user: User) -> Channel:
    ch = s.get(Channel, channel_id)
    if not ch or ch.user_id != user.id:
        raise HTTPException(404, "Канал не найден")
    return ch


def _own_post(s, post_id: int, user: User) -> Post:
    p = s.get(Post, post_id)
    if not p or p.user_id != user.id:
        raise HTTPException(404, "Пост не найден")
    return p


# ── Мета ──────────────────────────────────────────────────────

@app.get("/api/config")
def get_config():
    return {
        "bot_username": config.TELEGRAM_BOT_USERNAME,
        "public_url": config.PUBLIC_URL,
        "packages": config.TOKEN_PACKAGES,
        "yookassa_enabled": billing.is_configured(),
        # Старый ключ оставлен для совместимости с фронтом, если браузер закэширует app.js.
        "yoomoney_enabled": billing.is_configured(),
    }


# ── Auth ──────────────────────────────────────────────────────

@app.post("/api/register")
def register(data: AuthIn):
    email = data.email.strip().lower()
    if "@" not in email or len(data.password) < 6:
        raise HTTPException(400, "Нужен корректный email и пароль от 6 символов")

    with session() as s:
        if s.exec(select(User).where(User.email == email)).first():
            raise HTTPException(400, "Пользователь с таким email уже есть")

        # Проверяем реферальный код
        referrer = None
        ref_code = (data.ref_code or "").strip().upper()
        if ref_code:
            referrer = s.exec(select(User).where(User.ref_code == ref_code)).first()

        user = User(
            email=email,
            password_hash=security.hash_password(data.password),
            token_balance=config.WELCOME_TOKENS,
            ref_code=_gen_ref_code(),
            referred_by=referrer.id if referrer else None,
        )
        s.add(user)
        s.commit()
        s.refresh(user)

        # CTA/Journey Diagnostics: связываем регистрацию с сессией лендинга,
        # если она была передана. User не трогаем -- пишем только в LandingEvent
        # (новая таблица, безопасно создаётся через create_all, без ALTER TABLE
        # на существующих таблицах). Не блокирует регистрацию при сбое.
        if data.lp_session:
            try:
                s.add(LandingEvent(
                    session_id=data.lp_session[:64],
                    event="register_success",
                    user_id=user.id,
                    utm_source=(data.utm_source or "")[:50],
                    utm_medium=(data.utm_medium or "")[:50],
                    utm_campaign=(data.utm_campaign or "")[:100],
                ))
                s.commit()
            except Exception:
                pass

        # Attribution: источник трафика для разделения Telegram Ads / Yandex
        # Direct / organic перед запуском Telegram Ads. Не блокирует
        # регистрацию при сбое -- та же безопасная схема что LandingEvent выше.
        try:
            linked = False
            if data.lp_session:
                # Сначала пробуем привязать уже существующую запись по той же
                # сессии (могла быть создана раньше: на /api/landing-event
                # landing_view с UTM, либо ботом при /start tgads_*). Так не
                # плодим дублирующие TrafficAttribution на одну сессию.
                existing = s.exec(
                    select(TrafficAttribution).where(
                        TrafficAttribution.landing_session_id == data.lp_session[:64],
                        TrafficAttribution.user_id == None,  # noqa: E711
                    )
                ).first()
                if existing:
                    existing.user_id = user.id
                    s.add(existing)
                    s.commit()
                    linked = True

            if not linked and data.utm_source:
                # Запасной путь: UTM пришли прямо с формы регистрации, но
                # записи в TrafficAttribution по сессии ещё не было (например
                # landing_view не успел отправиться, или session_id не дошёл).
                src, med = classify_utm(data.utm_source, data.utm_medium)
                s.add(TrafficAttribution(
                    user_id=user.id,
                    landing_session_id=(data.lp_session[:64] if data.lp_session else None),
                    source=src,
                    medium=med,
                    campaign=(data.utm_campaign or "")[:100],
                    content=(data.utm_content or "")[:100],
                ))
                s.commit()
        except Exception:
            pass

        # Начисляем бонус рефереру
        if referrer:
            referrer_obj = s.get(User, referrer.id)
            if referrer_obj:
                referrer_obj.token_balance += config.REFERRAL_BONUS_TOKENS
                ref = Referral(
                    referrer_id=referrer.id,
                    referred_id=user.id,
                    bonus_tokens=config.REFERRAL_BONUS_TOKENS,
                )
                s.add(referrer_obj)
                s.add(ref)
                # Бонус новому пользователю тоже
                user.token_balance += config.REFERRAL_BONUS_TOKENS
                s.add(user)
                s.commit()
                s.refresh(user)

        return {"token": security.create_token(user.id), "email": user.email}


class _LandingEventIn(_BaseModel):
    session_id: str
    event: str
    url: str = ""
    utm_source: str = ""
    utm_medium: str = ""
    utm_campaign: str = ""
    utm_content: str = ""
    yclid: str = ""
    user_agent: str = ""


_ALLOWED_LANDING_EVENTS = {
    "landing_view",
    "cta_hero_bot_click",
    "cta_hero_app_click",
    "cta_header_click",
    "cta_final_click",
    "bot_start_from_landing",
    "web_register_opened",
    "register_success",
    "activation_1",
    "first_post_generated",
    "channel_connected",
    "first_post_published",
}


@app.post("/api/landing-event")
def landing_event(data: _LandingEventIn, request: Request):
    """
    Read-only диагностика пути landing -> Telegram/web -> registration
    (CTA/Journey Diagnostics). Не влияет на бизнес-логику, не блокирует
    пользователя при сбое -- событие просто не записывается.
    """
    if not data.session_id or not data.event:
        return {"ok": False}
    if data.event not in _ALLOWED_LANDING_EVENTS:
        return {"ok": False}
    try:
        ua = data.user_agent or request.headers.get("user-agent", "")
        with session() as s:
            s.add(LandingEvent(
                session_id=data.session_id[:64],
                event=data.event,
                url=(data.url or "")[:500],
                utm_source=(data.utm_source or "")[:50],
                utm_medium=(data.utm_medium or "")[:50],
                utm_campaign=(data.utm_campaign or "")[:100],
                yclid=(data.yclid or "")[:100],
                user_agent=ua[:300],
            ))
            # Attribution: фиксируем источник трафика как можно раньше (на
            # первом событии лендинга с UTM), без user_id -- привязка к
            # user_id произойдёт позже в /api/register по тому же session_id.
            # Пишем только один раз на сессию (landing_view -- первое событие
            # пути), чтобы не плодить дублирующие записи на каждый клик.
            if data.event == "landing_view" and data.utm_source:
                already = s.exec(
                    select(TrafficAttribution).where(
                        TrafficAttribution.landing_session_id == data.session_id[:64]
                    )
                ).first()
                if not already:
                    src, med = classify_utm(data.utm_source, data.utm_medium)
                    s.add(TrafficAttribution(
                        landing_session_id=data.session_id[:64],
                        source=src,
                        medium=med,
                        campaign=(data.utm_campaign or "")[:100],
                        content=(data.utm_content or "")[:100],
                    ))
            s.commit()
    except Exception:
        pass
    return {"ok": True}


class _ProductEventIn(_BaseModel):
    event: str
    package_id: str = ""


_ALLOWED_PRODUCT_EVENTS = {
    "pricing_viewed",
    "payment_cta_clicked",
    "payment_failed",
    "payment_returned",
    "quota_warning_seen",
    "limit_reached",
    # Онбординг: выбор пути в начале quick start.
    # package_id хранит значение: generate_first_post / analyze_existing_channel / skip
    "onboarding_choice_selected",
    # Качество первого поста: пользователь оценивает результат генерации.
    # package_id хранит: good / bad
    "first_post_feedback",
    # Причина недовольства первым постом (если first_post_feedback == bad).
    # package_id хранит: too_generic / wrong_style / wrong_topic / too_dry / too_salesy / other
    "first_post_feedback_reason",
}


@app.post("/api/product-event")
def product_event(data: _ProductEventIn, user: User = Depends(current_user)):
    """
    Минимальная диагностика payment path после регистрации (не для рекламной
    атрибуции -- для этого уже есть LandingEvent/Метрика). Read-only, не
    влияет на бизнес-логику, не блокирует пользователя при сбое.

    Намеренно не пишет события которые уже есть как backend truth
    (registration/channel_created/post_generated/payment_started/
    payment_success) -- те уже надёжно видны через User/Channel/Post/Payment
    напрямую, дублировать их здесь не нужно (см. карту событий в
    internal_payment_path.py).
    """
    if data.event not in _ALLOWED_PRODUCT_EVENTS:
        return {"ok": False}
    try:
        with session() as s:
            s.add(ProductEvent(
                user_id=user.id,
                event=data.event,
                package_id=(data.package_id or "")[:20],
            ))
            s.commit()
    except Exception:
        pass
    return {"ok": True}


@app.post("/api/login")
def login(data: AuthIn):
    email = data.email.strip().lower()
    with session() as s:
        user = s.exec(select(User).where(User.email == email)).first()
        if not user or not security.verify_password(data.password, user.password_hash):
            raise HTTPException(401, "Неверный email или пароль")
        return {"token": security.create_token(user.id), "email": user.email}


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    with session() as s:
        refs = s.exec(select(Referral).where(Referral.referrer_id == user.id)).all()
        count = len(refs)
    return {
        "id": user.id,
        "email": user.email,
        "token_balance": user.token_balance,
        "is_admin": user.is_admin,
        "ref_code": user.ref_code,
        "referrals_count": count,
        "tg_chat_id": user.tg_chat_id,
        "notify_published": user.notify_published,
        "notify_low_tokens": user.notify_low_tokens,
    }


# ── Каналы ────────────────────────────────────────────────────

def _channel_dict(ch: Channel) -> dict:
    d = ch.model_dump()
    try:
        d["daily_times"] = json.loads(ch.daily_times or "[]")
    except Exception:
        d["daily_times"] = []
    return d


@app.get("/api/channels")
def list_channels(user: User = Depends(current_user)):
    with session() as s:
        chans = s.exec(select(Channel).where(Channel.user_id == user.id)).all()
        return [_channel_dict(c) for c in chans]


class _TopicValidateIn(_BaseModel):
    topic: str


@app.post("/api/validate-topic")
async def validate_topic(data: _TopicValidateIn, user: User = Depends(current_user)):
    """
    Валидация темы ДО создания канала (quick start onboarding).

    Критично: этот эндпоинт не создаёт ничего в БД. Раньше тема проверялась
    только внутри generate_for_channel(), то есть ПОСЛЕ создания Channel —
    из-за этого неподходящая тема всё равно попадала в dashboard/settings
    как уже существующий канал, даже если генерация поста потом отказывала.
    Теперь фронт обязан вызвать этот эндпоинт первым и не создавать канал
    при отрицательном результате.
    """
    classification = await generator.classify_topic(data.topic)
    logger.info(f"validate_topic: user={user.id} topic_classification={classification} topic=«{data.topic[:80]}»")

    if classification == "ambiguous_intimate_topic":
        # Task E: серая зона — не жёсткий отказ, а уточняющий вопрос.
        # ok=false (канал пока не создаём), но это другая категория чем
        # rejection — фронт должен показать иной UX (предложение продолжить
        # с переформулированной/уточнённой темой, не просто "тема запрещена").
        return {
            "ok": False,
            "classification": classification,
            "message": generator.AMBIGUOUS_INTIMATE_CLARIFICATION,
            "is_clarification": True,
        }

    rejection_msg = generator.rejection_message(classification)
    return {
        "ok": rejection_msg is None,
        "classification": classification,
        "message": rejection_msg,
        "is_clarification": False,
    }


@app.post("/api/channels")
def create_channel(data: ChannelIn, user: User = Depends(current_user)):
    with session() as s:
        # Идемпотентность quick start (task item E): если этот client_request_id
        # уже обработан раньше (повторный клик после "Load failed", двойной
        # сабмит формы) -- возвращаем уже созданный канал, не создаём новый.
        #
        # КРИТИЧНО (P0 fix): возвращаем существующий канал ТОЛЬКО если его
        # about совпадает с текущим запросом. Если client_request_id совпал,
        # но about отличается -- это значит ключ "протёк" из предыдущей
        # quick-start сессии (stale App._qsRequestId на фронте, browser
        # back-forward cache, или любая другая причина повторного
        # использования ключа), а не настоящий повторный клик внутри одной
        # генерации. В этом случае НЕЛЬЗЯ тихо вернуть канал со старой темой —
        # лучше создать новый канал с правильной темой, чем дать пользователю
        # пост про то, что он не вводил.
        if data.client_request_id:
            existing_key = s.exec(
                select(IdempotencyKey).where(
                    IdempotencyKey.user_id == user.id,
                    IdempotencyKey.client_request_id == data.client_request_id,
                )
            ).first()
            if existing_key:
                existing_channel = s.get(Channel, existing_key.channel_id)
                if existing_channel and existing_channel.about == data.about:
                    logger.info(f"create_channel: повторный client_request_id «{data.client_request_id}», about совпадает, возвращаю существующий канал {existing_channel.id}")
                    return _channel_dict(existing_channel)
                elif existing_channel:
                    logger.warning(
                        f"create_channel: client_request_id «{data.client_request_id}» совпал, но about отличается "
                        f"(existing=«{existing_channel.about}» vs new=«{data.about}») -- stale request_id, создаю новый канал, НЕ возвращаю старый"
                    )

        ch = Channel(
            user_id=user.id,
            title=data.title,
            tg_chat=data.tg_chat.strip(),
            about=data.about,
            style=data.style,
            style_profile=data.style_profile,
            post_length=data.post_length,
            language=data.language,
            post_voice=data.post_voice,
            post_format=data.post_format,
            emoji_style=data.emoji_style,
            cta_enabled=data.cta_enabled,
            cta_text=data.cta_text,
            use_web_search=data.use_web_search,
            auto_publish=data.auto_publish,
            schedule_kind=data.schedule_kind,
            interval_hours=data.interval_hours,
            daily_times=json.dumps(data.daily_times),
            enabled=data.enabled,
            onboarded=data.onboarded,
        )
        s.add(ch)
        s.commit()
        s.refresh(ch)

        if data.client_request_id:
            # Если у этого client_request_id уже была другая запись (stale,
            # about не совпал) -- не плодим дублирующиеся idempotency-записи
            # на один ключ, перезаписываем на актуальный канал.
            old_keys = s.exec(
                select(IdempotencyKey).where(
                    IdempotencyKey.user_id == user.id,
                    IdempotencyKey.client_request_id == data.client_request_id,
                )
            ).all()
            for k in old_keys:
                s.delete(k)
            s.add(IdempotencyKey(
                user_id=user.id,
                client_request_id=data.client_request_id,
                channel_id=ch.id,
            ))
            s.commit()

        logger.info(f"[create_channel] создан channel_id={ch.id} title=«{ch.title}» about=«{ch.about}» client_request_id=«{data.client_request_id}»")
        return _channel_dict(ch)


@app.get("/api/channels/{channel_id}")
def get_channel(channel_id: int, user: User = Depends(current_user)):
    with session() as s:
        return _channel_dict(_own_channel(s, channel_id, user))


@app.patch("/api/channels/{channel_id}")
def patch_channel(channel_id: int, data: ChannelPatch, user: User = Depends(current_user)):
    with session() as s:
        ch = _own_channel(s, channel_id, user)
        payload = data.model_dump(exclude_none=True)
        if "daily_times" in payload:
            payload["daily_times"] = json.dumps(payload["daily_times"])
        if "tg_chat" in payload:
            new_chat = payload["tg_chat"].strip()
            payload["tg_chat"] = new_chat
            # Сбрасываем verified только если реально поменялся username
            if new_chat != (ch.tg_chat or ""):
                ch.verified = False
        # При возобновлении ставим last_generated_at = now
        # чтобы следующая авто-генерация была через полный интервал, а не немедленно
        if payload.get("enabled") is True and not ch.enabled:
            ch.last_generated_at = datetime.utcnow()
        for k, v in payload.items():
            setattr(ch, k, v)
        s.add(ch)
        s.commit()
        s.refresh(ch)
        return _channel_dict(ch)


@app.delete("/api/channels/{channel_id}")
def delete_channel(channel_id: int, user: User = Depends(current_user)):
    from database import ChannelRule
    try:
        with session() as s:
            ch = _own_channel(s, channel_id, user)
            for src in s.exec(select(Source).where(Source.channel_id == channel_id)).all():
                s.delete(src)
            for p in s.exec(select(Post).where(Post.channel_id == channel_id)).all():
                s.delete(p)
            for r in s.exec(select(ChannelRule).where(ChannelRule.channel_id == channel_id)).all():
                s.delete(r)
            s.delete(ch)
            s.commit()
    except HTTPException:
        raise  # 404 "канал не найден" от _own_channel — пропускаем как есть, это уже понятный текст
    except Exception as e:
        logger.error(f"delete_channel: не удалось удалить канал {channel_id}: {e}")
        raise HTTPException(500, "Не удалось удалить канал. Обновите страницу и попробуйте ещё раз.")

    # Чистим idempotency-ключи, указывающие на этот канал (task item E) —
    # иначе повторный клик с тем же client_request_id попытается вернуть
    # уже удалённый канал.
    #
    # КРИТИЧНО (P0 regression fix): это отдельная, изолированная попытка,
    # ПОСЛЕ того как сам канал и все его данные уже успешно удалены. Раньше
    # очистка IdempotencyKey была частью той же транзакции, что и удаление
    # канала — если таблица IdempotencyKey по любой причине не существовала
    # в БД (например create_all() не успел создать её на проде), весь запрос
    # падал с OperationalError и откатывал ВСЮ транзакцию, включая удаление
    # канала. Теперь это не может случиться: основное удаление уже
    # подтверждено и закоммичено выше, эта очистка — best-effort, любая её
    # ошибка только логируется.
    try:
        with session() as s:
            for k in s.exec(select(IdempotencyKey).where(IdempotencyKey.channel_id == channel_id)).all():
                s.delete(k)
            s.commit()
    except Exception as e:
        logger.warning(f"delete_channel: не удалось очистить IdempotencyKey для канала {channel_id} (не критично, канал уже удалён): {e}")

    return {"ok": True}


@app.post("/api/channels/{channel_id}/verify")
async def verify_channel(channel_id: int, user: User = Depends(current_user)):
    with session() as s:
        ch = _own_channel(s, channel_id, user)
        chat = ch.tg_chat
    if not chat:
        raise HTTPException(400, "Сначала укажите @username канала")
    ok, message = await telegram_api.verify_channel(chat)
    with session() as s:
        ch = _own_channel(s, channel_id, user)
        ch.verified = ok
        s.add(ch)
        s.commit()
    return {"ok": ok, "message": message}


@app.post("/api/channels/{channel_id}/generate")
async def generate_channel(channel_id: int, data: PostIn = PostIn(), user: User = Depends(current_user)):
    with session() as s:
        _own_channel(s, channel_id, user)
    result = await tasks.generate_for_channel(channel_id, topic=data.topic, force_pending=True)
    if not result["ok"]:
        raise HTTPException(400, result["message"])
    return result


@app.post("/api/channels/{channel_id}/generate_format")
async def generate_channel_format(
    channel_id: int, data: GenerateFormatIn, user: User = Depends(current_user)
):
    """Генерирует пост в конкретном формате (для онбординга — 3 варианта)."""
    with session() as s:
        ch = _own_channel(s, channel_id, user)
        u = s.get(User, user.id)
        if u.token_balance <= 0:
            raise HTTPException(400, "Бесплатный лимит закончился. Пополните баланс, чтобы создавать новые посты.")

    # Временно меняем формат для этой генерации
    with session() as s:
        ch = s.get(Channel, channel_id)
        original_format = ch.post_format
        ch.post_format = data.post_format
        s.add(ch)
        s.commit()

    try:
        result = await tasks.generate_for_channel(channel_id, force_pending=True)
    finally:
        # Возвращаем оригинальный формат
        with session() as s:
            ch = s.get(Channel, channel_id)
            if ch:
                ch.post_format = original_format
                s.add(ch)
                s.commit()

    if not result["ok"]:
        raise HTTPException(400, result["message"])

    # Возвращаем текст поста напрямую для онбординга
    with session() as s:
        post = s.get(Post, result.get("post_id"))
        text = post.text if post else ""

    return {
        "ok": True,
        "post_id": result.get("post_id"),
        "text": text,
        "tokens_used": result.get("tokens_used", 0),
    }


@app.post("/api/channels/{channel_id}/analyze")
async def analyze_channel(channel_id: int, data: AnalyzeIn, user: User = Depends(current_user)):
    with session() as s:
        _own_channel(s, channel_id, user)
        u = s.get(User, user.id)
        if u.token_balance <= 0:
            raise HTTPException(400, "Бесплатный лимит закончился. Пополните баланс, чтобы создавать новые посты.")

    posts = await research.scrape_channel(data.link)
    if not posts:
        raise HTTPException(400, "Не удалось прочитать канал. Он должен быть публичным.")

    profile, tokens = await generator.analyze_style(posts)

    with session() as s:
        ch = s.get(Channel, channel_id)
        ch.style_profile = profile
        u = s.get(User, user.id)
        u.token_balance = max(0, u.token_balance - tokens)
        s.add(ch); s.add(u); s.commit()

    return {"ok": True, "profile": profile, "analyzed_posts": len(posts), "tokens_used": tokens}


@app.post("/api/analyze_style_only")
async def analyze_style_only(data: AnalyzeStyleOnly, user: User = Depends(current_user)):
    """Анализ стиля без привязки к каналу — для онбординга."""
    with session() as s:
        u = s.get(User, user.id)
        if u.token_balance <= 0:
            raise HTTPException(400, "Бесплатный лимит закончился. Пополните баланс, чтобы создавать новые посты.")

    posts = await research.scrape_channel(data.link)
    if not posts:
        raise HTTPException(400, "Не удалось прочитать канал. Он должен быть публичным.")

    profile, tokens = await generator.analyze_style(posts)

    with session() as s:
        u = s.get(User, user.id)
        u.token_balance = max(0, u.token_balance - tokens)
        s.add(u); s.commit()

    return {"ok": True, "profile": profile, "analyzed_posts": len(posts)}


# ── Источники ─────────────────────────────────────────────────

@app.get("/api/channels/{channel_id}/sources")
def list_sources(channel_id: int, user: User = Depends(current_user)):
    with session() as s:
        _own_channel(s, channel_id, user)
        srcs = s.exec(select(Source).where(Source.channel_id == channel_id)).all()
        return [src.model_dump() for src in srcs]


@app.post("/api/channels/{channel_id}/sources")
def add_source(channel_id: int, data: SourceIn, user: User = Depends(current_user)):
    url = data.url.strip()
    if not url.startswith("http"):
        raise HTTPException(400, "Источник должен быть ссылкой (http/https)")
    with session() as s:
        _own_channel(s, channel_id, user)
        src = Source(channel_id=channel_id, url=url)
        s.add(src); s.commit(); s.refresh(src)
        return src.model_dump()


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int, user: User = Depends(current_user)):
    with session() as s:
        src = s.get(Source, source_id)
        if not src:
            raise HTTPException(404, "Источник не найден")
        _own_channel(s, src.channel_id, user)
        s.delete(src); s.commit()
    return {"ok": True}


# ── Посты ──────────────────────────────────────────────────────

@app.get("/api/channels/{channel_id}/posts")
def list_posts(channel_id: int, user: User = Depends(current_user)):
    with session() as s:
        _own_channel(s, channel_id, user)
        posts = s.exec(
            select(Post).where(Post.channel_id == channel_id).order_by(Post.created_at.desc())
        ).all()
        return [p.model_dump() for p in posts]


@app.patch("/api/posts/{post_id}")
def edit_post(post_id: int, data: PostPatch, user: User = Depends(current_user)):
    with session() as s:
        p = _own_post(s, post_id, user)
        if p.status == "published":
            raise HTTPException(400, "Опубликованный пост нельзя редактировать")
        p.text = data.text
        s.add(p); s.commit(); s.refresh(p)
        return p.model_dump()


@app.get("/api/posts/{post_id}/status")
def post_status(post_id: int, user: User = Depends(current_user)):
    """
    Лёгкий статус-эндпоинт для reconciliation на фронте после ложного
    timeout публикации (P0 fix): фронт опрашивает его, чтобы узнать
    реальное состояние поста, не повторяя сам publish.
    """
    with session() as s:
        p = _own_post(s, post_id, user)
        return {
            "id": p.id,
            "status": p.status,
            "telegram_message_id": p.tg_message_id,
            "published_at": p.published_at.isoformat() if p.published_at else None,
        }


@app.post("/api/posts/{post_id}/publish")
async def publish(post_id: int, background_tasks: BackgroundTasks, user: User = Depends(current_user)):
    with session() as s:
        _own_post(s, post_id, user)
    result = await tasks.publish_post(post_id)
    if not result["ok"]:
        raise HTTPException(400, result["message"])
    # Уведомление и автодогенерация очереди — после ответа клиенту, не
    # блокируют его (см. tasks.publish_post: это была причина false timeout,
    # когда автодогенерация следующего поста в очереди задерживала HTTP-ответ
    # на десятки секунд уже после успешной публикации в Telegram).
    if not result.get("already_published"):
        background_tasks.add_task(tasks.post_publish_followup, post_id)
    return result


@app.post("/api/posts/{post_id}/schedule")
def schedule_post(post_id: int, data: ScheduleIn, user: User = Depends(current_user)):
    try:
        when = datetime.fromisoformat(data.scheduled_at.replace("Z", ""))
    except Exception:
        raise HTTPException(400, "Неверный формат даты")
    with session() as s:
        p = _own_post(s, post_id, user)
        p.status = "scheduled"
        p.scheduled_at = when
        s.add(p); s.commit(); s.refresh(p)
        return p.model_dump()


@app.post("/api/posts/{post_id}/reject")
async def reject_post(post_id: int, user: User = Depends(current_user)):
    with session() as s:
        p = _own_post(s, post_id, user)
        channel_id = p.channel_id
        p.status = "rejected"
        s.add(p); s.commit()
    await tasks._refill_if_active(channel_id)
    return {"ok": True}


@app.delete("/api/posts/{post_id}")
async def delete_post(post_id: int, user: User = Depends(current_user)):
    with session() as s:
        p = _own_post(s, post_id, user)
        channel_id = p.channel_id
        s.delete(p); s.commit()
    await tasks._refill_if_active(channel_id)
    return {"ok": True}


# ── Биллинг ───────────────────────────────────────────────────

@app.get("/api/packages")
def packages():
    return config.TOKEN_PACKAGES


async def _sync_yookassa_pending_payments(user_id: int) -> None:
    """
    Резервная синхронизация платежей с YooKassa.

    Нужна на случай, если HTTP-уведомление YooKassa не дошло или было
    пропущено. Проверяем только платежи текущего пользователя со статусом
    pending/waiting_for_capture и уже известным operation_id = payment_id YooKassa.
    """
    if not billing.is_configured():
        return

    with session() as s:
        pending = s.exec(
            select(Payment).where(
                Payment.user_id == user_id,
                Payment.status.in_(["pending", "waiting_for_capture"]),
                Payment.operation_id != "",
            ).order_by(Payment.created_at.desc())
        ).all()
        items = [(p.id, p.operation_id) for p in pending if p.operation_id]

    for local_payment_id, yookassa_payment_id in items:
        try:
            yk_payment = await billing.get_payment(yookassa_payment_id)
        except billing.YooKassaError as exc:
            logger.warning(
                "Не удалось синхронизировать платёж YooKassa %s: %s",
                yookassa_payment_id, exc,
            )
            continue

        status = yk_payment.get("status", "")
        paid = yk_payment.get("paid") is True

        with session() as s:
            pay = s.get(Payment, local_payment_id)
            if not pay or pay.user_id != user_id:
                continue

            if status == "canceled":
                if pay.status != "paid":
                    pay.status = "canceled"
                    s.add(pay)
                    s.commit()
                continue

            if status != "succeeded" or not paid:
                if status and pay.status != status:
                    pay.status = status
                    s.add(pay)
                    s.commit()
                continue

            try:
                actual_amount = round(float((yk_payment.get("amount") or {}).get("value", 0)), 2)
            except Exception:
                actual_amount = 0
            expected_amount = round(float(pay.rub), 2)
            if actual_amount != expected_amount:
                logger.error(
                    "YooKassa sync: сумма не совпала, payment_id=%s, actual=%s, expected=%s",
                    yookassa_payment_id, actual_amount, expected_amount,
                )
                continue

            if pay.status != "paid":
                pay.status = "paid"
                pay.paid_at = datetime.utcnow()
                u = s.get(User, pay.user_id)
                if u:
                    u.token_balance += pay.tokens
                    s.add(u)
                s.add(pay)
                s.commit()
                logger.info(
                    "Платёж YooKassa зачтён через sync: пользователь %s +%s токенов",
                    pay.user_id, pay.tokens,
                )


@app.get("/api/payments")
async def payments(user: User = Depends(current_user)):
    await _sync_yookassa_pending_payments(user.id)
    with session() as s:
        ps = s.exec(
            select(Payment).where(Payment.user_id == user.id).order_by(Payment.created_at.desc())
        ).all()
        return [p.model_dump() for p in ps]


@app.post("/api/billing/buy")
async def buy(data: BuyIn, user: User = Depends(current_user)):
    pkg = config.package_by_id(data.package_id)
    if not pkg:
        raise HTTPException(400, "Пакет не найден")
    if not billing.is_configured():
        raise HTTPException(400, "Приём платежей не настроен")

    label = f"u{user.id}-{data.package_id}-{secrets.token_hex(6)}"
    description = f"Автопост: пакет «{pkg['title']}» ({pkg['tokens']} токенов)"

    with session() as s:
        pay = Payment(
            user_id=user.id,
            package_id=pkg["id"],
            label=label,
            rub=pkg["rub"],
            tokens=pkg["tokens"],
            status="pending",
        )
        s.add(pay)
        s.commit()
        s.refresh(pay)
        local_payment_id = pay.id

    try:
        yk_payment = await billing.create_payment(
            label=label,
            amount_rub=pkg["rub"],
            description=description,
            user_id=user.id,
            package_id=pkg["id"],
            user_email=user.email,
        )
    except billing.YooKassaError as exc:
        with session() as s:
            pay = s.get(Payment, local_payment_id)
            if pay:
                pay.status = "failed"
                s.add(pay)
                s.commit()
        error_msg = str(exc)
        logger.error(f"YooKassa payment error for user {user.id}: {error_msg}")
        raise HTTPException(400, f"Ошибка оплаты: {error_msg}")

    payment_id = yk_payment.get("id", "")
    payment_status = yk_payment.get("status", "pending")
    confirmation_url = (yk_payment.get("confirmation") or {}).get("confirmation_url")
    if not confirmation_url:
        # Раньше Payment оставался pending навсегда -- diagnostics не мог
        # отличить "провайдер не ответил" от "пользователь ещё не оплатил".
        with session() as s:
            pay = s.get(Payment, local_payment_id)
            if pay:
                pay.status = "failed"
                s.add(pay)
                s.commit()
        raise HTTPException(502, "YooKassa не вернула ссылку на оплату")

    with session() as s:
        pay = s.get(Payment, local_payment_id)
        if pay:
            pay.operation_id = payment_id
            pay.status = payment_status or "pending"
            s.add(pay)
            s.commit()

    return {"payment_url": confirmation_url, "label": label, "payment_id": payment_id}


@app.post("/api/yookassa/notify")
async def yookassa_notify(request: Request):
    """Webhook YooKassa. Начисляет токены только после проверки платежа через API YooKassa."""
    try:
        payload = await request.json()
    except Exception:
        logger.warning("YooKassa webhook: невалидный JSON")
        return PlainTextResponse("OK", status_code=200)

    event = payload.get("event", "")
    obj = payload.get("object") or {}
    payment_id = obj.get("id", "")
    if not payment_id:
        logger.warning("YooKassa webhook без payment id: %s", payload)
        return PlainTextResponse("OK", status_code=200)

    try:
        yk_payment = await billing.get_payment(payment_id)
    except billing.YooKassaError as exc:
        logger.warning("Не удалось проверить платёж YooKassa %s: %s", payment_id, exc)
        return PlainTextResponse("retry", status_code=500)

    metadata = yk_payment.get("metadata") or obj.get("metadata") or {}
    label = metadata.get("label", "")
    status = yk_payment.get("status", "")
    paid = yk_payment.get("paid") is True

    with session() as s:
        pay = s.exec(select(Payment).where(Payment.operation_id == payment_id)).first()
        if not pay and label:
            pay = s.exec(select(Payment).where(Payment.label == label)).first()
        if not pay:
            logger.warning("YooKassa webhook: локальный платёж не найден, payment_id=%s, label=%s", payment_id, label)
            return PlainTextResponse("OK", status_code=200)

        pay.operation_id = payment_id

        if event == "payment.canceled" or status == "canceled":
            if pay.status != "paid":
                pay.status = "canceled"
                s.add(pay)
                s.commit()
            return PlainTextResponse("OK", status_code=200)

        if event != "payment.succeeded" or status != "succeeded" or not paid:
            pay.status = status or pay.status
            s.add(pay)
            s.commit()
            return PlainTextResponse("OK", status_code=200)

        try:
            actual_amount = round(float((yk_payment.get("amount") or {}).get("value", 0)), 2)
        except Exception:
            actual_amount = 0
        expected_amount = round(float(pay.rub), 2)
        if actual_amount != expected_amount:
            logger.error(
                "YooKassa webhook: сумма не совпала, payment_id=%s, actual=%s, expected=%s",
                payment_id, actual_amount, expected_amount,
            )
            return PlainTextResponse("OK", status_code=200)

        if pay.status != "paid":
            pay.status = "paid"
            pay.paid_at = datetime.utcnow()
            u = s.get(User, pay.user_id)
            if u:
                u.token_balance += pay.tokens
                s.add(u)
            s.add(pay)
            s.commit()
            logger.info("Платёж YooKassa зачтён: пользователь %s +%s токенов", pay.user_id, pay.tokens)

    return PlainTextResponse("OK", status_code=200)


# ── Раздача сайта ─────────────────────────────────────────────

@app.delete("/api/me")
def delete_account(user: User = Depends(current_user)):
    from database import ChannelRule
    import uuid as _uuid
    uid = user.id
    correlation_id = _uuid.uuid4().hex[:12]
    log_prefix = f"[delete_account#{correlation_id}] uid={uid}"

    def _fail(step: str, e: Exception):
        logger.error(
            f"{log_prefix} ОШИБКА на шаге «{step}»: "
            f"exception_type={type(e).__name__} repr={repr(e)} "
            f"orig={repr(getattr(e, 'orig', None))}"
        )
        raise HTTPException(
            500,
            f"Не удалось удалить аккаунт. Обновите страницу и попробуйте ещё раз. (код: {correlation_id})"
        )

    try:
        with session() as s:
            chans = s.exec(select(Channel).where(Channel.user_id == uid)).all()
            chan_ids = [c.id for c in chans]
            logger.info(f"{log_prefix} шаг 1: найдено channels={len(chan_ids)}")
    except Exception as e:
        _fail("чтение channels", e)

    posts_count = sources_count = rules_count = 0
    try:
        with session() as s:
            for ch in chans:
                for p in s.exec(select(Post).where(Post.channel_id == ch.id)).all():
                    s.delete(p); posts_count += 1
                for src in s.exec(select(Source).where(Source.channel_id == ch.id)).all():
                    s.delete(src); sources_count += 1
                for r in s.exec(select(ChannelRule).where(ChannelRule.channel_id == ch.id)).all():
                    s.delete(r); rules_count += 1
            # Посты могут существовать и без явной привязки в цикле выше, если
            # модель Post хранит user_id напрямую -- подчищаем по user_id тоже.
            for p in s.exec(select(Post).where(Post.user_id == uid)).all():
                s.delete(p); posts_count += 1
            s.commit()
            logger.info(f"{log_prefix} шаг 2: удалены posts={posts_count} sources={sources_count} rules={rules_count}")
    except Exception as e:
        _fail("удаление posts/sources/channel_rules", e)

    try:
        with session() as s:
            for ch in chans:
                ch2 = s.get(Channel, ch.id)
                if ch2:
                    s.delete(ch2)
            s.commit()
            logger.info(f"{log_prefix} шаг 3: удалены channels={len(chan_ids)}")
    except Exception as e:
        _fail("удаление channels", e)

    try:
        with session() as s:
            payments_count = len(s.exec(select(Payment).where(Payment.user_id == uid)).all())
            s.exec(delete(Payment).where(Payment.user_id == uid))
            s.commit()
            logger.info(f"{log_prefix} шаг 4: удалены payments={payments_count}")
    except Exception as e:
        _fail("удаление payments", e)

    try:
        with session() as s:
            referrals_count = (
                len(s.exec(select(Referral).where(Referral.referrer_id == uid)).all())
                + len(s.exec(select(Referral).where(Referral.referred_id == uid)).all())
            )
            s.exec(delete(Referral).where(Referral.referrer_id == uid))
            s.exec(delete(Referral).where(Referral.referred_id == uid))
            s.commit()
            logger.info(f"{log_prefix} шаг 5: удалены referrals={referrals_count}")
    except Exception as e:
        _fail("удаление referrals", e)

    try:
        with session() as s:
            # КРИТИЧНО (root cause найден): User.referred_by -- это FK на
            # user.id у ДРУГИХ пользователей (тех, кто зарегистрировался по
            # реферальному коду этого аккаунта). На Postgres это настоящий
            # FK constraint -- попытка удалить юзера, на которого ссылается
            # чужая строка через referred_by, падает с ForeignKeyViolation.
            # Локальный тест на SQLite этого не поймал, потому что SQLite по
            # умолчанию не enforces FK constraints — баг проявлялся только
            # на реальных продовых аккаунтах, у которых есть рефералы.
            # Обнуляем ссылку (не удаляем самих рефералов, они остаются
            # обычными пользователями, просто без привязки к удалённому
            # пригласившему).
            referred_users = s.exec(select(User).where(User.referred_by == uid)).all()
            for ru in referred_users:
                ru.referred_by = None
                s.add(ru)
            s.commit()
            logger.info(f"{log_prefix} шаг 6: обнулён referred_by у {len(referred_users)} пользователей, которых пригласил uid={uid}")
    except Exception as e:
        _fail("обнуление referred_by у приглашённых пользователей", e)

    # КРИТИЧНО (настоящий root cause, найден по реальному логу Railway):
    # реальная ошибка была
    #   "update or delete on table user violates foreign key constraint
    #    idempotencykey_user_id_fkey ... Key (id)=(21) is still referenced
    #    from table idempotencykey"
    # Очистка IdempotencyKey раньше стояла ПОСЛЕ удаления User (шаг 8) --
    # это и было причиной FK violation: Postgres не разрешает удалить
    # родительскую строку, пока есть ссылающиеся дочерние. Переносим этот
    # шаг ДО удаления User. Чистим и по user_id (это и есть constraint,
    # который реально нарушался), и по channel_id (на случай записей без
    # явного user_id или рассинхрона).
    try:
        with session() as s:
            removed = 0
            for k in s.exec(select(IdempotencyKey).where(IdempotencyKey.user_id == uid)).all():
                s.delete(k); removed += 1
            for cid in chan_ids:
                for k in s.exec(select(IdempotencyKey).where(IdempotencyKey.channel_id == cid)).all():
                    s.delete(k); removed += 1
            s.commit()
            logger.info(f"{log_prefix} шаг 6.5: IdempotencyKey очищены ДО удаления User: {removed}")
    except Exception as e:
        logger.warning(f"{log_prefix} шаг 6.5 (IdempotencyKey) не удался: exception_type={type(e).__name__} repr={repr(e)} orig={repr(getattr(e, 'orig', None))}")
        # НЕ критично само по себе как шаг, НО если эта очистка не сработала
        # (например другая ошибка), то шаг 7 (удаление User) ниже всё равно
        # упадёт с тем же FK violation -- лог покажет это явно на шаге 7.

    try:
        with session() as s:
            u = s.get(User, uid)
            if u:
                s.delete(u)
                s.commit()
            logger.info(f"{log_prefix} шаг 7: пользователь удалён, ВСЁ ОСНОВНОЕ УДАЛЕНИЕ УСПЕШНО")
    except Exception as e:
        logger.error(
            f"{log_prefix} шаг 7 (hard delete) ПРОВАЛЕН: "
            f"exception_type={type(e).__name__} repr={repr(e)} orig={repr(getattr(e, 'orig', None))}"
        )
        # Fallback (task requirement): если есть FK constraint, который мы не
        # предусмотрели заранее (неизвестная таблица), не показываем
        # пользователю мёртвый отказ -- анонимизируем запись через уже
        # существующие поля вместо физического удаления строки. Это не
        # требует изменения схемы (новых колонок типа is_deleted), поэтому
        # безопасно деплоить прямо сейчас. Пользователь теряет доступ
        # (email больше не совпадает ни с одним логином), что эквивалентно
        # удалению аккаунта с точки зрения UX.
        try:
            with session() as s2:
                u2 = s2.get(User, uid)
                if u2:
                    anon_suffix = correlation_id
                    u2.email = f"deleted-{anon_suffix}@deleted.local"
                    u2.password_hash = "deleted"
                    u2.tg_chat_id = None
                    u2.tg_username = ""
                    u2.ref_code = f"DEL-{anon_suffix}"
                    s2.add(u2)
                    s2.commit()
                logger.warning(f"{log_prefix} шаг 7 fallback: запись анонимизирована (soft-delete через существующие поля), физическая строка User сохранена из-за неизвестного FK")
        except Exception as e2:
            logger.error(f"{log_prefix} шаг 7 fallback ТОЖЕ ПРОВАЛЕН: exception_type={type(e2).__name__} repr={repr(e2)}")
            _fail("удаление самого User (включая fallback-анонимизацию)", e)

    return {"ok": True}


@app.post("/api/verify_channel_only")
async def verify_channel_only(data: _VerifyIn, user: User = Depends(current_user)):
    chat = data.tg_chat.strip()
    if not chat:
        raise HTTPException(400, "Укажите @username канала")
    ok, message = await telegram_api.verify_channel(chat)
    return {"ok": ok, "message": message}


@app.patch("/api/me")
def patch_me(data: _MePatch, user: User = Depends(current_user)):
    with session() as s:
        u = s.get(User, user.id)
        if data.notify_new_post is not None: u.notify_new_post = data.notify_new_post
        if data.notify_published is not None: u.notify_published = data.notify_published
        if data.notify_low_tokens is not None: u.notify_low_tokens = data.notify_low_tokens
        if data.tg_chat_id is not None: u.tg_chat_id = data.tg_chat_id
        s.add(u); s.commit()
    return {"ok": True}


@app.post("/api/channels/{channel_id}/consult")
async def consult_channel(channel_id: int, data: _ConsultIn, user: User = Depends(current_user)):
    with session() as s:
        ch = s.get(Channel, channel_id)
        if not ch or ch.user_id != user.id:
            raise HTTPException(404, "Канал не найден")
        from database import ChannelRule
        from sqlmodel import select as sel
        rules = s.exec(sel(ChannelRule).where(ChannelRule.channel_id == channel_id)).all()
        rules_text = "\n".join(f"- {r.rule_text}" for r in rules)
    response, suggested_rule = await generator.consult(ch, data.message, data.history, rules_text)
    return {"response": response, "suggested_rule": suggested_rule}


@app.get("/api/channels/{channel_id}/rules")
def list_rules(channel_id: int, user: User = Depends(current_user)):
    from database import ChannelRule
    from sqlmodel import select as sel
    with session() as s:
        ch = s.get(Channel, channel_id)
        if not ch or ch.user_id != user.id:
            raise HTTPException(404, "Канал не найден")
        rules = s.exec(sel(ChannelRule).where(ChannelRule.channel_id == channel_id)).all()
        return [{"id": r.id, "rule_text": r.rule_text, "created_at": str(r.created_at)} for r in rules]


@app.post("/api/channels/{channel_id}/rules")
def add_rule(channel_id: int, data: _RuleIn, user: User = Depends(current_user)):
    from database import ChannelRule
    with session() as s:
        ch = s.get(Channel, channel_id)
        if not ch or ch.user_id != user.id:
            raise HTTPException(404, "Канал не найден")
        rule = ChannelRule(channel_id=channel_id, rule_text=data.rule_text.strip())
        s.add(rule); s.commit(); s.refresh(rule)
        return {"id": rule.id, "rule_text": rule.rule_text}


@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: int, user: User = Depends(current_user)):
    from database import ChannelRule
    with session() as s:
        rule = s.get(ChannelRule, rule_id)
        if not rule:
            raise HTTPException(404, "Правило не найдено")
        ch = s.get(Channel, rule.channel_id)
        if not ch or ch.user_id != user.id:
            raise HTTPException(403, "Нет доступа")
        s.delete(rule); s.commit()
    return {"ok": True}


@app.post("/api/bot/start")
async def bot_start(request: Request):
    """Webhook для получения /start от бота — привязывает tg_chat_id к аккаунту."""
    try:
        data = await request.json()
        message = data.get("message", {})
        text = message.get("text", "")
        chat_id = message.get("chat", {}).get("id")
        if text.startswith("/start") and chat_id:
            parts = text.split()
            if len(parts) > 1 and parts[1].startswith("u"):
                user_id = int(parts[1][1:])
                with session() as s:
                    u = s.get(User, user_id)
                    if u:
                        u.tg_chat_id = chat_id
                        s.add(u); s.commit()
    except Exception:
        pass
    return {"ok": True}


class _TgVerifyIn(_BaseModel):
    username: str

@app.post("/api/me/verify_tg")
async def verify_tg_username(data: _TgVerifyIn, user: User = Depends(current_user)):
    """Пробует отправить тестовое сообщение пользователю по username."""
    username = data.username.strip()
    if not username.startswith("@"):
        username = "@" + username
    # Bot API не поддерживает отправку по username для личных чатов.
    # Сохраняем username и инструктируем пользователя написать /start боту.
    with session() as s:
        u = s.get(User, user.id)
        u.tg_username = username
        s.add(u); s.commit()
    bot_link = f"https://t.me/{config.TELEGRAM_BOT_USERNAME or 'trpst_bot'}?start=u{user.id}"
    return {
        "ok": True,
        "message": f"Username сохранён. Для активации уведомлений напиши /start боту — он свяжет аккаунты автоматически.",
        "bot_link": bot_link
    }


@app.get("/legal/offer")
def legal_offer():
    return FileResponse("static/legal/offer.html")

@app.get("/legal/privacy")
def legal_privacy():
    return FileResponse("static/legal/privacy.html")

@app.get("/legal/refund")
def legal_refund():
    return FileResponse("static/legal/refund.html")


@app.get("/landing")
def landing():
    return FileResponse("static/landing.html")

@app.get("/robots.txt")
def robots_txt():
    return FileResponse("static/robots.txt", media_type="text/plain")

@app.get("/sitemap.xml")
def sitemap_xml():
    return FileResponse("static/sitemap.xml", media_type="application/xml")

@app.get("/")
def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Запуск ────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=False)
