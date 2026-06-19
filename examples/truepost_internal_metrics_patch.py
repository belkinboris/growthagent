"""
Пример endpoint /api/internal/metrics для TruePost.

Это НЕ часть Growth Agent -- это код, который нужно добавить в проект
TruePost (АвтоПост), чтобы Growth Agent мог получать от него метрики.

Скопируйте этот код (или возьмите как образец) в TruePost и подключите
роутер к основному FastAPI-приложению TruePost.

Что обязательно нужно от вас в TruePost:
1. Модели User, Channel, Post, Payment (используются здесь как пример --
   замените на реальные импорты из database.py вашего проекта).
2. Переменная окружения TRUEPOST_INTERNAL_API_TOKEN -- секретный токен,
   который Growth Agent будет передавать в заголовке Authorization.
   Тот же токен нужно прописать в Growth Agent как
   PROJECT_INTERNAL_API_TOKEN.
3. Поле "as_of" в ответе ОБЯЗАТЕЛЬНО -- без него Growth Agent отклонит
   ответ как невалидный (см. app/connectors/truepost.py в Growth Agent).
   Это защита от того, чтобы агент не интерпретировал "зависшие" данные
   как актуальные.

Формат ответа -- см. CONTRACT.md в репозитории Growth Agent, раздел
"Контракт: Growth Agent <-> Project Metrics API".
"""

import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException
from sqlmodel import Session, select, func

# Замените на реальные импорты из вашего database.py / models.py
# from database import get_session
# from models import User, Channel, Post, Payment


router = APIRouter()

INTERNAL_API_TOKEN = os.environ.get("TRUEPOST_INTERNAL_API_TOKEN")


def _check_auth(authorization: str | None) -> None:
    """
    Проверяет заголовок Authorization: Bearer {token}.
    Growth Agent всегда передаёт токен именно так (см.
    app/connectors/truepost.py: headers = {"Authorization": f"Bearer {api_token}"}).
    """
    if not INTERNAL_API_TOKEN:
        raise HTTPException(status_code=503, detail="Internal API token not configured on this server")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization.removeprefix("Bearer ").strip()
    if token != INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


@router.get("/api/internal/metrics")
async def get_internal_metrics(
    period_hours: int,
    authorization: str | None = Header(default=None),
    # session: Session = Depends(get_session),  # раскомментируйте и используйте свою сессию
):
    """
    Growth Agent вызывает этот endpoint с period_hours = 3, 24 или 168 (7 дней)
    -- три отдельных запроса за один цикл наблюдения, не один запрос со всеми
    окнами сразу. Каждый запрос независим и не должен иметь побочных эффектов
    (никаких "пересчётов" в БД, только чтение).
    """
    _check_auth(authorization)

    now = datetime.now(timezone.utc)
    period_start = now - timedelta(hours=period_hours)

    # --- Пример агрегации. Замените на реальные запросы к вашей БД. ---
    #
    # with get_session() as session:
    #     users_created = session.exec(
    #         select(func.count(User.id)).where(User.created_at >= period_start)
    #     ).one()
    #
    #     channels_created = session.exec(
    #         select(func.count(Channel.id)).where(Channel.created_at >= period_start)
    #     ).one()
    #
    #     channels_verified = session.exec(
    #         select(func.count(Channel.id)).where(
    #             Channel.created_at >= period_start, Channel.is_verified == True
    #         )
    #     ).one()
    #
    #     posts_generated = session.exec(
    #         select(func.count(Post.id)).where(Post.created_at >= period_start)
    #     ).one()
    #
    #     posts_published = session.exec(
    #         select(func.count(Post.id)).where(
    #             Post.created_at >= period_start, Post.status == "published"
    #         )
    #     ).one()
    #
    #     payments_started = session.exec(
    #         select(func.count(Payment.id)).where(Payment.created_at >= period_start)
    #     ).one()
    #
    #     payments_success = session.exec(
    #         select(func.count(Payment.id)).where(
    #             Payment.created_at >= period_start, Payment.status == "succeeded"
    #         )
    #     ).one()
    #
    #     revenue_rub = session.exec(
    #         select(func.coalesce(func.sum(Payment.amount), 0)).where(
    #             Payment.created_at >= period_start, Payment.status == "succeeded"
    #         )
    #     ).one()
    #
    #     pending_payments = session.exec(
    #         select(func.count(Payment.id)).where(Payment.status == "pending")
    #     ).one()

    # Заглушка для примера -- в реальном коде замените на запросы выше
    users_created = 0
    channels_created = 0
    channels_verified = 0
    posts_generated = 0
    posts_published = 0
    payments_started = 0
    payments_success = 0
    revenue_rub = 0
    pending_payments = 0

    return {
        "period_hours": period_hours,
        # "as_of" -- момент, на который ПОСЧИТАНЫ данные (обычно сейчас, но
        # если у вас есть задержка репликации БД или кэш, укажите реальный
        # момент актуальности данных, не время ответа на запрос).
        "as_of": now.isoformat().replace("+00:00", "Z"),
        "users_created": users_created,
        "channels_created": channels_created,
        "channels_verified": channels_verified,
        "posts_generated": posts_generated,
        "posts_published": posts_published,
        "payments_started": payments_started,
        "payments_success": payments_success,
        "revenue_rub": revenue_rub,
        "pending_payments": pending_payments,
    }


# В основном файле приложения TruePost (main.py) подключите роутер:
#
#   from internal_metrics import router as internal_metrics_router
#   app.include_router(internal_metrics_router)
#
# И добавьте переменную окружения в TruePost:
#   TRUEPOST_INTERNAL_API_TOKEN=какой-то-длинный-случайный-секрет
#
# Тот же секрет пропишите в Growth Agent (.env):
#   PROJECT_INTERNAL_API_TOKEN=тот-же-самый-секрет
