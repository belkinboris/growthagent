"""
Человекочитаемый словарь метрик для Аналитика Воронки 2.0.

Главный принцип задачи: запрещены строки вида "19 / 15 / 45 / 0" без
расшифровки, запрещены technical/английские названия (activation_1,
Telegram path) без перевода. Этот модуль -- единственное место, где
живут переводы метрик в русский текст, чтобы при необходимости поправить
формулировку один раз, не искать её по всем файлам форматирования.

Смысл activation_1/activation_2 взят из РЕАЛЬНОГО кода, не придуман:
см. connectors/truepost.py DEFAULT_FUNNEL_MAPPING --
  activation_1 = channels_created (создание канала, событие на пользователя)
  activation_2 = posts_generated (генерация поста, может быть много на
                  одного пользователя -- это НЕ uniq users, это events)
Это и объясняет видимую "аномалию" из задачи ("активация 2 больше
регистраций") -- это не баг данных, это разная единица измерения, которая
теперь явно проговаривается, а не скрывается.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class MetricMeta:
    """
    label -- русское название для отчёта.
    unit -- "users" (уникальные пользователи) | "events" (события, могут
            повторяться на одного пользователя) | "unknown" (смысл не
            установлен точно -- тогда агент обязан написать "неясно,
            это пользователи или события").
    source -- "product" | "metrika" | "direct" -- откуда фактически приходит
              число, нужно для требования "разделять источники".
    explain -- короткое пояснение, почему число может выглядеть нелогично
               (например, "может быть больше регистраций, потому что один
               пользователь генерирует много постов").
    """

    label: str
    unit: str
    source: str
    explain: Optional[str] = None


# Нормализованные ключи воронки (см. rules.py/connectors/truepost.py) ->
# человекочитаемые метаданные. Это ОСНОВНОЙ словарь для продуктовых метрик.
FUNNEL_METRIC_VOCABULARY: dict[str, MetricMeta] = {
    "traffic": MetricMeta(
        label="Клики из рекламы",
        unit="events",
        source="direct",
    ),
    "signup": MetricMeta(
        label="Успешные регистрации",
        unit="users",
        source="product",
    ),
    "activation_1": MetricMeta(
        label="Создали канал",
        unit="users",
        source="product",
        explain="Один пользователь обычно создаёт канал один раз -- это пользователи, не события.",
    ),
    "activation_2": MetricMeta(
        label="Сгенерировали пост",
        unit="events",
        source="product",
        explain=(
            "Это количество ГЕНЕРАЦИЙ постов, не уникальных пользователей -- один человек может "
            "сгенерировать несколько постов. Поэтому это число может быть больше, чем регистраций "
            "или даже чем число пользователей, создавших канал -- это не ошибка данных."
        ),
    ),
    "payment_started": MetricMeta(
        label="Начали оплату",
        unit="users",
        source="product",
    ),
    "payment_success": MetricMeta(
        label="Успешно оплатили",
        unit="users",
        source="product",
    ),
    "revenue": MetricMeta(
        label="Выручка",
        unit="rub",
        source="product",
    ),
}

# Метрики воронки лендинга (см. connectors/landing.py) -> человекочитаемые
# названия. Отдельный словарь, потому что это другой набор полей с другим
# смыслом (просмотры лендинга, клики по CTA -- не пересекается с
# FUNNEL_METRIC_VOCABULARY напрямую, хотя register_success концептуально
# близок к signup).
LANDING_METRIC_VOCABULARY: dict[str, MetricMeta] = {
    "landing_views": MetricMeta(label="Просмотры лендинга", unit="events", source="product"),
    "cta_hero_bot_clicks": MetricMeta(label="Клики по кнопке Telegram", unit="events", source="product"),
    "cta_hero_app_clicks": MetricMeta(label="Клики по кнопке веб-версии", unit="events", source="product"),
    "bot_starts_from_landing": MetricMeta(label="Открытия мини-приложения Telegram", unit="events", source="product"),
    "web_register_opened": MetricMeta(label="Открытия формы регистрации (веб)", unit="events", source="product"),
    "register_success": MetricMeta(label="Успешные регистрации", unit="users", source="product"),
    "activation_1": MetricMeta(
        label="Создали канал", unit="users", source="product",
        explain="Один пользователь обычно создаёт канал один раз -- это пользователи, не события.",
    ),
}

# Пути воронки -- запрещённые "Telegram path"/"Web path" заменяются на это.
PATH_LABELS = {
    "telegram": "Путь через Телеграм",
    "web": "Путь через веб-версию",
}

# Источники данных -> человекочитаемое название источника, используется
# при явном разделении "регистрации продукта vs Метрики vs Директа"
# (требование 3 из задачи).
SOURCE_LABELS = {
    "product": "по данным продукта",
    "metrika": "по цели в Яндекс.Метрике",
    "direct": "по данным Яндекс.Директа",
}


def format_metric_line(normalized_key: str, value, vocabulary: dict[str, MetricMeta] = None) -> str:
    """
    Возвращает одну строку отчёта вида "19 регистраций (по данным продукта)"
    -- единообразная сборка "число + расшифровка + источник", чтобы не
    собирать эти строки руками в каждом месте форматирования по-разному.

    Если normalized_key не найден в словаре -- возвращает явное предупреждение
    вместо тихой заглушки, по правилу задачи: "если неизвестно, не использовать
    название в отчёте, а писать, что событие требует расшифровки".
    """
    vocabulary = vocabulary or FUNNEL_METRIC_VOCABULARY
    meta = vocabulary.get(normalized_key)

    if meta is None:
        return f"Неизвестное событие «{normalized_key}» — требуется расшифровка в коде."

    value_str = "—" if value is None else str(value)
    unit_note = "" if meta.unit == "users" else f" {('событий' if meta.unit == 'events' else meta.unit)}"
    source_note = SOURCE_LABELS.get(meta.source, "")

    line = f"{value_str}{unit_note} — {meta.label.lower() if not meta.label[0].isupper() else meta.label}"
    if source_note:
        line += f" ({source_note})"
    return line


def get_metric_explain(normalized_key: str, vocabulary: dict[str, MetricMeta] = None) -> Optional[str]:
    """Возвращает поясняющий текст для метрики, если он задан (например, про activation_2)."""
    vocabulary = vocabulary or FUNNEL_METRIC_VOCABULARY
    meta = vocabulary.get(normalized_key)
    return meta.explain if meta else None


# Причины отрицательного отзыва о первом посте. Продукт присылает их
# английскими ключами (и разными: и too_promotional, и too_salesy), а в
# тексте владельцу они появлялись как есть -- «Главная причина: wrong_style».
# Этот словарь -- единственное место перевода, чтобы формулировка правилась
# один раз. Незнакомый ключ не выдумываем: возвращаем как есть, но в тексте
# он хотя бы окажется в кавычках рядом с честной пометкой.
FEEDBACK_REASON_LABELS = {
    "too_generic": "слишком общий",
    "wrong_style": "не тот стиль",
    "wrong_topic": "не про тему",
    "too_dry": "слишком сухо",
    "too_promotional": "слишком рекламно",
    "too_salesy": "слишком рекламно",
    "not_my_style": "не тот стиль",
    "off_topic": "не про тему",
    "too_formal": "слишком сухо",
    "other": "другое",
}


def feedback_reason_label(key: str) -> str:
    """Русское название причины отзыва. Незнакомый ключ отдаём как есть --
    выдумывать перевод хуже, чем показать исходный код причины."""
    return FEEDBACK_REASON_LABELS.get(str(key), str(key))


# "other" -- признак того, что человек не написал ничего конкретного, а не
# отдельная содержательная причина. Раньше это выводилось на экран как
# «Главная причина — другое», если таких ответов набиралось больше всего --
# формулировка звучала как содержательный вывод, хотя на деле означала
# «мы не знаем, что именно не понравилось».
GENERIC_REASON_KEYS = {"other"}


@dataclass(frozen=True)
class TopReason:
    label: str          # что показать владельцу
    count: int          # сколько раз встретилась
    is_specific: bool    # False -- это «другое»/неопределено, не конкретная причина


def top_feedback_reason(reasons: dict | None) -> Optional[TopReason]:
    """
    Самая частая КОНКРЕТНАЯ причина недовольства -- специально предпочитает
    «не тот стиль» над «другое», даже если «другое» набрало больше голосов.
    «Другое» -- это отсутствие содержательного ответа, а не сама частая
    причина; называть его «главной причиной» значит выдавать пустоту за
    вывод. Если содержательных причин нет вовсе, честно возвращает
    is_specific=False с количеством неопределённых ответов -- решение,
    что с этим делать, остаётся за текстом, который это показывает.
    """
    if not isinstance(reasons, dict):
        return None
    specific: dict[str, int] = {}
    generic_count = 0
    for key, count in reasons.items():
        try:
            count = int(count)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        if str(key) in GENERIC_REASON_KEYS:
            generic_count += count
            continue
        label = feedback_reason_label(key)
        specific[label] = specific.get(label, 0) + count
    if specific:
        label, count = max(specific.items(), key=lambda kv: kv[1])
        return TopReason(label=label, count=count, is_specific=True)
    if generic_count:
        return TopReason(label="причину не уточнили", count=generic_count, is_specific=False)
    return None


def format_feedback_reasons(reasons: dict | None) -> str:
    """
    Причины отзывов человеческой строкой: «не тот стиль — 1, другое — 1».
    Раньше сюда попадал питоновский словарь целиком, включая нулевые причины:
    «{'too_generic': 0, 'wrong_style': 1, ...}» -- владельцу это читать нельзя.
    Нулевые причины не показываем: они ничего не сообщают.
    """
    if not isinstance(reasons, dict) or not reasons:
        return ""
    # несколько ключей могут переводиться в одну метку -- складываем
    merged: dict[str, int] = {}
    for key, count in reasons.items():
        try:
            count = int(count)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        label = feedback_reason_label(key)
        merged[label] = merged.get(label, 0) + count
    if not merged:
        return ""
    parts = sorted(merged.items(), key=lambda kv: -kv[1])
    return ", ".join(f"{label} — {count}" for label, count in parts)
