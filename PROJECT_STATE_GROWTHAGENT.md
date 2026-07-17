DATABASE_URL=
PUBLIC_URL=

BOT_TOKEN=
BOT_ADMIN_CHAT_IDS=

LLM_PROVIDER=none
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=claude-sonnet-4-6

PROJECT_NAME=АвтоПост
PROJECT_TYPE=telegram_saas
PROJECT_CONNECTOR=truepost
PROJECT_BASE_URL=https://autopost26.up.railway.app
PROJECT_INTERNAL_API_TOKEN=

YANDEX_OAUTH_TOKEN=
METRIKA_COUNTER_ID=109941485
METRIKA_GOAL_IDS_JSON={"signup": 123456, "activation_1": 123457, "activation_2": 123458, "payment_started": 123459, "payment_success": 123460}

# DIRECT_OAUTH_TOKEN опционален, если задан YANDEX_OAUTH_TOKEN выше --
# Growth Agent использует один общий OAuth-токен Яндекса для Метрики и
# Директа, если отдельный токен для Директа не указан. Задавайте отдельно
# только если используете разные токены/аккаунты для этих двух сервисов.
DIRECT_OAUTH_TOKEN=
DIRECT_CLIENT_LOGIN=
# Список ID кампаний через запятую, без пробелов. Если оставить пустым --
# Growth Agent будет получать отчёт по ВСЕМ кампаниям аккаунта.
DIRECT_CAMPAIGN_IDS=12345,67890
DIRECT_SANDBOX=false

# Технические детали Direct connector (не требуют действий, для справки):
# запрос к Reports Service выполняется с заголовком returnMoneyInMicros=true,
# поэтому суммы Cost/AvgCpc в ответе API приходят умноженными на 1_000_000 --
# connector делит их обратно, чтобы получить рубли. Если вы видите аномально
# большие суммы расхода в логах -- это первое, что стоит проверить.

YOOKASSA_SHOP_ID=
YOOKASSA_SECRET_KEY=

WATCH_INTERVAL_SECONDS=10800
DEFAULT_MODE=watch_only
INTEGRATION_STALE_MINUTES=180
