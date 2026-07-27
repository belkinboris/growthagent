"""
Аутентификация веб-платформы Аналитика Воронки.

Принцип: платформа -- внутренний инструмент владельца. Обычный посетитель
сайта (в том числе будущие пользователи Compass на том же домене) не должен
видеть сырую аналитику, поэтому:
- без PLATFORM_ADMIN_PASSWORD в окружении платформа заблокирована (503);
- вход -- по одному паролю владельца, сессия -- подписанный HMAC-токен
  в httpOnly cookie (та же схема, что в security.py АвтоПоста);
- секрет подписи -- PLATFORM_SECRET_KEY; если не задан, генерируется
  случайный на процесс (сессии переживают только до рестарта -- честный
  деградированный режим, а не тихая дыра).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request

from app.config import get_settings

SESSION_COOKIE = "ga_platform_session"

_runtime_secret: Optional[str] = None


def _secret() -> str:
    global _runtime_secret
    settings = get_settings()
    if settings.platform_secret_key:
        return settings.platform_secret_key
    if _runtime_secret is None:
        _runtime_secret = secrets.token_hex(32)
    return _runtime_secret


def _sign(payload: str) -> str:
    return hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()


def verify_password(password: str) -> bool:
    settings = get_settings()
    if not settings.platform_admin_password:
        return False
    # Сравниваем хэши, а не строки: hmac.compare_digest на не-ASCII строках
    # бросает TypeError, и пароль с кириллицей ронял вход в 500 вместо
    # честного «неверный пароль». Хэши ещё и уравнивают длину, так что
    # время сравнения не зависит от длины введённого пароля.
    given = hashlib.sha256((password or "").encode("utf-8")).digest()
    expected = hashlib.sha256(settings.platform_admin_password.encode("utf-8")).digest()
    return hmac.compare_digest(given, expected)


@dataclass(frozen=True)
class Identity:
    """Кто пришёл. `user_id is None` — вход по паролю из окружения:
    это владелец платформы, у него аккаунта в базе может и не быть."""

    user_id: Optional[int] = None
    is_owner: bool = True

    @property
    def is_env_owner(self) -> bool:
        return self.user_id is None


def issue_session_token(user_id: Optional[int] = None) -> str:
    """Токен вида "<expires_ts>.<hmac>" или "<expires_ts>:u<id>.<hmac>".

    Личность попала внутрь подписанного payload, а не в отдельную cookie:
    иначе её можно подменить, не трогая подпись. Старый формат (без `:u`)
    остаётся валидным — у владельца не должна разлогиниться вкладка из-за
    выкатки.
    """
    settings = get_settings()
    expires = int(time.time()) + settings.platform_session_ttl_hours * 3600
    payload = str(expires) if user_id is None else f"{expires}:u{int(user_id)}"
    return f"{payload}.{_sign(payload)}"


def resolve_session_token(token: Optional[str]) -> Optional[Identity]:
    """Проверяет подпись и срок. Возвращает личность или None."""
    if not token or "." not in token:
        return None
    payload, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(payload)):
        return None

    expires_part, _, user_part = payload.partition(":")
    try:
        if int(expires_part) <= time.time():
            return None
    except ValueError:
        return None

    if not user_part:
        return Identity(user_id=None, is_owner=True)
    if not user_part.startswith("u"):
        return None
    try:
        return Identity(user_id=int(user_part[1:]), is_owner=False)
    except ValueError:
        return None


def validate_session_token(token: Optional[str]) -> bool:
    return resolve_session_token(token) is not None


def _token_from_request(request: Request) -> Optional[str]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    return token


def require_admin(request: Request) -> Identity:
    """FastAPI-dependency: пускает только с валидной сессией и возвращает,
    кто именно пришёл.

    Поддерживает и cookie (браузер), и Authorization: Bearer (скрипты/API) --
    Bearer сравнивается с тем же форматом токена, что выдаёт /api/login.

    Платформа считается настроенной, если задан пароль владельца ИЛИ в базе
    есть хотя бы один аккаунт: иначе клиент, зарегистрировавшийся сам, не
    смог бы войти без переменной окружения на чужом сервере.
    """
    settings = get_settings()
    identity = resolve_session_token(_token_from_request(request))

    if not settings.platform_admin_password and not _has_any_account():
        raise HTTPException(status_code=503, detail="Платформа не настроена: задайте PLATFORM_ADMIN_PASSWORD")
    if identity is None:
        raise HTTPException(status_code=401, detail="Не авторизован")
    if identity.is_env_owner and not settings.platform_admin_password:
        # Пароль владельца убрали из окружения -- старые «владельческие»
        # сессии обязаны умереть вместе с ним.
        raise HTTPException(status_code=401, detail="Не авторизован")
    return identity


def _has_any_account() -> bool:
    """Есть ли хоть один аккаунт. Ошибку базы трактуем как «нет»: на пустой
    или недоступной базе вход всё равно невозможен, а 500 из dependency
    выглядел бы как поломка платформы."""
    try:
        from sqlmodel import select

        from app.db import get_session
        from app.models import PlatformUser

        with get_session() as session:
            return session.exec(select(PlatformUser.id)).first() is not None
    except Exception:
        return False
