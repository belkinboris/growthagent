"""
Пример endpoint /api/internal/metrics для TruePost.

Это код для добавления в проект TruePost (АвтоПост), чтобы Growth Agent мог
получать от него метрики. Написан строго под реальную схему database.py
TruePost (модели User, Channel, Post, Payment) -- не абстрактный пример.

Подтверждённые значения статусов (из реального проекта):
- Payment.status == "paid"        -- успешная оплата через YooKassa
- Post.status == "published"      -- пост реально опубликован в канале

Что нужно сделать в TruePost:
1. Сохранить этот файл как internal_metrics.py в корне проекта TruePost
   (туда же, где лежит database.py).
2. Подключить роутер в основном файле приложения (см. инструкцию в самом
   низу этого файла).
3. Добавить переменную окружения TRUEPOST_INTERNAL_API_TOKEN -- секретный
   токен, который Growth Agent будет передавать в заголовке Authorization.
   Тот же токен нужно прописать в Growth Agent как PROJECT_INTERNAL_API_TOKEN.
4. Передеплоить TruePost.

Поле "as_of" в ответе ОБЯЗАТЕЛЬНО -- без него Growth Agent отклонит ответ
как невалидный (см. app/connectors/truepost.py в Growth Agent). Это защита
от того, чтобы агент не интерпретировал "зависшие" данные как актуальные.
"""

import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException
from sqlmodel import select, func

import database
from database import User, Channel, Post, Payment


router = APIRouter()

INTERNAL_API_TOKEN = os.environ.get("TRUEPOST_INTERNAL_API_TOKEN")


def _check_auth(authorization: str | None) -> None:
    """
    Проверяет заголовок Authorization: Bearer {token}.
    Growth Agent всегда передаёт токен именно так (см.
    app/connectors/truepost.py: headers = {"Authorization": f"Bearer {api_token}"}).
    """
    if not INTERNAL_API_TOKEN:
        raise HTTPException(status_code=503, detail="TRUEPOST_INTERNAL_API_TOKEN not configured on this server")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization.removeprefix("Bearer ").strip()
    if token != INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


@router.get("/api/internal/metrics")
async def get_internal_metrics(
    period_hours: int,
    authorization: str | None = Header(default=None),
):
    """
    Growth Agent вызывает этот endpoint с period_hours = 3, 24 или 168
    (7 дней) -- три отдельных запроса за один цикл наблюдения. Каждый
    запрос независим, только чтение, без побочных эффектов в БД.

    database.py использует datetime.utcnow() как default_factory для всех
    created_at -- то есть в БД хранятся НАИВНЫЕ datetime без timezone, в UTC.
    Поэтому period_start здесь тоже наивный UTC, без tzinfo -- иначе
    сравнение datetime с tzinfo и без него в SQL упадёт с ошибкой.
    """
    _check_auth(authorization)

    now_aware = datetime.now(timezone.utc)
    now_naive = now_aware.replace(tzinfo=None)
    period_start = now_naive - timedelta(hours=period_hours)

    with database.session() as s:
        users_created = s.exec(
            select(func.count(User.id)).where(User.created_at >= period_start)
        ).one()

        channels_created = s.exec(
            select(func.count(Channel.id)).where(Channel.created_at >= period_start)
        ).one()

        # В схеме TruePost поле называется "verified", не "is_verified".
        channels_verified = s.exec(
            select(func.count(Channel.id)).where(
                Channel.created_at >= period_start, Channel.verified == True  # noqa: E712
            )
        ).one()

        posts_generated = s.exec(
            select(func.count(Post.id)).where(Post.created_at >= period_start)
        ).one()

        # Post.status == "published" -- подтверждённое значение из реального проекта.
        posts_published = s.exec(
            select(func.count(Post.id)).where(
                Post.created_at >= period_start, Post.status == "published"
            )
        ).one()

        payments_started = s.exec(
            select(func.count(Payment.id)).where(Payment.created_at >= period_start)
        ).one()

        # Payment.status == "paid" -- подтверждённое значение из реального проекта,
        # не "succeeded", как можно было бы предположить по аналогии с YooKassa API.
        payments_success = s.exec(
            select(func.count(Payment.id)).where(
                Payment.created_at >= period_start, Payment.status == "paid"
            )
        ).one()

        revenue_rub = s.exec(
            select(func.coalesce(func.sum(Payment.rub), 0)).where(
                Payment.created_at >= period_start, Payment.status == "paid"
            )
        ).one()

        # "Зависшие" платежи -- не ограничены периодом, потому что зависший
        # платёж может быть создан раньше period_start, но всё ещё актуален
        # как проблема прямо сейчас.
        pending_payments = s.exec(
            select(func.count(Payment.id)).where(Payment.status == "pending")
        ).one()

    return {
        "period_hours": period_hours,
        # "as_of" -- момент, на который ПОСЧИТАНЫ данные. Здесь это "сейчас",
        # потому что запрос идёт напрямую в БД без кэша и без задержки
        # репликации. Если в будущем появится кэш или read-replica с
        # задержкой -- здесь нужно будет передавать реальный момент
        # актуальности данных, не время ответа на запрос.
        "as_of": now_aware.isoformat().replace("+00:00", "Z"),
        "users_created": users_created,
        "channels_created": channels_created,
        "channels_verified": channels_verified,
        "posts_generated": posts_generated,
        "posts_published": posts_published,
        "payments_started": payments_started,
        "payments_success": payments_success,
        "revenue_rub": float(revenue_rub),
        "pending_payments": pending_payments,
    }


# Чтобы подключить роутер, добавьте в основной файл приложения TruePost
# (там, где создаётся FastAPI app, обычно main.py или app.py):
#
#   from internal_metrics import router as internal_metrics_router
#   app.include_router(internal_metrics_router)
#
# И добавьте переменную окружения в TruePost (Railway → Variables):
#   TRUEPOST_INTERNAL_API_TOKEN=придумайте-длинный-случайный-секрет
#
# Тот же секрет пропишите в Growth Agent (.env):
#   PROJECT_INTERNAL_API_TOKEN=тот-же-самый-секрет
#
# Проверка после деплоя (замените значения на свои):
#   curl -H "Authorization: Bearer ваш-секрет" \
#     "https://autopost26.up.railway.app/api/internal/metrics?period_hours=24"
