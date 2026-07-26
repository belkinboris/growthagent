# План: Аналитик Воронки → веб-платформа

Дата начала: 2026-07-23. Ветка во всех репо: `claude/funnel-analytics-migration-jgztix`.

Этот файл — живой журнал прогресса. Если сессия оборвалась, продолжать отсюда:
прочитать раздел «Чеклист прогресса», найти первый незакрытый пункт.

## Контекст

- АвтоПост переехал: `autopost26.up.railway.app` (Railway) → `https://projectautopost.ru`
  (Timeweb, домен на reg.ru). LLM автопоста уже на YandexGPT/DeepSeek (159-ФЗ: серверы в РФ).
- Growth Agent (этот репозиторий) — «аналитик воронки», сейчас интерфейс — Telegram-бот,
  LLM — только Anthropic (недоступен с российских IP), веб-UI — заглушка.
- Экосистема: «Создатель» (projectsozdatel.ru, repo belkinboris/Creator) ведёт
  проект на ранних стадиях (оффер → smoke-лендинг → вердикт по спросу), Аналитик
  подхватывает дальше, когда идея проверена и появился настоящий продукт с
  internal API. В будущем платформы объединяются (вероятно, на домене Создателя,
  путь /growth) — см. CREATOR_INTEGRATION.md. Сырая аналитика не должна быть
  видна обычным пользователям. Первоначально в этом плане фигурировал Compass —
  это была ошибка, Compass к экосистеме не относится (правка от 2026-07-23,
  заметка из репо Compass откачена).

## Целевая архитектура

1. **Веб-платформа вместо ТГ-чата.** FastAPI-приложение growthagent получает:
   - админ-аутентификацию (env `PLATFORM_ADMIN_PASSWORD`, сессионные HMAC-токены,
     httpOnly cookie) — обычный посетитель не видит ничего, кроме страницы логина;
   - все страницы и API платформы живут под префиксом `/growth` (роутер
     `app/platform_api.py` + статика `app/static/platform/`) — так платформу можно
     смонтировать в любое другое FastAPI-приложение (например, «Создатель») на одном домене;
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

- [x] 1. Этот план закоммичен и запушен (growthagent)
- [x] 2. Бэкенд платформы: `app/platform_auth.py`, `app/platform_api.py`,
        монтирование в `app/main.py` под `/growth`, projects CRUD,
        test-connection с автообнаружением endpoints (проверено smoke-тестами
        и мок-сервером internal API — вход/выход/создание/активация работают)
- [x] 3. LLM-роутер в `app/ask.py`: провайдер `yandex` (native + openai-режим),
        новые env в `app/config.py`, обновлён `.env.example` (unit-тест обоих
        режимов пройден)
- [x] 4. Веб-UI: `app/static/platform/index.html` — логин, обзор
        (воронка/интеграции/сигналы), проекты (мастер подключения), чат
- [x] 5. Восстановлены повреждённые корневые `Procfile`, `requirements.txt`,
        `railway.toml` (битые после загрузки через GitHub UI)
- [x] 6. AutoPost: `robots.txt`/`sitemap.xml` → projectautopost.ru,
        подключён `user_events_router` в `main.py` (9/9 интеграционных
        тестов test_user_events.py прошли)
- [x] 7. `CREATOR_INTEGRATION.md` — экосистема Создатель → Аналитик и как
        смонтировать платформу на одном домене (изначально была написана
        COMPASS_INTEGRATION.md — заменена, Compass ни при чём)

## Что дальше (следующая сессия)

- [ ] Деплой growthagent на Timeweb (см. раздел «Деплой» ниже) и проверка
      `/growth` на живом сервере.
- [ ] Ротация internal-токена: сгенерировать новый, поставить в env АвтоПоста
      (`TRUEPOST_INTERNAL_API_TOKEN`) и в проект на платформе (старый засветился в чате).
- [ ] Прогнать чат с аналитиком на реальном YANDEX_API_KEY (режим openai/DeepSeek).
- [ ] Опционально: перенести Метрику/Директ-ключи на уровень проекта
      (сейчас OAuth-токены Яндекса общие, из env).
- [ ] При появлении полноценных аккаунтов в Создателе — заменить require_admin
      на сессию Создателя (одна точка, см. CREATOR_INTEGRATION.md).
- [ ] Ссылка из кабинета Создателя (карточка проекта стадии ③+) на /growth.

## Деплой (когда код готов)

1. growthagent деплоится на любой РФ-хостинг как отдельное приложение; логично —
   Timeweb-аккаунт Создателя: `uvicorn app.main:app --host 0.0.0.0 --port 8080`
   (Timeweb не прокидывает $PORT; Python 3.12; healthcheck /health — уроки
   деплоя Создателя из его README).
   Домен: бесплатный поддомен Создателя, новый покупать не нужно —
   на reg.ru в зоне projectsozdatel.ru добавить A-запись
   (например, `analitik` → публичный IP Timeweb-приложения аналитика),
   привязать analitik.projectsozdatel.ru в Timeweb, платформа будет на
   https://analitik.projectsozdatel.ru/growth без изменений кода.
2. Env: `DATABASE_URL`, `PLATFORM_ADMIN_PASSWORD`, `SECRET_KEY`,
   `LLM_PROVIDER=yandex`, `YANDEX_API_KEY`, `YANDEX_FOLDER_ID`,
   `PROJECT_BASE_URL=https://projectautopost.ru`, `PROJECT_INTERNAL_API_TOKEN=<новый>`;
   Telegram-переменные — опционально.
3. На стороне АвтоПоста: `TRUEPOST_INTERNAL_API_TOKEN=<тот же новый токен>`.
4. Вариант «на одном домене с Создателем»: см. `CREATOR_INTEGRATION.md`.
