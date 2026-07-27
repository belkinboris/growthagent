# Контракт подключения продукта к Аналитику Воронки

Этот файл описывает, что должен отдавать ваш продукт, чтобы аналитик мог
показывать воронку, находить потери и предлагать, что чинить. Больше от
продукта ничего не требуется: аналитик сам ходит к вам по сети раз в
несколько часов и только читает.

> Файл восстановлен 27.07.2026: при загрузке 23.07.2026 его содержимое было
> затёрто чужим кодом (там лежали тесты `app/ask.py`). Восстановлен не по
> памяти, а по действующему коду — `app/connectors/*.py` и
> `app/platform_api.py`. Тест `tests/test_contract_doc.py` теперь следит,
> чтобы документ не расходился с кодом и не был затёрт снова.

## Коротко

- Один обязательный endpoint: `GET /api/internal/metrics`.
- Всё остальное — необязательно. Без него аналитик работает, просто видит меньше.
- Авторизация везде одна: заголовок `Authorization: Bearer <токен>`.
- Токен придумываете вы и храните у себя в переменной окружения; тот же
  токен вводится один раз в мастере подключения на `/growth`.
- Наружу отдаются **только агрегаты и анонимные ключи**. Ни почты, ни имён,
  ни telegram_id, ни внутренних id пользователей в ответах быть не должно.

Готовый рабочий код endpoint'ов — в `examples/`:
`truepost_internal_metrics_patch.py` и
`truepost_onboarding_diagnostics_patch.py`. Они написаны под конкретный
продукт (АвтоПост), но структура ответа одинакова для всех.

## Общие правила для всех endpoint'ов

1. **Только чтение.** Аналитик вызывает их регулярно и в любом порядке;
   побочных эффектов быть не должно.
2. **Поле `as_of`** — момент, на который посчитаны числа, в ISO 8601
   (`2026-07-27T18:40:00Z` или со смещением). Для обязательного endpoint'а
   оно обязательно: без него аналитик отклонит ответ. Это защита от того,
   чтобы залипшие данные не выдавались за свежие.
3. **Неизвестный период — ошибка, а не ноль.** Если вы не умеете считать за
   запрошенное окно, честнее ответить ошибкой: ноль будет воспринят как факт
   «ничего не произошло».
4. **404 на необязательном endpoint'е — это нормально.** Аналитик отличает
   «endpoint не реализован» от «endpoint сломан» и пишет владельцу разные
   вещи. Заглушек с выдуманными числами делать не надо.
5. **Ошибка источника — не бизнес-сигнал.** Если endpoint не ответил,
   аналитик пометит источник как недоступный и не станет делать выводов о
   продукте по отсутствующим данным.

## 1. Обязательный: метрики воронки

```
GET /api/internal/metrics?period_hours=24
Authorization: Bearer <токен>
```

`period_hours` аналитик присылает три раза за цикл: `3`, `24` и `168`
(семь дней) — три независимых запроса.

Ответ:

```json
{
  "as_of": "2026-07-27T18:40:00Z",
  "period_hours": 24,
  "users_created": 42,
  "channels_created": 31,
  "posts_generated": 128,
  "payments_started": 5,
  "payments_success": 3,
  "revenue_rub": 2970,
  "pending_payments": 1
}
```

Имена полей — ваши. Аналитик переводит их в свои нормализованные шаги
воронки через разметку `funnel_mapping`, которую вы задаёте при подключении
проекта (в мастере она уже заполнена значениями по умолчанию):

| шаг воронки у аналитика | что это значит | поле по умолчанию |
|---|---|---|
| `signup` | зарегистрировался | `users_created` |
| `activation_1` | сделал первое значимое действие | `channels_created` |
| `activation_2` | дошёл до основной ценности продукта | `posts_generated` |
| `payment_started` | начал оплату | `payments_started` |
| `payment_success` | оплатил | `payments_success` |
| `revenue` | выручка за период, в рублях | `revenue_rub` |

Шаг `traffic` (сколько людей пришло) продукт не отдаёт — он приходит из
Яндекс.Директа или Метрики, если вы их подключите. Без них воронка просто
начинается с регистраций.

`pending_payments` — оплаты, начатые и не завершённые. Поле необязательное,
но по нему аналитик замечает зависшие платежи.

## 2. Необязательные endpoint'ы

Аналитик сам проверяет при подключении, какие из них у вас есть, и
показывает список в мастере. Ни один не обязателен.

### `GET /api/internal/payment-path-diagnostics?period_hours=24`

Разбор пути до оплаты: где именно теряются люди перед деньгами.

Ожидаемые поля: `registrations`, `channels_created`, `post_generations`,
`pricing_viewed`, `payment_cta_clicked`, `payment_started`,
`payment_success`, `payment_failed`, `payment_returned`,
`quota_warning_seen`, `limit_reached`, `biggest_dropoff`,
`likely_explanation`, `missing_data`, `conversion_steps`, `event_map`.

Дополнительно, если считаете: `onboarding_choice_counts`,
`first_post_feedback_good`, `first_post_feedback_bad`,
`first_post_feedback_reasons` (словарь «код причины → сколько раз»),
`post_generations_verified`, `post_generations_unverified`,
`source_breakdown`.

Аналитик понимает и распространённые синонимы имён (`payments_started`
вместо `payment_started` и подобные) — переименовывать у себя ничего не надо.

### `GET /api/internal/landing-funnel-diagnostics?period_hours=24`

Воронка лендинга: `landing_views`, `cta_hero_bot_clicks`,
`cta_hero_app_clicks`, `bot_starts_from_landing`, `web_register_opened`,
`register_success`, `activation_1`.

У каждого поля можно дополнительно отдать пару с суффиксом `_raw`
(например, `landing_views_raw`). Основное значение — уникальные люди,
`_raw` — все события. Аналитик сравнивает их и предупреждает, если счётчик
считает дубли; для выводов о продукте используются только уникальные.

### `GET /api/internal/onboarding-diagnostics?period_hours=24`

Первые шаги нового пользователя: `registrations`, `onboarding_started`,
`create_channel_clicked`, `channels_created`, `channels_verified`,
`first_post_generated`, `payment_started`, `payment_success`,
`errors_count`, `dropoff_by_step` (список), `last_known_step_summary`
(словарь), `notes` (список строк).

### `GET /api/internal/user-journeys?period_hours=24&limit=100`

Анонимные пути отдельных людей — чтобы видеть не только суммы, но и на чём
конкретно человек застрял.

```json
{"ok": true, "period_hours": 24, "as_of": "...", "journeys": [
  {"user_key": "u_febdae54", "source": "direct", "utm_source": "yandex",
   "utm_campaign": "...", "utm_content": "...",
   "registered_at": "...", "channel_created_at": "...",
   "onboarding_choice": null, "first_post_feedback": "good",
   "first_post_feedback_reason": null, "first_post_feedback_at": "...",
   "pricing_viewed_at": null, "payment_cta_clicked_at": null,
   "payment_started_at": null, "payment_success_at": null,
   "payment_failed_at": null,
   "last_step": "channel_created", "stuck_at": "pricing",
   "minutes_since_last_step": 47}
]}
```

`user_key` — **анонимный** ключ (например, короткий хэш). Не telegram_id,
не почта, не имя. Это требование, а не пожелание: приватность — принцип
продукта, и аналитик не должен получать возможность узнать человека.

### `GET /api/internal/user-events?period_minutes=120&limit=200`

Дискретные события для живой ленты — то же, что journeys, но событиями:

```json
{"ok": true, "events": [
  {"event_id": "ev_1029", "event_type": "payment_success",
   "user_key": "u_febdae54", "source": "direct",
   "utm_source": "yandex", "utm_campaign": "...", "utm_content": "...",
   "created_at": "2026-07-27T18:31:00Z",
   "journey_snapshot": {"registered": true, "channel_created": true,
     "pricing_viewed": true, "payment_started": true, "payment_success": true}}
]}
```

`event_id` должен быть устойчивым: по нему аналитик отбрасывает повторы,
чтобы одно событие не попало в ленту дважды.

Типы событий, которые аналитик понимает без настройки: `user_registered`,
`channel_created`, `first_post_feedback_good`, `first_post_feedback_bad`,
`pricing_viewed`, `payment_cta_clicked`, `payment_started`,
`payment_success`, `payment_failed`. Свои типы присылать можно — они
попадут в ленту как есть, и их можно переименовать в интерфейсе.

## Как проверить, что всё подключилось

1. Заведите токен у себя в переменной окружения и поднимите endpoint.
2. Откройте `/growth`, вкладка «Проекты» → «Подключить проект»: название,
   адрес продукта, токен.
3. Кнопка «Проверить подключение» вызовет ваши endpoint'ы и покажет, какие
   ответили. Обязателен только `metrics` — без него подключение не пройдёт.
4. После подключения нажмите «Проверить сейчас» на обзоре: воронка появится
   сразу, динамика — через пару дней наблюдений.

Проверить руками можно так:

```bash
curl -H "Authorization: Bearer ВАШ_ТОКЕН" \
  "https://ваш-продукт.ру/api/internal/metrics?period_hours=24"
```

Ответ без `as_of` или с кодом 401 — самые частые причины, по которым
подключение не проходит.
