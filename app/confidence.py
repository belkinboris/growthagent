"""
Расчёт confidence (low / medium / high) для алертов.

Полностью детерминированный, не зависит от LLM. LLM получает готовый
confidence и обязан его озвучить человеческим языком, а не пересчитывать
или оспаривать.

Пороги разные для "трафиковых" метрик (клики, показы -- их много, шум
сглаживается быстрее) и "конверсионных" метрик (регистрации, оплаты --
их мало, но даже 1-2 события на таком объёме значимы).
"""

from app.models import ConfidenceLevel


# Пороги sample_size для трафиковых метрик (клики, показы, визиты)
_TRAFFIC_THRESHOLDS = {
    "low": 15,     # меньше 15 кликов -- выводы преждевременны
    "medium": 50,  # 15-49 -- первый сигнал, но выборка небольшая
    # 50+ -- high
}

# Пороги sample_size для конверсионных метрик (регистрации, активации, оплаты)
_CONVERSION_THRESHOLDS = {
    "low": 3,      # меньше 3 событий -- одна случайность может всё исказить
    "medium": 10,  # 3-9 -- сигнал есть
    # 10+ -- high
}


def compute_confidence(sample_size: int, metric_type: str) -> ConfidenceLevel:
    """
    metric_type: "traffic" | "conversion"

    sample_size -- это объём данных, на основании которого сделан вывод.
    Для правила "clicks без signup" sample_size = clicks (знаменатель).
    Для правила "signup без activation_1" sample_size = signup.
    """
    if metric_type == "traffic":
        thresholds = _TRAFFIC_THRESHOLDS
    elif metric_type == "conversion":
        thresholds = _CONVERSION_THRESHOLDS
    else:
        raise ValueError(f"Unknown metric_type: {metric_type}")

    if sample_size < thresholds["low"]:
        return ConfidenceLevel.low
    if sample_size < thresholds["medium"]:
        return ConfidenceLevel.medium
    return ConfidenceLevel.high


CONFIDENCE_RU = {
    ConfidenceLevel.low: "данных мало, вывод осторожный",
    ConfidenceLevel.medium: "есть первый сигнал, но выборка небольшая",
    ConfidenceLevel.high: "проблема подтверждается, данных достаточно",
}
