"""
Пример endpoint /api/internal/onboarding-diagnostics для TruePost.

Это НЕ часть Growth Agent -- это код-образец для добавления в проект
TruePost, симметричный examples/truepost_internal_metrics_patch.py.

Growth Agent уже готов работать с этим endpoint (см.
app/connectors/onboarding.py) и graceful обрабатывает его отсутствие --
этот патч НЕ блокирует текущую работу агента, его можно внедрить в
TruePost в любой момент, когда будет удобно.

Зачем это нужно: сейчас агент видит только "N регистраций, активации
нет" -- факт без понимания, на каком именно шаге люди останавливаются.
Этот endpoint даёт детализацию пути пользователя после регистрации.

Минимальный набор данных, который реально нужен Growth Agent:
- registrations -- сколько зарегистрировалось за период;
- last_known_step_summary -- сколько дошло до каждого шага (или null,
  если шаг пока не трекается вообще).

Если в TruePost пока НЕТ событий onboarding_started/channel_created как
таковых -- можно начать с того, что реально есть (например, дата
создания первого канала уже есть в модели Channel), и постепенно
добавлять более детальный tracking.
"""

import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException
from sqlmodel import select, func

import database
from database import User, Channel, Post


router = APIRouter()

INTERNAL_API_TOKEN = os.environ.get("TRUEPOST_INTERNAL_API_TOKEN")


def _check_auth(authorization: str | None) -> None:
    """Тот же механизм авторизации, что в truepost_internal_metrics_patch.py."""
    if not INTERNAL_API_TOKEN:
        raise HTTPException(status_code=503, detail="TRUEPOST_INTERNAL_API_TOKEN not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    if token != INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


@router.get("/api/internal/onboarding-diagnostics")
async def get_onboarding_diagnostics(
    period_hours: int = 24,
    authorization: str | None = Header(default=None),
):
    """
    Минимальная реализация на основе того, что УЖЕ есть в database.py
    TruePost (User.created_at, Channel.created_at, Channel.verified,
    Post.created_at) -- без добавления новых событий/таблиц. Это первый
    шаг; полноценный tracking onboarding_started/create_channel_clicked
    можно добавить позже отдельно, когда появится такая необходимость.
    """
    _check_auth(authorization)

    now_aware = datetime.now(timezone.utc)
    now_naive = now_aware.replace(tzinfo=None)
    period_start = now_naive - timedelta(hours=period_hours)

    notes = []

    with database.session() as s:
        registrations = s.exec(
            select(func.count(User.id)).where(User.created_at >= period_start)
        ).one()

        # "channel_created" -- у нас уже есть это событие напрямую (Channel.created_at).
        # Считаем КАНАЛЫ, созданные пользователями, ЗАРЕГИСТРИРОВАВШИМИСЯ в этом
        # же периоде -- иначе посчитаем канал, созданный старым пользователем,
        # как будто это конверсия нового.
        recent_user_ids = s.exec(
            select(User.id).where(User.created_at >= period_start)
        ).all()

        channels_created = 0
        channels_verified = 0
        first_post_generated = 0

        if recent_user_ids:
            channels_created = s.exec(
                select(func.count(Channel.id)).where(Channel.user_id.in_(recent_user_ids))
            ).one()
            channels_verified = s.exec(
                select(func.count(Channel.id)).where(
                    Channel.user_id.in_(recent_user_ids), Channel.verified == True  # noqa: E712
                )
            ).one()
            first_post_generated = s.exec(
                select(func.count(Post.id)).where(Post.user_id.in_(recent_user_ids))
            ).one()

    # "onboarding_started" -- у нас пока нет отдельного события для этого
    # шага (между регистрацией и созданием канала). Честно отдаём None
    # (не 0!), чтобы Growth Agent знал: это не "0 человек начали онбординг",
    # это "мы пока не умеем такое измерять".
    notes.append("onboarding_started event is not tracked yet -- using channel_created as the next available signal")

    last_known_step_summary = {
        "registered": registrations,
        "onboarding_started": None,  # пока не трекается отдельно
        "channel_created": channels_created,
        "post_generated": first_post_generated,
    }

    dropoff_by_step = [
        {"step": "registered", "users": registrations},
        {"step": "onboarding_started", "users": None},
        {"step": "channel_created", "users": channels_created},
        {"step": "post_generated", "users": first_post_generated},
    ]

    return {
        "period_hours": period_hours,
        "as_of": now_aware.isoformat().replace("+00:00", "Z"),
        "registrations": registrations,
        "onboarding_started": None,
        "create_channel_clicked": None,  # тоже не трекается отдельно от самого создания
        "channels_created": channels_created,
        "channels_verified": channels_verified,
        "first_post_generated": first_post_generated,
        "payment_started": None,  # уже есть в основном /api/internal/metrics, здесь не дублируем
        "payment_success": None,
        "errors_count": None,  # нет соответствующего трекинга ошибок онбординга пока
        "dropoff_by_step": dropoff_by_step,
        "last_known_step_summary": last_known_step_summary,
        "notes": notes,
    }


# Подключение -- так же, как truepost_internal_metrics_patch.py:
#
#   from onboarding_diagnostics import router as onboarding_diagnostics_router
#   app.include_router(onboarding_diagnostics_router)
#
# Использует тот же TRUEPOST_INTERNAL_API_TOKEN, что и основной
# /api/internal/metrics -- отдельный токен не нужен.
#
# Проверка после деплоя:
#   curl -H "Authorization: Bearer ваш-секрет" \
#     "https://autopost26.up.railway.app/api/internal/onboarding-diagnostics?period_hours=24"
#
# Будущее улучшение (не нужно сейчас): добавить реальное событие
# onboarding_started, которое создаётся, когда пользователь видит экран
# "создайте свой первый канал" после регистрации -- это даст Growth Agent
# различить "не дошёл до экрана онбординга" от "увидел экран, но не нажал
# создать канал", сейчас эти два случая неразличимы.
