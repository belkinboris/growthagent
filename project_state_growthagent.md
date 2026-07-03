# PROJECT_STATE_GROWTHAGENT — Growth Agent / Аналитик Воронки

Человекочитаемый реестр для владельца проекта. Отделён от project_state_truepost —
это состояние отдельного проекта (Growth Agent), который читает данные TruePost
через internal API, но является самостоятельным Telegram-ботом/сервисом.

---

## Текущий статус: Board Refactor + Founder Live Feed (v2)

**BUILD_MARKER:** нужно обновить в config.py на `growth-agent-board-refactor-2026-06-29`
(не забыть при деплое)

**Статус:** Полный рефакторинг структуры команд под "одна главная доска".
Двухуровневая событийная архитектура (user-events → user-journeys → aggregate deltas).

---

## Структура команд (после рефакторинга)

### Owner-facing (показываются в /start)
| Команда | Что делает | Builder |
|---|---|---|
| `/board` | Главная доска: РЕШЕНИЕ / НЕДЕЛЯ / ФОКУС / СЕГОДНЯ / НЕ МЕНЯТЬ | `build_board_report()` |
| `/today` | Alias к `/board` (`cmd_today = cmd_board`, тот же объект функции) | — |
| `/journeys` | Последние 5-10 путей пользователей, не агрегаты | `build_journeys_report()` |
| `/checks` | Активная проверка, правила решения, кандидат, что отложено | `build_checks_report()` |
| `/experiments` | Alias к `/checks` (`cmd_experiments = cmd_checks`) | — |
| `/funnel` | Конверсия по шагам + главный провал | `build_funnel_report()` |
| `/pay` | Только payment_started/success/failed/stuck | `build_pay_report()` |
| `/ads` | Direct/TG Ads/unknown + downstream качество | `build_ads_report()` + `format_source_breakdown()` |
| `/alerts` | Статус live-уведомлений + `on/off/smart/founder` | `cmd_alerts` |
| `/live` | Alias к `/alerts` (`cmd_live = cmd_alerts`) | — |
| `/status` | Только health системы, не бизнес-отчёт | `cmd_status` |

### Технические (не в /start, только в /help)
`/ping`, `/build`, `/run` (короткий: обновляет + показывает /board), `/run_full` (длинный owner report), `/mode`, `/settings`, `/debug`, `/test_metrika`, `/test_direct`, `/deep_direct`, `/check_landing`, `/check_onboarding`, `/alerts_legacy` (старая система Alert-объектов, переименована, не в help)

### Изменения относительно предыдущей версии
- `source_breakdown` перенесён из `/funnel` в `/ads` (funnel теперь "сухая воронка")
- Старый длинный `/run` (owner report через `format_cycle_message`) доступен как `/run_full`
- Новый короткий `/run` = "Данные обновлены." + `build_board_report()`
- Старый `/alerts` (legacy Alert objects, open/escalated/resolved) переименован в `/alerts_legacy`
- Новый `/alerts` управляет режимом live-уведомлений (Founder Live Feed)

---

## Founder Live Feed — архитектура (v2, приоритет источников)

`notify_product_signal_deltas()` в scheduler.py — главная точка входа, вызывается
после каждого успешного live `/run`, ПЕРЕД перезаписью кэша payment_path.

**Уровень 0 — alert_mode gate:**
`get_alert_mode(session, project_id)` — хранится через `DeepDiagnosticsCache`
(ключ `alert_mode_v1`), без миграции схемы БД. Режимы: `off` / `smart` (дефолт) / `founder`.
Если `off` — вообще ничего не отправляется, все остальные уровни пропускаются.

**Уровень 1 — user-events (Founder Live Feed, приоритетный):**
`app/connectors/user_events.py` → `GET /api/internal/user-events?period_minutes=120&limit=200`
Дискретные события с `event_id` — дедупликация по `user_event:<event_id>`.
Фильтруется по smart/founder (`should_notify_event()`).
Если >10 событий за цикл → дайджест (`format_feed_digest()`).
**TODO(TruePost): этот endpoint может быть ещё не задеплоен** — тогда
`fetch_user_events` вернёт `status="not_found"`, и код падает на уровень 2.

**Уровень 2 — user-journeys (snapshot diffing, fallback):**
`app/connectors/user_journeys.py` → `GET /api/internal/user-journeys?period_hours=24&limit=100`
Снимок текущего состояния каждого пути (не дискретные события).
Дедупликация по `journey:<user_key>:<step>:<timestamp>`.
Уже был реализован в предыдущей сессии — работает как есть.

**Уровень 3 — aggregate deltas (последний fallback):**
Старейшая логика: сравнение агрегатов `payment_path` между циклами `/run`.
Дедупликация по `<event_type>:<project_id>:<current_value>`.
Работает даже если TruePost вообще ничего нового не задеплоил.

### Дедупликация — общая инфраструктура
`NotificationLog` модель (models.py), `was_notified()`/`mark_notified()` (service.py).
Запись пишется **только после** успешной HTTP-отправки — при сбое следующий цикл
попробует снова.

### Anti-spam
- User-events: `FOUNDER_FEED_DIGEST_THRESHOLD = 10`
- Journeys: `DIGEST_THRESHOLD_JOURNEYS = 20` (в `_notify_from_journeys`)
- Aggregate deltas: `DIGEST_THRESHOLD_PER_RUN = 20`

### Stuck detection
`STUCK_TARIFF_SCREEN_MINUTES = 45` — если `pricing_viewed_at` есть, `payment_started_at`
нет, и `minutes_since_last_step >= 45` → синтетическое stuck-уведомление.
Реализовано на уровнях journeys (`pick_recent_stuck_journey`) и в `detect_stuck_events`
для events-уровня (менее точное — endpoint user-events не даёт `minutes_since_last_step`
напрямую в контракте, полноценный stuck остаётся на journeys-уровне).

---

## Raw post_generations — принцип (не менять!)

`activation_2` / `post_generations` — техническая метрика, смешивает ручные действия
пользователя и автогенерацию системой (подтверждено на проде: 1 регистрация → 3 поста
без 3 ручных действий).

**Правило:** НИКОГДА не используется как доказательство вовлечённости, нигде в
owner-facing текстах (`/board`, `/checks`, `/journeys`, `/funnel`, `/today`, Founder
Live Feed сообщения). Событие `post_generated`/`auto_post_created` явно фильтруется
на уровне `user_events.py` connector (`_IGNORED_EVENT_TYPES`), даже если TruePost
когда-нибудь его пришлёт.

**Future task для TruePost:** добавить `first_post_shown`/`first_post_ready` —
событие когда пользователь реально УВИДЕЛ первый результат (не когда система его
создала в фоне). Это заменит текущий плейсхолдер "Данные по первому посту собираются
через отзыв" на точный шаг воронки.

---

## Ключи кэша (все)

| Ключ | Что хранит | Обновляется |
|---|---|---|
| `7d` | legacy deep_direct granular | `/deep_direct` |
| `direct_intelligence_24h` | classify_search_queries результат | `/deep_direct` |
| `payment_path_7d` | payment-path diagnostics агрегаты | каждый live `/run` |
| `user_journeys_24h` | per-user journeys snapshot | каждый live `/run` (если endpoint доступен) |
| `alert_mode_v1` | режим live-уведомлений (off/smart/founder) | `/alerts on\|off\|smart\|founder` |
| `landing_funnel_24h` | landing funnel анализ | `/check_landing` |
| `onboarding_*` | onboarding diagnostics | `/check_onboarding` |

---

## DB schema — новая модель

**`NotificationLog`** (models.py) — журнал отправленных live-уведомлений.
Поля: `id`, `project_id`, `event_key` (индексирован), `event_type`, `user_id`
(nullable, анонимный `user_key`), `sent_at`, `payload_json`.
Не требует миграции для будущих изменений — `payload_json` расширяем свободно.

**Прочие факты (не изменились):**
- `Channel.verified` (не `is_verified`)
- `Payment.status == "paid"` (не `"succeeded"`)
- `Post.status == "published"`
- Datetimes — naive UTC
- JSON columns не принимают нативные datetime — `_make_json_safe()` в scheduler.py

---

## Известная проблема: Railway "ran out of memory"

Периодически приходит `Your deployment for web in elegant-playfulness ran out of
memory within the production environment and crashed.`

**Вероятные причины (по мере вероятности):**
1. Polling TruePost каждые 1-2 минуты (user-events) + user-journeys + payment-path
   в одном цикле `/run` — если `/run` вызывается часто (по расписанию), это может
   накапливать httpx-клиенты или держать большие JSON в памяти дольше необходимого.
2. `NotificationLog` растёт бесконечно — сейчас нет TTL/очистки старых записей.
   При активном трафике таблица может разрастись, но это не должно вызывать OOM
   на уровне процесса (это БД, не память процесса) — только если делается
   full-table scan без индекса. `event_key` индексирован, `project_id` тоже
   через FK — должно быть ок, но стоит проверить план запроса `IN (candidate_keys)`
   при большом списке.
3. `journeys`/`events` списки от TruePost — если TruePost отдаёт сотни/тысячи
   записей и Growth Agent держит их в памяти (в `result_json` кэша плюс в
   локальных переменных) — это может быть источником пиков памяти.

**Что можно сделать (не делал, требует отдельного решения):**
- Проверить Railway plan — free/hobby tier обычно даёт 512MB-1GB RAM,
  этого может не хватать с ростом трафика. Апгрейд плана — самое быстрое решение.
- Добавить `limit` в user-events/user-journeys запросы поменьше (сейчас 200/100) —
  если TruePost начнёт отдавать много записей, уменьшить.
- Добавить периодическую очистку старых `NotificationLog` записей (например,
  старше 30 дней) — не блокер сейчас, но станет проблемой при масштабе.
- Проверить не создаётся ли `httpx.AsyncClient` без явного закрытия где-то
  в коде — уже используется `async with httpx.AsyncClient(...)` везде, но
  стоит перепроверить `user_events.py`/`user_journeys.py`/`payment_path.py`
  на утечки соединений при частых ошибках/таймаутах.
- Смотреть Railway Metrics (Memory graph) — если память растёт монотонно
  между рестартами (а не резко скачет) — это утечка в самом процессе, не
  разовый пик от большого запроса.

**Не делал:** глубокую диагностику причины — это требует доступа к Railway
Metrics графику памяти во времени, которого у меня нет. Рекомендую сначала
посмотреть график: если пилообразный (растёт плавно, потом OOM, потом рестарт
и всё сначала) — это утечка памяти в Python-процессе. Если резкий скачок
в момент конкретного запроса — это разовая большая нагрузка (например,
TruePost вернул неожиданно много journeys/events).

---

## Бизнес-контекст (данные на 29.06.2026)

- ~15-30 регистраций, ~12-26 каналов, 0 оплат (варьируется по последним тестам)
- pricing_viewed = 1-2 (событие настроено, данных мало)
- payment_started = 0 (попыток оплаты нет)
- Активная проверка: "Путь после первого поста" — почему создают канал,
  но почти не открывают тарифы
- Главный кандидат следующего эксперимента: "Очередь постов на неделю"

---

## Отложено / не делать сейчас

- Recurring subscriptions — до первых стабильных оплат
- per-query registration attribution — технически недоступно через Direct API
- Изменения лендинга, ставок, бюджета, цен, тарифов, UX TruePost — не трогать
- Dream Team / мультиагентная архитектура — явно отложено по запросу владельца
- Отдельное репо для Growth Agent — не делаем, всё в одном репо

---

## Тесты

`python -m compileall app && python -m pytest tests/ -q`

**Результат на 29.06.2026:** 359/359 passed

Тест-файлы:
- `tests/test_daily_review.py` (главный, ~3700+ строк) — classifier, spend gate,
  commercial layer, P0 stability, board/checks/journeys refactor, Founder Live
  Feed (user_events connector, smart/founder modes, digest, dedup, stuck detection)
- `tests/test_payment_path.py` — payment_path connector и formatter
- `tests/test_owner_report.py` — legacy owner_report.py layer
- `tests/test_direct.py`, `test_metrika.py`, `test_deep_direct_runtime.py`

**Примечание:** `test_daily_review.py` разросся очень сильно (несколько тысяч строк).
Стоит рассмотреть разбивку на `test_commercial_report.py`, `test_notifications.py`,
`test_user_journeys.py`, `test_user_events.py` в следующей итерации для читаемости
— пока отложено, чтобы не тратить время на рефакторинг тестов вместо фич.

---

## Файловая структура (ключевые файлы)

```
app/
  commercial_report.py    # весь owner-facing текст:
                          #   build_board_report() -- главная доска (NEW)
                          #   build_checks_report() -- проверки (NEW, было build_experiments_report)
                          #   build_journeys_report() -- пути пользователей (NEW)
                          #   build_run_report() -- длинный отчёт, теперь для /run_full
                          #   build_funnel_report(), build_pay_report(), build_ads_report()
                          #   build_deep_direct_status()
  notifications.py        # вся логика уведомлений:
                          #   compute_deltas/format_notification -- aggregate deltas (v0, fallback уровень 3)
                          #   build_journey_event_key/format_journey_* -- per-user journeys (v1, уровень 2)
                          #   should_notify_event/format_feed_* -- Founder Live Feed (v2, уровень 1, NEW)
  connectors/
    payment_path.py       # агрегаты payment-path-diagnostics
    user_journeys.py      # snapshot per-user journeys
    user_events.py        # NEW -- дискретные user-events (Founder Live Feed)
    traffic_sources.py    # source_breakdown parsing/formatting (теперь используется в /ads)
    direct.py, onboarding.py
  service.py               # +get_alert_mode/set_alert_mode (NEW, хранится через DeepDiagnosticsCache)
                          # +was_notified/mark_notified (dedup)
                          # +USER_JOURNEYS_CACHE_PERIOD_KEY, ALERT_MODE_CACHE_KEY
  scheduler.py              # notify_product_signal_deltas() -- главная точка входа,
                          #   3-уровневый fallback: events -> journeys -> deltas
                          # _notify_from_events/_notify_from_journeys/_notify_from_deltas
                          # _send_telegram_notification -- общий helper отправки
  telegram_bot.py           # cmd_board (NEW, главный), cmd_today = cmd_board (alias)
                          # cmd_checks (NEW), cmd_experiments = cmd_checks (alias)
                          # cmd_journeys (NEW)
                          # cmd_run (короткий, compact=True), cmd_run_full (длинный, compact=False)
                          # cmd_alerts (NEW, управление режимом), cmd_live = cmd_alerts (alias)
                          # cmd_alerts_legacy (переименован из старого cmd_alerts)
                          # cmd_start, cmd_help -- обновлены под новую структуру
  models.py                # +NotificationLog
tests/
  test_daily_review.py     # основной тест-файл, все новые тесты здесь
```
