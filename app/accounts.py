"""
Аккаунты платформы: хэширование паролей, регистрация, доступ к проектам.

Часть 1 задачи B1. Здесь появляются пользователи и связь «кто владеет
проектом»; изоляция запросов по владельцу и регистрация в интерфейсе —
следующие части, чтобы одна большая правка не превратилась в непроверяемую.

Почему не bcrypt/passlib: лишняя зависимость на РФ-хостинге — риск, а
pbkdf2_hmac есть в стандартной библиотеке и при 200 000 итерациях даёт
запас на годы. Формат хранения самоописывающийся, поэтому число итераций
можно поднять, не ломая старые пароли.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import PlatformUser, Project, ProjectMember

PBKDF2_ITERATIONS = 200_000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Пароли
# ---------------------------------------------------------------------------


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Возвращает строку "pbkdf2_sha256$<итераций>$<соль>$<хэш>"."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", (password or "").encode("utf-8"), bytes.fromhex(salt), iterations
    )
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    """Проверка пароля. Любой мусор в поле хэша — это «не подошёл»,
    а не исключение: битая запись не должна ронять вход в 500."""
    if not stored:
        return False
    try:
        algo, iterations, salt, expected = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", (password or "").encode("utf-8"), bytes.fromhex(salt), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


# ---------------------------------------------------------------------------
# Пользователи
# ---------------------------------------------------------------------------


class EmailTaken(Exception):
    """Почта уже занята. Отдельный тип, чтобы API ответил 409, а не 500."""


def create_user(
    session: Session,
    email: str,
    password: str,
    *,
    display_name: Optional[str] = None,
    is_owner: bool = False,
) -> PlatformUser:
    normalized = normalize_email(email)
    if not normalized or "@" not in normalized:
        raise ValueError("Нужна почта вида имя@домен")
    if len(password or "") < 8:
        raise ValueError("Пароль короче 8 символов — так нельзя")

    user = PlatformUser(
        email=normalized,
        password_hash=hash_password(password),
        display_name=(display_name or "").strip() or None,
        is_owner=is_owner,
    )
    session.add(user)
    try:
        session.commit()
    except IntegrityError:
        # Уникальный индекс, а не предварительная проверка: два запроса
        # с одной почтой могут прийти одновременно (та же гонка, что была
        # с ежедневным письмом).
        session.rollback()
        raise EmailTaken(normalized)
    session.refresh(user)
    return user


def get_user_by_email(session: Session, email: str) -> Optional[PlatformUser]:
    normalized = normalize_email(email)
    if not normalized:
        return None
    return session.exec(select(PlatformUser).where(PlatformUser.email == normalized)).first()


def authenticate(session: Session, email: str, password: str) -> Optional[PlatformUser]:
    """Возвращает пользователя или None. Причину («нет такого» / «пароль
    не тот») наружу не отдаём намеренно: иначе форма входа превращается
    в проверялку, зарегистрирован ли человек."""
    user = get_user_by_email(session, email)
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login_at = utcnow()
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Доступ к проектам
# ---------------------------------------------------------------------------


def grant_project(session: Session, project_id: int, user_id: int, role: str = "owner") -> ProjectMember:
    """Идемпотентно: повторный вызов возвращает существующую связь."""
    existing = session.exec(
        select(ProjectMember)
        .where(ProjectMember.project_id == project_id)
        .where(ProjectMember.user_id == user_id)
    ).first()
    if existing is not None:
        return existing
    member = ProjectMember(project_id=project_id, user_id=user_id, role=role)
    session.add(member)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return session.exec(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .where(ProjectMember.user_id == user_id)
        ).first()
    session.refresh(member)
    return member


def revoke_project(session: Session, project_id: int, user_id: int) -> None:
    member = session.exec(
        select(ProjectMember)
        .where(ProjectMember.project_id == project_id)
        .where(ProjectMember.user_id == user_id)
    ).first()
    if member is not None:
        session.delete(member)
        session.commit()


def user_project_ids(session: Session, user_id: int) -> list[int]:
    rows = session.exec(select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)).all()
    return sorted({int(r) for r in rows})


def user_can_access(session: Session, user_id: int, project_id: int) -> bool:
    return (
        session.exec(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .where(ProjectMember.user_id == user_id)
        ).first()
        is not None
    )


def adopt_orphan_projects(session: Session, user_id: int) -> int:
    """Проекты без единого владельца отдаём этому пользователю.

    Нужно ровно один раз — при появлении первого аккаунта на базе, где
    проекты уже есть (наш случай: АвтоПост заведён до аккаунтов). Без
    этого после включения изоляции живой проект стал бы невидимым для всех.
    Возвращает число усыновлённых проектов.
    """
    owned = {int(r) for r in session.exec(select(ProjectMember.project_id)).all()}
    adopted = 0
    for project in session.exec(select(Project)).all():
        if project.id in owned:
            continue
        grant_project(session, project.id, user_id)
        adopted += 1
    return adopted
