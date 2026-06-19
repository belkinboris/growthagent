"""
Analyzer.

Главная функция analyze() получает нормализованные метрики по трём окнам
(3h / 24h / 7d) для одного проекта и возвращает список AlertCandidate.

analyzer.py НЕ пишет в БД. Сохранение, дедупликация по fingerprint,
escalation и отправка в Telegram -- ответственность service-слоя
(scheduler.py), который вызывается после analyzer.py.

integration_down -- отдельная системная категория. Если источник не
ответил (его нет в metrics.sources_ok), правила, требующие этот источник,
просто не сработают (см. _has() в rules.py) -- это не интерпретируется
как "0 событий", а как "нет данных", и никакого бизнес-алерта не создаётся.
Системный алерт integration_down создаёт сам scheduler.py на основе
статусов Integration, не analyzer.py -- это решение принадлежит слою,
который реально знает про сетевые ошибки коннекторов.
"""

from dataclasses import dataclass

from app.confidence import compute_confidence
from app.models import AlertCategory, AlertSeverity, ConfidenceLevel
from app.rules import get_rules, NormalizedMetrics, RuleResult


@dataclass
class AlertCandidate:
    """
    Результат анализа одного правила на одном окне. Это ещё не Alert
    из БД -- это "предложение создать/обновить алерт", которое service-слой
    превращает в реальный Alert с учётом fingerprint и текущего статуса.
    """

    fingerprint: str
    rule_id: str
    period_key: str
    title: str
    category: AlertCategory
    severity: AlertSeverity
    affected_step: str
    confidence: ConfidenceLevel
    hypothesis: str
    check_action: str
    do_not_action: str
    payload: dict


def analyze_window(
    project_id: int,
    metrics: NormalizedMetrics,
    thresholds: dict | None = None,
) -> list[AlertCandidate]:
    """
    Применяет все правила к метрикам одного окна. Возвращает кандидатов
    для тех правил, условие которых сработало.

    thresholds -- опциональный override порогов из Project.settings_json
    (см. rules.get_rules()). В v1 почти всегда передаётся None -- тогда
    используются дефолты из config.py.
    """
    candidates: list[AlertCandidate] = []

    for rule in get_rules(thresholds):
        result: RuleResult | None = rule.evaluate(metrics)
        if result is None:
            continue

        confidence = compute_confidence(result.sample_size, result.metric_type)

        candidates.append(
            AlertCandidate(
                fingerprint=rule.fingerprint(project_id, metrics.period_key),
                rule_id=result.rule_id,
                period_key=metrics.period_key,
                title=result.title,
                category=result.category,
                severity=result.severity,
                affected_step=result.affected_step,
                confidence=confidence,
                hypothesis=result.hypothesis,
                check_action=result.check_action,
                do_not_action=result.do_not_action,
                payload=result.payload,
            )
        )

    return candidates


def analyze(
    project_id: int,
    metrics_by_window: dict[str, NormalizedMetrics],
    thresholds: dict | None = None,
) -> list[AlertCandidate]:
    """
    metrics_by_window: {"3h": NormalizedMetrics(...), "24h": ..., "7d": ...}

    Применяет правила к каждому окну независимо и собирает все кандидаты
    в один список. Выбор "какое окно главное для Telegram-сообщения"
    происходит НЕ здесь -- это ответственность слоя, который форматирует
    сообщение (см. CONTRACT.md: выбирается окно с более высоким confidence).
    """
    all_candidates: list[AlertCandidate] = []
    for period_key, metrics in metrics_by_window.items():
        all_candidates.extend(analyze_window(project_id, metrics, thresholds))
    return all_candidates


_SEVERITY_SCORE = {
    AlertSeverity.p0: 1000,
    AlertSeverity.p1: 300,
    AlertSeverity.p2: 200,
    AlertSeverity.p3: 100,
    AlertSeverity.info: 0,
}

_CONFIDENCE_SCORE = {
    ConfidenceLevel.high: 80,
    ConfidenceLevel.medium: 40,
    ConfidenceLevel.low: 0,
}

_WINDOW_SCORE = {
    "7d": 30,
    "24h": 20,
    "3h": 10,
}

# Порог "достаточно большой выборки" -- если sample_size кандидата выше
# этого значения, начисляется дополнительный балл. Это отдельно от
# confidence (который уже учитывает sample_size через свои пороги в
# confidence.py), но даёт дополнительный вес очень большим выборкам
# (сотни кликов/событий), а не только пересечение порога "high".
_LARGE_SAMPLE_THRESHOLD = 100
_LARGE_SAMPLE_SCORE = 20


def _priority_score(candidate: "AlertCandidate") -> int:
    """
    weighted priority = severity_score + confidence_score + window_score + sample_score

    P0 спроектирован так, чтобы всегда побеждать: его severity_score (1000)
    превышает максимально возможную сумму confidence+window+sample для
    P1/P2/P3 (80 + 30 + 20 = 130). Поэтому отдельная проверка "P0 всегда
    наверху" не нужна как особый случай -- она следует из самих чисел,
    но это хрупко, если веса поменяют не глядя на этот комментарий.
    """
    score = _SEVERITY_SCORE.get(candidate.severity, 0)
    score += _CONFIDENCE_SCORE.get(candidate.confidence, 0)
    score += _WINDOW_SCORE.get(candidate.period_key, 0)
    if candidate_sample_size(candidate) >= _LARGE_SAMPLE_THRESHOLD:
        score += _LARGE_SAMPLE_SCORE
    return score


def candidate_sample_size(candidate: "AlertCandidate") -> int:
    """
    AlertCandidate не хранит sample_size как отдельное поле (его знает только
    RuleResult внутри analyzer_window, и он используется там для confidence).
    Чтобы не тащить лишнее поле через весь pipeline только для сортировки,
    восстанавливаем разумную оценку из payload -- большинство правил кладут
    туда clicks или signup или их аналог. Это эвристика для priority score,
    не для confidence (confidence уже посчитан раньше и не пересчитывается).
    """
    payload = candidate.payload
    for key in ("clicks", "signup", "activation_1", "payment_started", "pending_payments"):
        if key in payload:
            return int(payload[key])
    return 0


def pick_primary_candidate(candidates: list[AlertCandidate]) -> AlertCandidate | None:
    """
    Выбирает один "главный сигнал" для Telegram-сообщения по весовой формуле:

        score = severity_score + confidence_score + window_score + sample_score

    Это позволяет P2/high (устойчивый сигнал на большой выборке) обгонять
    P1/low (потенциальный шум на маленькой выборке), но P0 спроектирован
    так, чтобы всегда оставаться наверху независимо от confidence/window
    остальных кандидатов.

    При равном score предпочитаем более длинное окно (7d > 24h > 3h) как
    тай-брейк.
    """
    if not candidates:
        return None

    window_tiebreak = {"7d": 0, "24h": 1, "3h": 2}
    confidence_tiebreak = {ConfidenceLevel.high: 0, ConfidenceLevel.medium: 1, ConfidenceLevel.low: 2}

    # При равном score (как в примере P1/low/7d vs P2/high/7d на наших
    # весах -- оба дают 330) сортировка по window ничего не решает, если
    # окна совпадают. Поэтому confidence -- следующий тай-брейк перед
    # window: более уверенный сигнал должен выигрывать у менее уверенного
    # при равном суммарном score, это соответствует духу правки (P1/low
    # не должен автоматически выигрывать у P2/high).
    return sorted(
        candidates,
        key=lambda c: (
            -_priority_score(c),
            confidence_tiebreak.get(c.confidence, 99),
            window_tiebreak.get(c.period_key, 99),
        ),
    )[0]


def secondary_candidates(
    candidates: list[AlertCandidate],
    primary: AlertCandidate | None,
    limit: int = 2,
) -> list[AlertCandidate]:
    """
    Возвращает до `limit` дополнительных сигналов (кроме primary), которые
    стоит упомянуть как "также есть ранний сигнал" в Telegram-сообщении.
    Используется service-слоем при формировании текста, не обязателен к
    использованию каждый раз -- если на окне один кандидат, список будет пустым.
    """
    if primary is None:
        return []
    rest = [c for c in candidates if c.fingerprint != primary.fingerprint]
    rest_sorted = sorted(rest, key=lambda c: -_priority_score(c))
    return rest_sorted[:limit]
