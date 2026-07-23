# План: Аналитик Воронки → веб-платформа

Дата начала: 2026-07-23. Ветка во всех репо: `claude/funnel-analytics-migration-jgztix`.

Этот файл — живой журнал прогресса. Если сессия оборвалась, продолжать отсюда:
прочитать раздел «Чеклист прогресса», найти первый незакрытый пункт.

## Контекст

- АвтоПост переехал: `autopost26.up.railway.app` (Railway) → `https://projectautopost.ru`
  (Timeweb, домен на reg.ru). LLM автопоста уже на YandexGPT/DeepSeek (159-ФЗ: серверы в РФ).
- Growth Agent (этот репозиторий) — «аналитик воронки», сейчас интерфейс — Telegram-бот,
  LLM — только Anthropic (недоступен с российских IP), веб-UI — заглушка.
- Compass — отдельная платформа (M&A-сделки), FastAPI + SPA, ассистент уже на Yandex
  Responses API (DeepSeek). В будущем платформа аналитика мерджится с Compass
  (возможно на одном домене), при этом сырая аналитика не должна быть видна обычным
  пользователям.

## Целевая архитектура

1. **Веб-платформа вместо ТГ-чата.** FastAPI-приложение growthagent получает:
   - админ-аутентификацию (env `PLATFORM_ADMIN_PASSWORD`, сессионные HMAC-токены,
     httpOnly cookie) — обычный посетитель не видит ничего, кроме страницы логина;
   - все страницы и API платформы живут под префиксом `/growth` (роутер
     `app/platform_api.py` + статика `app/static/platform/`) — так платформу можно
     смонтировать в любое другое FastAPI-приложение (Compass) на одном домене;
   - Telegram-бот остаётся опциональным каналом уведомлений (`BOT_TOKEN` пуст → бот
     не стартует, всё работает через веб).

2. **Мультипроектность + мастер подключения.** Проекты создаются через UI/API,
   а не только через env. Поля, которые заполняет пользователь:
   - обязательные: `name`, `base_url` проекта, `internal_api_token`;
   - опциональные: Метрика (counter_id + OAuth), Директ (логин + OAuth + кампании),
     тип проекта, funnel mapping override.
   Автоматически (без пользователя): проверка связи, автообнаружение доступных
   `/api/internal/*` endpoints (probe), дефолтный funnel mapping, регистрация
   integration-статусов. Контракт internal API описан в `CONTRACT.md` — любой проект,
   который реализует хотя бы `/api/internal/metrics` с Bearer-токеном, подключаем.

3. **LLM-роутер.** `LLM_PROVIDER=yandex` — YandexGPT (native) или DeepSeek через
   Yandex AI Studio Responses API (`YANDEX_API_MODE=openai`), по образцу
   `generator.py` АвтоПоста. Anthropic остаётся как опция.

4. **Подключение к новому АвтоПосту:** `PROJECT_BASE_URL=https://projectautopost.ru`,
   тот же `PROJECT_INTERNAL_API_TOKEN` (⚠️ токен засветился в чате — при деплое
   сгенерировать новый и поставить с обеих сторон).

## Чеклист прогресса

- [ ] 1. Этот план закоммичен и запушен (growthagent)
- [ ] 2. Бэкенд платформы: `app/platform_auth.py`, `app/platform_api.py`,
        монтирование в `app/main.py` под `/growth`, projects CRUD,
        test-connection с автообнаружением endpoints
- [ ] 3. LLM-роутер в `app/ask.py`: провайдер `yandex` (native + openai-режим),
        новые env в `app/config.py`, обновлён `.env.example`
- [ ] 4. Веб-UI: `app/static/platform/` — логин, доска, проекты (мастер
        подключения), чат с аналитиком
- [ ] 5. Восстановлены повреждённые корневые `Procfile`, `requirements.txt`
        (битые после загрузки через GitHub UI)
- [ ] 6. AutoPost: `robots.txt`/`sitemap.xml` → projectautopost.ru,
        подключён `user_events_router` в `main.py`
- [ ] 7. `COMPASS_INTEGRATION.md` — как смонтировать платформу в Compass
        на одном домене; финальный пуш всех трёх репо

## Деплой (когда код готов)

1. growthagent деплоится на Timeweb Cloud (рядом с АвтоПостом) или любой РФ-хостинг:
   `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
2. Env: `DATABASE_URL`, `PLATFORM_ADMIN_PASSWORD`, `SECRET_KEY`,
   `LLM_PROVIDER=yandex`, `YANDEX_API_KEY`, `YANDEX_FOLDER_ID`,
   `PROJECT_BASE_URL=https://projectautopost.ru`, `PROJECT_INTERNAL_API_TOKEN=<новый>`;
   Telegram-переменные — опционально.
3. На стороне АвтоПоста: `TRUEPOST_INTERNAL_API_TOKEN=<тот же новый токен>`.
4. Вариант «на одном домене с Compass»: см. `COMPASS_INTEGRATION.md`.
