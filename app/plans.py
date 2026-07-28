"""
Тарифы платформы (задачи E1/E2).

Сейчас платформа обслуживает одного владельца (Бориса) — платить некому и
не за что, и это НЕ временная заглушка: `COMMERCIAL_MODE=false` по
умолчанию значит «лимитов и оплаты нет вообще», а не «функция не готова».
Когда владелец решит отдавать аналитик другим фаундерам за деньги, он
включает флаг — и вот тогда лимиты и кнопка оплаты появляются, без правок
кода. Ядро (эта логика) не знает, продаёт ли сейчас платформа что-то.

Тарифы — код, не таблица в БД: их меняют реже, чем читают, а обсуждать
цену в отрыве от кода (что стоит, что входит) — источник рассинхрона.
Если тарифов станет много и они начнут меняться часто — тогда стоит
выносить в БД, не раньше.
"""
from __future__ import annotations

PLANS: dict[str, dict] = {
    "free": {
        "title": "Бесплатный",
        "price_rub": 0,
        # None = без ограничения. У бесплатного тарифа лимит по числу
        # ПРОЕКТОВ, которыми владеет аккаунт, а не по глубине истории или
        # частоте цикла — платформа не должна выглядеть обрезанной для
        # тех, кто честно подключил один продукт бесплатно.
        "max_projects": 1,
    },
    "pro": {
        "title": "Pro",
        "price_rub": 2990,
        "max_projects": None,
    },
}

DEFAULT_PLAN = "free"


def plan_limits(plan: str) -> dict:
    return PLANS.get(plan, PLANS[DEFAULT_PLAN])


def current_plan(session, user_id: int | None) -> str:
    """Активный тариф аккаунта. Без аккаунта (вход владельца платформы по
    паролю из окружения) считаем pro: ограничивать самого себя бессмысленно."""
    if user_id is None:
        return "pro"

    from app.models import PlatformSubscription, utcnow

    from sqlmodel import select

    sub = session.exec(
        select(PlatformSubscription)
        .where(PlatformSubscription.user_id == user_id)
        .where(PlatformSubscription.status == "active")
        .order_by(PlatformSubscription.created_at.desc())
    ).first()
    if sub is None:
        return DEFAULT_PLAN
    paid_until = sub.paid_until
    if paid_until is not None:
        # SQLite отдаёт datetime без часового пояса, даже если писали aware --
        # без нормализации сравнение с utcnow() падает TypeError'ом.
        if paid_until.tzinfo is None:
            from datetime import timezone
            paid_until = paid_until.replace(tzinfo=timezone.utc)
        if paid_until < utcnow():
            return DEFAULT_PLAN
    return sub.plan


def can_create_project(session, user_id: int | None, settings) -> tuple[bool, str | None]:
    """Можно ли аккаунту подключить ещё один проект.

    Пока COMMERCIAL_MODE выключен — можно всегда: лимиты существуют только
    тогда, когда есть смысл их применять (то есть когда платформу реально
    кому-то продают).
    """
    if not getattr(settings, "commercial_mode", False):
        return True, None
    if user_id is None:
        return True, None

    from app import accounts

    plan = current_plan(session, user_id)
    limit = plan_limits(plan)["max_projects"]
    if limit is None:
        return True, None

    owned = len(accounts.user_project_ids(session, user_id))
    if owned >= limit:
        return False, (
            f"На тарифе «{plan_limits(plan)['title']}» можно подключить не больше "
            f"{limit} {_project_word(limit)}. Чтобы добавить ещё один, перейдите на Pro."
        )
    return True, None


def _project_word(n: int) -> str:
    tail, last = n % 100, n % 10
    if 11 <= tail <= 14 or last == 0 or last >= 5:
        return "проектов"
    if last == 1:
        return "проект"
    return "проекта"
