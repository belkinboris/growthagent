# Как смонтировать платформу Аналитика Воронки в Compass

Платформа целиком живёт под префиксом `/growth` и не имеет глобального
состояния уровня приложения, кроме своей БД и настроек. Обычные посетители
Compass ничего из неё не видят: без валидной сессии владельца каждый
`/growth/api/*` отвечает 401, а страница `/growth/` показывает только форму
входа. Ссылок на `/growth` в публичном UI ставить не нужно.

## Вариант А (рекомендуемый сейчас): два сервиса, один домен

Compass и аналитик остаются отдельными приложениями; реверс-прокси
(nginx/Timeweb Apps) маршрутизирует по пути:

```nginx
location /growth/ {
    proxy_pass http://growth-agent-app:8000;   # процесс growthagent
    proxy_set_header Host $host;
}
location / {
    proxy_pass http://compass-app:8000;
}
```

Плюсы: ничего не переписывать, независимые деплои и падения, у каждого
своя БД. Именно так стоит жить, пока Compass не обзавёлся собственной
аутентификацией.

## Вариант Б: одно приложение (настоящий мердж)

Оба проекта — FastAPI, поэтому роутер платформы подключается напрямую.
В `main.py` Compass:

```python
from app.platform_api import router as growth_router  # пакет app/ из growthagent

app.include_router(growth_router, prefix="/growth")
```

Что нужно учесть при этом:

1. **Зависимости.** В requirements Compass добавить: `sqlmodel`,
   `pydantic-settings`, `apscheduler`, `python-telegram-bot` (последний —
   только если оставляете Telegram-уведомления; сам роутер платформы без
   него работает).
2. **БД.** Аналитик использует свою `DATABASE_URL` (SQLModel, таблицы
   project/alert/metricsnapshot/...). Проще всего дать ему отдельную базу
   или отдельную схему в общем Postgres — с данными Compass он не
   пересекается.
3. **Планировщик.** Циклический сбор метрик запускается в startup
   growthagent (`app/main.py`). При мердже перенести регистрацию джобов
   в startup Compass либо оставить отдельный воркер-процесс.
4. **Env.** Добавить переменные из `.env.example` growthagent
   (PLATFORM_ADMIN_PASSWORD, PLATFORM_SECRET_KEY, YANDEX_API_KEY,
   YANDEX_FOLDER_ID, DATABASE_URL и т.д.).
5. **Доступ.** Сейчас платформа однопользовательская (пароль владельца).
   Когда в Compass появятся аккаунты (`db/models.py` там уже содержит
   заготовку User/UserRole), замените dependency `require_admin` из
   `app/platform_auth.py` на проверку роли Compass — это единственная
   точка входа авторизации, менять больше ничего не нужно.

## Подключение нового проекта к аналитику (шпаргалка)

Пользователь заполняет три поля на вкладке «Проекты»:

| Поле | Пример |
|---|---|
| Название | АвтоПост |
| Base URL | https://projectautopost.ru |
| Токен внутреннего API | значение `TRUEPOST_INTERNAL_API_TOKEN` проекта |

Остальное платформа делает сама: проверяет связь, определяет доступные
`/api/internal/*` endpoints, ставит стандартную разметку воронки
(переопределяется через PATCH `/growth/api/projects/{id}`).

Минимальный контракт для любого нового проекта: endpoint
`GET /api/internal/metrics?period_hours=N` c заголовком
`Authorization: Bearer <token>`, отвечающий JSON с полем `as_of` (ISO-время)
и счётчиками воронки. Полный контракт и опциональные endpoints — CONTRACT.md.
