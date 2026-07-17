# Growth Agent Watchtower — контракт и структура (v1)

## Принцип универсальности (важно прочитать первым)

Growth Agent — универсальный сервис, который подключается к любому проекту,
отдающему метрики в нормализованном формате. TruePost/АвтоПост — первый
подключённый проект и первый connector, а не основа архитектуры.

В ядре (models.py, analyzer.py, rules.py, llm.py, telegram_bot.py, static/)
не должно быть слов "TruePost" или "АвтоПост" — только "project", "connector",
"Growth Agent". Слово "TruePost" допускается только внутри connectors/truepost.py
и examples/.

Это не значит "строить идеальную абстракцию под воображаемые будущие проекты".
Это значит: называть вещи универсально и держать mapping воронки в данных
(Project.settings_json), а не в коде. Сама форма нормализованной воронки может
и будет меняться, когда появится второй реальный проект — сейчас фиксируем
достаточно общую схему, не более.

### Нормализованная воронка (universal funnel keys)

```
traffic            -- визиты / клики из рекламы
signup              -- регистрация / создание аккаунта
activation_1          -- первый шаг вовлечения (для АвтоПоста: создан канал)
activation_2            -- второй шаг вовлечения (для АвтоПоста: пост сгенерирован)
payment_started            -- начата оплата
payment_success               -- оплата прошла
revenue                          -- выручка за период
```

Список не закрытый — если у будущего проекта 4 шага активации, добавляются
activation_3, activation_4. Schema MetricSnapshot хранит metrics_json как
свободный dict, нормализованные ключи — это соглашение на уровне analyzer.py,
не жёсткая колонка в БД.

### Mapping для TruePost (хранится в Project.settings_json как JSON, не в коде)

```json
{
  "funnel_mapping": {
    "signup": "users_created",
    "activation_1": "channels_created",
    "activation_2": "posts_generated",
    "payment_started": "payments_started",
    "payment_success": "payments_success",
    "revenue": "revenue_rub"
  },
  "metrika_goal_mapping": {
    "signup": "register_success",
    "activation_1": "channel_created",
    "activation_2": "post_generated",
    "payment_started": "payment_started",
    "payment_success": "payment_success"
  }
}
```

В v1 это редактируется руками в БД при настройке проекта, без UI-конструктора —
строить интерфейс для редактирования mapping под единственный реальный проект
не нужно, это будущая работа, когда появится второй клиент.


## Структура репозитория

```
growth-agent-watchtower/
  app/
    main.py              # FastAPI app, роуты /health /status /run /api/*
    config.py             # чтение .env, Settings (pydantic-settings)
    db.py                  # engine, session, init_db()
    models.py              # SQLModel: Project, FunnelStep, Integration,
                            #   MetricSnapshot, MetricBaseline, Alert,
                            #   Recommendation, AgentRun
    scheduler.py            # APScheduler, run_cycle() каждые N секунд
    telegram_bot.py          # команды + кнопки + notifier
    analyzer.py               # ТОЛЬКО детерминированные правила -> Alert
    rules.py                   # таблица правил отдельно от движка анализа
    confidence.py                # расчёт confidence по sample size
    llm.py                         # ТОЛЬКО объяснение, не принятие решений
    health_score.py                 # расчёт Growth Health 0-100
    connectors/
      base.py                        # абстрактный Connector, общий контракт
      truepost.py                     # HTTP-клиент к TruePost internal API
      metrika.py                       # Яндекс.Метрика API
      direct.py                         # Яндекс.Директ API
      yookassa.py                       # YooKassa (read-only)
    static/
      index.html
      app.js
      styles.css
  examples/
    truepost_internal_metrics_patch.py  # что добавить в TruePost
  README_RU.md
  .env.example
  requirements.txt
  Procfile
  railway.toml
```

## Контракт: Growth Agent <-> Project Metrics API

Growth Agent НЕ имеет прямого доступа к БД проекта. Только HTTP. Ниже —
обобщённый контракт; TruePost — первая конкретная реализация этого контракта
(см. connectors/truepost.py и examples/).

### Запрос

```
GET /api/internal/metrics?period_hours={3|24|168}
Authorization: Bearer {PROJECT_INTERNAL_API_TOKEN}
```

Growth Agent всегда запрашивает все три окна (3h / 24h / 7d = 168h) за один прогон,
но как три отдельных запроса — TruePost ничего не должен знать про "окна анализа",
это знание принадлежит Growth Agent.

### Ответ (Project Metrics API)

```json
{
  "period_hours": 3,
  "as_of": "2026-06-19T15:00:00Z",
  "users_created": 3,
  "channels_created": 1,
  "channels_verified": 1,
  "posts_generated": 4,
  "posts_published": 2,
  "payments_started": 1,
  "payments_success": 0,
  "revenue_rub": 0,
  "pending_payments": 1
}
```

`as_of` — обязательное поле. Это момент, на который TruePost насчитал агрегаты
(не момент ответа на запрос). Growth Agent использует его для:
- проверки свежести данных (если as_of старше N минут — алерт "интеграция не отвечает свежими данными", не "проблема в продукте")
- защиты от дублирования снэпшотов при ручных перезапросах через /run

### Соответствие ключей БД <-> целей Метрики

Это соответствие — общий словарь, который используют и analyzer.py (правило 8),
и health_score.py. Фиксируется в config.py как константа, не как магические строки
внутри функций:

| TruePost (БД)        | Метрика (цель)     |
|-----------------------|---------------------|
| users_created         | register_success     |
| channels_created        | channel_created        |
| posts_generated           | post_generated           |
| payments_started            | payment_started            |
| payments_success               | payment_success               |

### Ошибки и недоступность

Подключённый проект может быть недоступен или не отдавать internal API
(старая версия, или connector настроен неправильно). Growth Agent в этом случае:
- помечает Integration.status = "error", Integration.last_error = текст
- НЕ создаёт Alert о проблемах в продукте на основе отсутствующих данных
- создаёт отдельный системный Alert категории "integration_down", который
  не путается с бизнес-алертами (P1/P2) — это инфраструктурная проблема,
  а не сигнал "бизнес теряет деньги"

## Окна анализа (period_key)

MetricSnapshot.period_key всегда один из: "3h", "24h", "7d".
Каждый прогон scheduler создаёт ДО трёх снэпшотов (по одному на каждое окно),
если соответствующий источник данных доступен.

analyzer.py применяет правила к каждому окну отдельно, но финальное сообщение
в Telegram выбирает ОДНО окно как основное для "главного сигнала" — то, где
confidence выше. Если 3h окно даёт "low confidence", а 24h — "medium", в Telegram
идёт вывод по 24h, а 3h упоминается только в контексте ("за 3 часа данных мало,
но за 24 часа уже видно...").

## Confidence — единые правила (confidence.py)

Confidence не привязан к LLM, это детерминированная функция от sample size:

```python
def compute_confidence(sample_size: int, metric_type: str) -> str:
    # пороги различаются для кликов и для конверсионных событий,
    # т.к. клики численно больше, а конверсии (оплаты) редки и
    # значимы уже на малых числах
    ...
    return "low" | "medium" | "high"
```

Эта функция вызывается analyzer.py при создании Alert. LLM получает confidence
как готовое поле и обязан его озвучить, а не пересчитывать.

## Зона ответственности llm.py

LLM вызывается ПОСЛЕ analyzer.py и получает:
- список Alert (уже созданных, с fingerprint/confidence/status)
- метрики по всем трём окнам
- предыдущие 2-3 AgentRun для контекста "что менялось"

LLM возвращает строго структурированный текст по фиксированному шаблону
(см. формат сообщения ниже), не свободную форму. Это нужно, чтобы:
- бот не "уплывал" в стиль и не начинал советовать опасные действия
- одно и то же сообщение можно было собрать и без LLM (LLM_PROVIDER=none),
  просто менее живым языком, по тем же полям

## Формат Telegram-сообщения (с LLM и без LLM — одна структура)

```
Growth Agent — watch-only
Проект: {project.name}

Главный сигнал:
{alert.title}, confidence: {confidence_ru}

Где вероятно проблема:
{hypothesis}

Что проверить:
{check_action}

Что НЕ делать:
{do_not_action}

Метрики (24ч):
Реклама: {spend} ₽ / {clicks} кликов / CTR {ctr}%
Продукт: {signup} регистраций / {activation_1} / {activation_2} / {payment_success} оплат
```

Без LLM (LLM_PROVIDER=none) поля hypothesis/check_action/do_not_action заполняются
шаблонными строками из rules.py, привязанными к конкретному правилу — то есть
каждое правило в rules.py хранит не только условие, но и три заготовленных фразы.

## Что НЕ входит в v1 (явный список, чтобы не расползалось)

- нет прямого доступа к БД проекта (только HTTP к Project Metrics API)
- нет изменения рекламы/ставок/бюджетов
- нет изменения лендинга или продукта
- нет автоматических действий — даже в режимах recommend_only/approval_required/
  autopilot_limited (они есть в UI как заглушки disabled)
- нет UI-конструктора для multi-project и для редактирования funnel_mapping —
  Project как модель универсальна, но в v1 активен один проект (АвтоПост),
  а mapping редактируется руками в БД
- нет MetricBaseline-логики кроме самой таблицы (заполняется вручную или не
  заполняется вообще в v1)
- analyzer.py работает с нормализованными ключами воронки (signup, activation_1,
  ...), а не напрямую с полями TruePost — перевод "поле TruePost -> нормализованный
  ключ" происходит в connectors/truepost.py при помощи funnel_mapping, до того как
  данные попадают в analyzer
