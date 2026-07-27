"""
Готовый код endpoint'а для стороны клиента (задача B3).

Зачем. Чтобы подключить продукт, клиент должен поднять у себя
`GET /api/internal/metrics` по контракту (CONTRACT.md). Раньше он читал
документ и писал код сам — это самый дорогой шаг подключения и главное
место, где люди отваливаются. Здесь тот же контракт выдан кодом под его
стек: скопировал, подставил свои запросы к базе, задеплоил.

Почему шаблоны, а не генерация «под ключ». Аналитик не знает схему чужой
базы и не должен её угадывать: выдуманный запрос молча вернёт неверные
числа, а это прямое нарушение принципа честности данных. Поэтому места,
где нужны свои запросы, помечены явно и по-русски — их видно глазами, их
нельзя случайно оставить.

Ядро продуктовой специфики здесь нет: шаблоны говорят только о
нормализованных шагах воронки из CONTRACT.md.
"""

from __future__ import annotations

from typing import Optional

# Переменная окружения на стороне клиента. Одно имя во всех шаблонах:
# в инструкции, в коде и в мастере подключения оно должно совпадать,
# иначе человек ищет опечатку вместо подключения.
TOKEN_ENV_VAR = "ANALYTICS_INTERNAL_API_TOKEN"

_FASTAPI = '''"""
Endpoint для Аналитика Воронки. Только чтение, без побочных эффектов.
Подключите роутер в своём приложении: app.include_router(router)
"""
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException

router = APIRouter()

INTERNAL_API_TOKEN = os.environ.get("{env_var}")


def _check_auth(authorization: str | None) -> None:
    if not INTERNAL_API_TOKEN:
        raise HTTPException(status_code=503, detail="{env_var} не задан на сервере")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Нужен заголовок Authorization: Bearer")
    if authorization.removeprefix("Bearer ").strip() != INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="Неверный токен")


@router.get("/api/internal/metrics")
async def internal_metrics(period_hours: int, authorization: str | None = Header(default=None)):
    _check_auth(authorization)

    # Аналитик спрашивает три окна за цикл: 3, 24 и 168 часов.
    period_start = datetime.now(timezone.utc) - timedelta(hours=period_hours)

    # ↓↓↓ ЗАМЕНИТЕ НА СВОИ ЗАПРОСЫ К БАЗЕ ↓↓↓
    # Считайте события, случившиеся ПОСЛЕ period_start.
    users_created = 0      # сколько человек зарегистрировалось
    activation_1 = 0       # сделали первое значимое действие
    activation_2 = 0       # дошли до основной ценности продукта
    payments_started = 0   # начали оплату
    payments_success = 0   # оплатили
    revenue_rub = 0        # выручка за период, в рублях
    # ↑↑↑ ЗАМЕНИТЕ НА СВОИ ЗАПРОСЫ К БАЗЕ ↑↑↑

    return {{
        # as_of обязателен: без него аналитик отклонит ответ, чтобы не
        # выдать залипшие данные за свежие.
        "as_of": datetime.now(timezone.utc).isoformat(),
        "period_hours": period_hours,
        "users_created": users_created,
        "channels_created": activation_1,
        "posts_generated": activation_2,
        "payments_started": payments_started,
        "payments_success": payments_success,
        "revenue_rub": revenue_rub,
    }}
'''

_DJANGO = '''"""
Endpoint для Аналитика Воронки. Только чтение, без побочных эффектов.

1. Сохраните файл как internal_metrics.py в своём приложении.
2. В urls.py: path("api/internal/metrics", internal_metrics)
"""
import os
from datetime import timedelta

from django.http import JsonResponse
from django.utils import timezone

INTERNAL_API_TOKEN = os.environ.get("{env_var}")


def internal_metrics(request):
    if not INTERNAL_API_TOKEN:
        return JsonResponse({{"detail": "{env_var} не задан на сервере"}}, status=503)

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:].strip() != INTERNAL_API_TOKEN:
        return JsonResponse({{"detail": "Неверный токен"}}, status=401)

    try:
        period_hours = int(request.GET.get("period_hours", 24))
    except ValueError:
        return JsonResponse({{"detail": "period_hours должен быть числом"}}, status=400)

    # Аналитик спрашивает три окна за цикл: 3, 24 и 168 часов.
    period_start = timezone.now() - timedelta(hours=period_hours)

    # ↓↓↓ ЗАМЕНИТЕ НА СВОИ ЗАПРОСЫ К БАЗЕ ↓↓↓
    # Например: User.objects.filter(date_joined__gte=period_start).count()
    users_created = 0      # сколько человек зарегистрировалось
    activation_1 = 0       # сделали первое значимое действие
    activation_2 = 0       # дошли до основной ценности продукта
    payments_started = 0   # начали оплату
    payments_success = 0   # оплатили
    revenue_rub = 0        # выручка за период, в рублях
    # ↑↑↑ ЗАМЕНИТЕ НА СВОИ ЗАПРОСЫ К БАЗЕ ↑↑↑

    return JsonResponse({{
        # as_of обязателен: без него аналитик отклонит ответ, чтобы не
        # выдать залипшие данные за свежие.
        "as_of": timezone.now().isoformat(),
        "period_hours": period_hours,
        "users_created": users_created,
        "channels_created": activation_1,
        "posts_generated": activation_2,
        "payments_started": payments_started,
        "payments_success": payments_success,
        "revenue_rub": revenue_rub,
    }})
'''

_EXPRESS = '''// Endpoint для Аналитика Воронки. Только чтение, без побочных эффектов.
// Подключите роутер: app.use(require("./internalMetrics"))
const express = require("express");
const router = express.Router();

const INTERNAL_API_TOKEN = process.env.{env_var};

router.get("/api/internal/metrics", async (req, res) => {{
  if (!INTERNAL_API_TOKEN) {{
    return res.status(503).json({{ detail: "{env_var} не задан на сервере" }});
  }}
  const auth = req.get("Authorization") || "";
  if (!auth.startsWith("Bearer ") || auth.slice(7).trim() !== INTERNAL_API_TOKEN) {{
    return res.status(401).json({{ detail: "Неверный токен" }});
  }}

  // Аналитик спрашивает три окна за цикл: 3, 24 и 168 часов.
  const periodHours = Number(req.query.period_hours) || 24;
  const periodStart = new Date(Date.now() - periodHours * 3600 * 1000);

  // ↓↓↓ ЗАМЕНИТЕ НА СВОИ ЗАПРОСЫ К БАЗЕ ↓↓↓
  const usersCreated = 0;      // сколько человек зарегистрировалось
  const activation1 = 0;       // сделали первое значимое действие
  const activation2 = 0;       // дошли до основной ценности продукта
  const paymentsStarted = 0;   // начали оплату
  const paymentsSuccess = 0;   // оплатили
  const revenueRub = 0;        // выручка за период, в рублях
  // ↑↑↑ ЗАМЕНИТЕ НА СВОИ ЗАПРОСЫ К БАЗЕ ↑↑↑

  res.json({{
    // as_of обязателен: без него аналитик отклонит ответ, чтобы не
    // выдать залипшие данные за свежие.
    as_of: new Date().toISOString(),
    period_hours: periodHours,
    users_created: usersCreated,
    channels_created: activation1,
    posts_generated: activation2,
    payments_started: paymentsStarted,
    payments_success: paymentsSuccess,
    revenue_rub: revenueRub,
  }});
}});

module.exports = router;
'''

_ANY_STACK = '''# Если вашего стека нет в списке

Поднимите у себя GET /api/internal/metrics — на любом языке.

Токен держите в переменной окружения (в остальных шаблонах она называется
{env_var}; имя может быть любым, важно только,
чтобы то же значение вы ввели в поле «Токен внутреннего API» выше).

Что проверяет аналитик:
  1. Заголовок Authorization: Bearer <токен>. Чужой или отсутствующий
     токен — ответ 401.
  2. Параметр period_hours: приходит 3, 24 и 168 (семь дней), три
     независимых запроса за цикл.
  3. Ответ — JSON, обязательно с полем as_of в формате ISO 8601.
     Без as_of ответ считается невалидным: это защита от того, чтобы
     залипшие данные не выдали за свежие.

Ответ:

{{
  "as_of": "2026-07-27T18:40:00Z",
  "period_hours": 24,
  "users_created": 42,      // зарегистрировались
  "channels_created": 31,   // первое значимое действие
  "posts_generated": 128,   // дошли до основной ценности
  "payments_started": 5,
  "payments_success": 3,
  "revenue_rub": 2970
}}

Имена полей могут быть вашими: при подключении вы указываете, какое поле
какому шагу воронки соответствует. Считайте события, случившиеся за
последние period_hours часов. Endpoint должен быть только на чтение.
'''

# Порядок важен: первым идёт то, что предлагается по умолчанию.
SNIPPETS: dict[str, dict] = {
    "fastapi": {
        "title": "Python · FastAPI",
        "language": "python",
        "filename": "internal_metrics.py",
        "template": _FASTAPI,
    },
    "django": {
        "title": "Python · Django",
        "language": "python",
        "filename": "internal_metrics.py",
        "template": _DJANGO,
    },
    "express": {
        "title": "Node.js · Express",
        "language": "javascript",
        "filename": "internalMetrics.js",
        "template": _EXPRESS,
    },
    "any": {
        "title": "Другой стек",
        "language": "text",
        "filename": "",
        "template": _ANY_STACK,
    },
}

DEFAULT_STACK = "fastapi"


def available_stacks() -> list[dict]:
    return [{"key": key, "title": value["title"]} for key, value in SNIPPETS.items()]


def build_snippet(stack: Optional[str] = None) -> dict:
    """Код endpoint'а под выбранный стек. Неизвестный стек — это не ошибка:
    отдаём описание контракта, с ним человек сделает endpoint на чём угодно."""
    key = (stack or DEFAULT_STACK).strip().lower()
    if key not in SNIPPETS:
        key = "any"
    spec = SNIPPETS[key]
    return {
        "stack": key,
        "title": spec["title"],
        "language": spec["language"],
        "filename": spec["filename"],
        "env_var": TOKEN_ENV_VAR,
        "code": spec["template"].format(env_var=TOKEN_ENV_VAR),
        "steps": [
            f"Придумайте секретный токен и положите его в переменную окружения {TOKEN_ENV_VAR}.",
            "Вставьте код к себе и подставьте свои запросы к базе там, где помечено.",
            "Задеплойте продукт.",
            "Вернитесь сюда, заполните три поля выше и нажмите «Проверить подключение».",
        ],
    }
