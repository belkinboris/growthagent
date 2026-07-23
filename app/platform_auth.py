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
    return hmac.compare_digest(password or "", settings.platform_admin_password)


def issue_session_token() -> str:
    """Токен вида "<expires_ts>.<hmac>". Никаких данных пользователя внутри
    нет намеренно: платформа однопользовательская (владелец)."""
    settings = get_settings()
    expires = int(time.time()) + settings.platform_session_ttl_hours * 3600
    payload = str(expires)
    return f"{payload}.{_sign(payload)}"


def validate_session_token(token: Optional[str]) -> bool:
    if not token or "." not in token:
        return False
    payload, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(payload)):
        return False
    try:
        return int(payload) > time.time()
    except ValueError:
        return False


def require_admin(request: Request) -> None:
    """FastAPI-dependency: пускает только владельца с валидной сессией.

    Поддерживает и cookie (браузер), и Authorization: Bearer (скрипты/API) --
    Bearer сравнивается с тем же форматом токена, что выдаёт /api/login.
    """
    settings = get_settings()
    if not settings.platform_admin_password:
        raise HTTPException(status_code=503, detail="Платформа не настроена: задайте PLATFORM_ADMIN_PASSWORD")

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not validate_session_token(token):
        raise HTTPException(status_code=401, detail="Не авторизован")
