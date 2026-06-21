"""
Direct Deep Diagnostics.

Анализирует granular-данные Директа (ad group / search query level), чтобы
найти НЕ просто "низкая конверсия", а конкретный подозрительный сегмент:
группу объявлений, которая жжёт бюджет без результата, или кластер
нерелевантных поисковых запросов.

Ключевой принцип: это диагност, не "вечно что-то оптимизирующий советчик".
- Если данных мало -- explicit "недостаточно данных", не уверенный вывод.
- Если атрибуция регистраций к Direct не подтверждена -- никогда не пишем
  "клики дали регистрации" как причинно-следственную связь.
- Никаких автоматических действий -- только read-only анализ и рекомендации.

Этот модуль НЕ пишет в БД и не отправляет Telegram -- он принимает
granular-данные (из connectors/direct.fetch_ad_group_report/
fetch_search_query_report) и возвращает DiagnosticsResult. Persistence
(кэширование через DeepDiagnosticsCache) -- ответственность service.py,
как analyzer.py не пишет в БД для обычных алертов.
"""

from dataclasses import dataclass, field
from typing import Optional

from app.config import DEFAULT_QUERY_CLUSTERS, MIN_CLICKS_FOR_DEEP_DIAGNOSTICS
from app.models import AttributionStatus


# ---------------------------------------------------------------------------
# Кластеризация запросов (rule-based matching, без ML)
# ---------------------------------------------------------------------------


def classify_query(query: str, query_clusters: dict) -> Optional[dict]:
    """
    Простой rule-based matcher: lower-case запрос, проверяет include/exclude
    термины каждого кластера. Возвращает {"group": "good"|"irrelevant",
    "cluster_key": ..., "label": ...} для первого совпавшего кластера, или
    None, если запрос не попал ни в один кластер (это нормально -- не всё
    обязано быть классифицировано).

    Порядок проверки: сначала "irrelevant" кластеры, затем "good" -- это
    осознанный выбор: если запрос одновременно похож на нерелевантный
    кластер (по include) и не содержит exclude-термина, который убрал бы
    его из irrelevant, безопаснее пометить как irrelevant и привлечь
    внимание человека, чем тихо посчитать его хорошим.
    """
    query_lower = query.lower()

    for group in ("irrelevant", "good"):
        clusters = query_clusters.get(group, {})
        for cluster_key, cluster_def in clusters.items():
            include_terms = cluster_def.get("include", [])
            exclude_terms = cluster_def.get("exclude", [])

            matched_include = any(term.lower() in query_lower for term in include_terms)
            if not matched_include:
                continue

            matched_exclude = any(term.lower() in query_lower for term in exclude_terms)
            if matched_exclude:
                continue

            return {"group": group, "cluster_key": cluster_key, "label": cluster_def.get("label", cluster_key)}

    return None


def get_query_clusters(project_settings: dict) -> dict:
    """
    Возвращает query_clusters из Project.settings_json, либо
    DEFAULT_QUERY_CLUSTERS, если не задано или повреждено. Graceful
    fallback -- словарь специфичен для проекта, но система не должна
    падать, если он не настроен.
    """
    clusters = project_settings.get("query_clusters")
    if not clusters or not isinstance(clusters, dict):
        return DEFAULT_QUERY_CLUSTERS
    if "good" not in clusters and "irrelevant" not in clusters:
        # Структура совсем не похожа на ожидаемую -- лучше дефолт, чем
        # тихо работать с пустыми кластерами.
        return DEFAULT_QUERY_CLUSTERS
    return clusters


# ---------------------------------------------------------------------------
# Результат диагностики
# ---------------------------------------------------------------------------


@dataclass
class DiagnosticFinding:
    """Одна находка диагностики -- подозрительная группа, кластер запросов, и т.д."""

    finding_type: str  # "ad_group_budget_drain" | "irrelevant_query_cluster" | "low_ctr_segment" | "good_query_cluster"
    severity: str  # "P1" | "P2" | "info"
    confidence: str  # "low" | "medium" | "high"
    title: str
    detail: str
    recommended_action: Optional[str] = None
    payload: dict = field(default_factory=dict)


@dataclass
class DiagnosticsResult:
    """
    Полный результат deep diagnostics за период. insufficient_data=True
    означает, что данных было слишком мало для уверенных находок --
    findings в этом случае может быть пустым или содержать только info-level
    наблюдения, не "выводы".
    """

    period_key: str
    attribution_status: AttributionStatus
    total_clicks: int
    total_cost: float
    insufficient_data: bool
    findings: list = field(default_factory=list)
    main_finding: Optional[DiagnosticFinding] = None
    good_findings: list = field(default_factory=list)  # отдельно good_query_cluster -- не проблемы, а возможности

    def to_dict(self) -> dict:
        """Для сериализации в DeepDiagnosticsCache.result_json."""
        return {
            "period_key": self.period_key,
            "attribution_status": self.attribution_status.value,
            "total_clicks": self.total_clicks,
            "total_cost": self.total_cost,
            "insufficient_data": self.insufficient_data,
            "findings": [vars(f) for f in self.findings],
            "main_finding": vars(self.main_finding) if self.main_finding else None,
            "good_findings": [vars(f) for f in self.good_findings],
        }


# ---------------------------------------------------------------------------
# Правила диагностики
# ---------------------------------------------------------------------------


def _rule_ad_group_budget_drain(
    ad_group_rows: list,
    total_clicks: int,
    total_cost: float,
    irrelevant_ad_group_ids: set,
    low_ctr_ad_group_ids: set,
) -> list:
    """
    Если одна группа забирает значимую долю кликов/расхода. Порог "значимая
    доля" -- >= 40% кликов ИЛИ >= 40% расхода от общего объёма.

    ВАЖНО: концентрация бюджета в одной группе сама по себе НЕ проблема --
    это может быть просто основной/самый успешный сегмент кампании. Поэтому
    severity всегда "info" ("сегмент для проверки"), а не "P2" ("проблема"),
    если только эта же группа не пересекается с другой находкой (нерелевантный
    кластер запросов или низкий CTR в этой же группе) -- тогда есть реальное
    основание поднять severity до P2, потому что концентрация бюджета
    подтверждена плохим качеством трафика, не просто масштабом.

    irrelevant_ad_group_ids / low_ctr_ad_group_ids -- множества ad_group_id,
    уже найденных другими правилами как проблемные -- передаются явно, не
    вычисляются внутри этой функции, чтобы порядок вызова правил был
    очевиден и не создавал скрытой зависимости одного правила от внутренней
    реализации другого.
    """
    findings = []
    if total_clicks == 0 and total_cost == 0:
        return findings

    for row in ad_group_rows:
        clicks_share = (row["clicks"] / total_clicks) if total_clicks > 0 else 0
        cost_share = (row["cost"] / total_cost) if total_cost > 0 else 0

        if clicks_share < 0.4 and cost_share < 0.4:
            continue

        ad_group_id = row["ad_group_id"]
        has_corroborating_evidence = (
            ad_group_id in irrelevant_ad_group_ids or ad_group_id in low_ctr_ad_group_ids
        )

        group_label = row["ad_group_name"] or row["ad_group_id"]

        if has_corroborating_evidence:
            findings.append(DiagnosticFinding(
                finding_type="ad_group_budget_drain",
                severity="P2",
                confidence="medium" if row["clicks"] < MIN_CLICKS_FOR_DEEP_DIAGNOSTICS else "high",
                title=f"Группа «{group_label}» концентрирует бюджет и показывает признаки проблемы",
                detail=(
                    f"Группа «{group_label}» дала {round(clicks_share * 100)}% кликов и "
                    f"{round(cost_share * 100)}% расхода за период, и при этом отдельно найдены "
                    f"признаки низкого качества трафика в этой же группе (нерелевантные запросы "
                    f"или низкий CTR)."
                ),
                recommended_action="Проверить релевантность объявлений и запросов в этой группе.",
                payload={"ad_group_id": ad_group_id, "clicks": row["clicks"], "cost": row["cost"],
                         "clicks_share": round(clicks_share, 2), "cost_share": round(cost_share, 2)},
            ))
        else:
            # Нет подтверждения, что это плохо -- просто крупный сегмент.
            # Осторожная формулировка, info-уровень: не "проблема", а
            # "стоит посмотреть", потому что это может быть и хорошо
            # (основной рабочий канал, который и должен забирать бюджет).
            findings.append(DiagnosticFinding(
                finding_type="ad_group_budget_drain",
                severity="info",
                confidence="medium" if row["clicks"] < MIN_CLICKS_FOR_DEEP_DIAGNOSTICS else "high",
                title=f"Сегмент для проверки: группа «{group_label}»",
                detail=(
                    f"Группа «{group_label}» концентрирует {round(clicks_share * 100)}% кликов и "
                    f"{round(cost_share * 100)}% расхода за период. Это не обязательно проблема — "
                    f"возможно, это просто основной рабочий сегмент. Признаков низкого качества "
                    f"трафика в этой группе отдельно не найдено."
                ),
                recommended_action="Можно посмотреть конверсию этой группы отдельно, если данные позволяют.",
                payload={"ad_group_id": ad_group_id, "clicks": row["clicks"], "cost": row["cost"],
                         "clicks_share": round(clicks_share, 2), "cost_share": round(cost_share, 2)},
            ))

    return findings


def _rule_low_ctr_segment(ad_group_rows: list, min_impressions: int = 200, ctr_threshold: float = 1.0) -> tuple[list, set]:
    """
    Группа с большим числом показов, но низким CTR -- возможная проблема
    в объявлении/оффере, не в семантике запросов. min_impressions защищает
    от вывода на маленькой выборке показов. Возвращает также множество
    ad_group_id с низким CTR -- используется _rule_ad_group_budget_drain()
    для повышения severity при корреляции находок.
    """
    findings = []
    low_ctr_ad_group_ids: set = set()
    for row in ad_group_rows:
        if row["impressions"] < min_impressions:
            continue
        if row["ctr"] < ctr_threshold:
            low_ctr_ad_group_ids.add(row["ad_group_id"])
            findings.append(DiagnosticFinding(
                finding_type="low_ctr_segment",
                severity="P2",
                confidence="medium",
                title=f"Низкий CTR в группе «{row['ad_group_name'] or row['ad_group_id']}»",
                detail=(
                    f"Группа «{row['ad_group_name'] or row['ad_group_id']}» получила "
                    f"{row['impressions']} показов с CTR {row['ctr']}% -- ниже {ctr_threshold}%."
                ),
                recommended_action="Проверить текст объявления и соответствие оферу запросам пользователей.",
                payload={"ad_group_id": row["ad_group_id"], "impressions": row["impressions"], "ctr": row["ctr"]},
            ))
    return findings, low_ctr_ad_group_ids


def _rule_query_clusters(query_rows: list, query_clusters: dict) -> tuple[list, list, set]:
    """
    Классифицирует все запросы, агрегирует клики/расход по кластеру.
    Возвращает (irrelevant_findings, good_findings, irrelevant_ad_group_ids) --
    последнее множество используется _rule_ad_group_budget_drain(), чтобы
    повышать severity только для групп, где реально подтверждены
    нерелевантные запросы, не для любой крупной группы.
    """
    cluster_aggregates: dict = {}  # (group, cluster_key) -> {"label", "clicks", "cost", "queries": [], "ad_group_ids": set}

    for row in query_rows:
        classification = classify_query(row["query"], query_clusters)
        if classification is None:
            continue

        key = (classification["group"], classification["cluster_key"])
        if key not in cluster_aggregates:
            cluster_aggregates[key] = {
                "label": classification["label"], "clicks": 0, "cost": 0.0,
                "queries": [], "ad_group_ids": set(),
            }

        cluster_aggregates[key]["clicks"] += row["clicks"]
        cluster_aggregates[key]["cost"] += row["cost"]
        cluster_aggregates[key]["queries"].append(row["query"])
        if row.get("ad_group_id"):
            cluster_aggregates[key]["ad_group_ids"].add(row["ad_group_id"])

    irrelevant_findings = []
    good_findings = []
    irrelevant_ad_group_ids: set = set()

    for (group, cluster_key), agg in cluster_aggregates.items():
        if agg["clicks"] == 0:
            continue  # показы были, кликов не было -- не основание для finding на этом уровне

        top_queries = sorted(set(agg["queries"]))[:5]

        if group == "irrelevant":
            irrelevant_ad_group_ids |= agg["ad_group_ids"]
            irrelevant_findings.append(DiagnosticFinding(
                finding_type="irrelevant_query_cluster",
                severity="P1" if agg["clicks"] >= MIN_CLICKS_FOR_DEEP_DIAGNOSTICS else "P2",
                confidence="high" if agg["clicks"] >= MIN_CLICKS_FOR_DEEP_DIAGNOSTICS else "medium",
                title=f"Нерелевантный кластер запросов: {agg['label']}",
                detail=(
                    f"Кластер «{agg['label']}» дал {agg['clicks']} кликов и {round(agg['cost'], 2)} ₽ "
                    f"расхода. Запросы не похожи на целевую аудиторию продукта."
                ),
                recommended_action="Рассмотреть минус-фразы для этих запросов.",
                payload={"cluster_key": cluster_key, "clicks": agg["clicks"], "cost": round(agg["cost"], 2),
                         "top_queries": top_queries},
            ))
        elif group == "good":
            good_findings.append(DiagnosticFinding(
                finding_type="good_query_cluster",
                severity="info",
                confidence="medium",
                title=f"Релевантный кластер запросов: {agg['label']}",
                detail=(
                    f"Кластер «{agg['label']}» дал {agg['clicks']} кликов и {round(agg['cost'], 2)} ₽ "
                    f"расхода. Запросы соответствуют продукту -- возможная зона для расширения."
                ),
                recommended_action=None,
                payload={"cluster_key": cluster_key, "clicks": agg["clicks"], "cost": round(agg["cost"], 2),
                         "top_queries": top_queries},
            ))

    return irrelevant_findings, good_findings, irrelevant_ad_group_ids


def _pick_main_finding(findings: list) -> Optional[DiagnosticFinding]:
    """
    Выбирает главную находку для короткого Telegram-сообщения: сначала по
    severity (P1 > P2 > info), при равенстве -- по confidence (high > medium > low).
    Не использует ту же численную weighted-формулу, что analyzer.py
    (pick_primary_candidate), потому что здесь объекты другого типа
    (DiagnosticFinding, не AlertCandidate) и шкала проще -- 3 уровня
    severity вместо P0..P3, что не оправдывает veca формулу.
    """
    if not findings:
        return None

    severity_rank = {"P1": 0, "P2": 1, "info": 2}
    confidence_rank = {"high": 0, "medium": 1, "low": 2}

    return sorted(findings, key=lambda f: (severity_rank.get(f.severity, 99), confidence_rank.get(f.confidence, 99)))[0]


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------


def run_diagnostics(
    period_key: str,
    ad_group_rows: list,
    query_rows: list,
    attribution_status: AttributionStatus,
    query_clusters: Optional[dict] = None,
) -> DiagnosticsResult:
    """
    Главная точка входа. ad_group_rows и query_rows -- результат
    connectors/direct.fetch_ad_group_report()["rows"] и
    fetch_search_query_report()["rows"] соответственно.

    Если суммарных кликов меньше MIN_CLICKS_FOR_DEEP_DIAGNOSTICS --
    insufficient_data=True, findings будет пустым (агент честно говорит
    "данных мало", не выдумывает находки на шуме).
    """
    query_clusters = query_clusters or DEFAULT_QUERY_CLUSTERS

    total_clicks = sum(row["clicks"] for row in ad_group_rows)
    total_cost = sum(row["cost"] for row in ad_group_rows)

    if total_clicks < MIN_CLICKS_FOR_DEEP_DIAGNOSTICS:
        return DiagnosticsResult(
            period_key=period_key,
            attribution_status=attribution_status,
            total_clicks=total_clicks,
            total_cost=round(total_cost, 2),
            insufficient_data=True,
            findings=[],
            main_finding=None,
            good_findings=[],
        )

    # Порядок важен: сначала вычисляем находки, которые могут служить
    # "доказательством" для ad_group_budget_drain (нерелевантные запросы,
    # низкий CTR), потом budget_drain использует их, чтобы решить, поднимать
    # ли severity с info до P2 -- см. docstring _rule_ad_group_budget_drain.
    irrelevant_findings, good_findings, irrelevant_ad_group_ids = _rule_query_clusters(query_rows, query_clusters)
    low_ctr_findings, low_ctr_ad_group_ids = _rule_low_ctr_segment(ad_group_rows)
    budget_drain_findings = _rule_ad_group_budget_drain(
        ad_group_rows, total_clicks, total_cost, irrelevant_ad_group_ids, low_ctr_ad_group_ids,
    )

    findings = []
    findings.extend(budget_drain_findings)
    findings.extend(low_ctr_findings)
    findings.extend(irrelevant_findings)

    main_finding = _pick_main_finding(findings)

    return DiagnosticsResult(
        period_key=period_key,
        attribution_status=attribution_status,
        total_clicks=total_clicks,
        total_cost=round(total_cost, 2),
        insufficient_data=False,
        findings=findings,
        main_finding=main_finding,
        good_findings=good_findings,
    )


# ---------------------------------------------------------------------------
# Product Onboarding Diagnostics
# ---------------------------------------------------------------------------
#
# Симметрично Direct Deep Diagnostics выше: read-only анализ, осторожные
# формулировки, явная пометка при недостатке данных. Отличие -- источник
# данных -- connectors/onboarding.py (TruePost), не Director, и сам
# endpoint может ОТСУТСТВОВАТЬ (ожидаемо на данный момент) -- это
# обрабатывается отдельным статусом OnboardingDiagnosticsResult.status,
# не через insufficient_data (это разные ситуации: "данных мало" vs
# "источника данных вообще нет").


@dataclass
class OnboardingDiagnosticsResult:
    """
    status:
      "ok"            -- endpoint ответил, есть данные для анализа;
      "not_available" -- endpoint не реализован в TruePost (404) или
                          product connector не настроен -- ОЖИДАЕМАЯ
                          ситуация на данный момент, не ошибка;
      "error"         -- endpoint существует, но ответил ошибкой
                          (timeout, 5xx, invalid JSON).
    """

    status: str
    registrations: int = 0
    last_known_step: Optional[str] = None
    dropoff_summary: Optional[str] = None
    probable_causes: list = field(default_factory=list)
    recommended_actions: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    error_detail: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "registrations": self.registrations,
            "last_known_step": self.last_known_step,
            "dropoff_summary": self.dropoff_summary,
            "probable_causes": self.probable_causes,
            "recommended_actions": self.recommended_actions,
            "notes": self.notes,
            "error_detail": self.error_detail,
        }


# Стандартные шаги воронки онбординга в ожидаемом порядке (см. контракт
# endpoint в connectors/onboarding.py). Порядок важен для определения
# "последнего известного шага" -- это первый шаг по порядку, на котором
# users == 0, при условии что предыдущий шаг был > 0.
_ONBOARDING_STEP_ORDER = [
    ("registered", "регистрация"),
    ("onboarding_started", "начало онбординга"),
    ("channel_created", "создание канала"),
    ("post_generated", "генерация первого поста"),
]

# Типовые вероятные причины и действия для каждой пары "последний живой
# шаг -> где застряли". Это не привязано к конкретному продукту жёстко
# (формулировки общие для любого SaaS-онбординга), в отличие от
# query_clusters, которые специфичны для АвтоПоста -- поэтому здесь нет
# per-project override, в отличие от DEFAULT_QUERY_CLUSTERS.
_DROPOFF_CAUSES = {
    "registered": [
        "После регистрации неочевидно, что делать дальше.",
        "Нет явного призыва к действию на следующий шаг.",
        "Пользователь не попал на нужный экран после регистрации (проблема с redirect).",
        "Событие onboarding_started пока не трекается -- возможно, пользователь продвинулся дальше, но это не видно в данных.",
    ],
    "onboarding_started": [
        "Пользователь начал онбординг, но не дошёл до создания канала.",
        "Шаг создания канала может быть неочевиден или требовать действий вне продукта (добавление бота в Telegram-канал).",
    ],
    "channel_created": [
        "Канал создан, но первый пост не сгенерирован -- возможна проблема на шаге генерации контента.",
    ],
}

_DROPOFF_ACTIONS = {
    "registered": [
        "Проверить redirect сразу после регистрации.",
        "Проверить наличие явного CTA на следующий шаг.",
        "Добавить/проверить tracking событий onboarding_started и create_channel_clicked.",
    ],
    "onboarding_started": [
        "Проверить, понятен ли пользователю шаг добавления бота в канал.",
        "Проверить, не теряется ли пользователь между онбордингом и созданием канала.",
    ],
    "channel_created": [
        "Проверить, что происходит сразу после создания канала -- доходит ли пользователь до генерации первого поста.",
    ],
}


def analyze_onboarding(connector_result: dict) -> OnboardingDiagnosticsResult:
    """
    Принимает результат connectors/onboarding.fetch_onboarding_diagnostics()
    (уже успешный, status="ok" подразумевается -- ошибки/недоступность
    обрабатываются ДО вызова этой функции, на уровне scheduler.py, как и
    integration_down для остальных коннекторов). Определяет последний
    живой шаг воронки и формулирует осторожные находки.
    """
    last_known_step_summary = connector_result.get("last_known_step_summary") or {}
    registrations = connector_result.get("registrations", 0)
    notes = list(connector_result.get("notes", []))

    if registrations == 0:
        # Не должно обычно вызываться в этой ситуации (триггер -- alert
        # "регистрации без активации", то есть registrations > 0), но
        # защитная ветка на случай рассинхрона данных между основным
        # циклом и onboarding-запросом (разные моменты опроса).
        return OnboardingDiagnosticsResult(
            status="ok",
            registrations=0,
            last_known_step=None,
            dropoff_summary="За проверенный период регистраций не было -- не на чём строить диагностику онбординга.",
            notes=notes,
        )

    # Находим последний шаг, до которого реально дошла БОЛЬШАЯ ЧАСТЬ
    # зарегистрировавшихся -- не просто "значение > 0" (иначе 1 успешный
    # пользователь из 5 ошибочно выглядел бы как "все дошли до конца").
    # Алгоритм: идём по шагам по порядку, сравнивая каждый со значением
    # предыдущего шага. Последний шаг, где значение >= половины предыдущего,
    # считается "пройденным большинством"; первый шаг с резким падением
    # (< половины предыдущего) или с untracked (None) считается точкой обрыва.
    last_known_step_key = "registered"
    untracked_steps = []
    previous_value = registrations

    for step_key, _ in _ONBOARDING_STEP_ORDER[1:]:  # registered точно есть, раз registrations > 0
        value = last_known_step_summary.get(step_key)
        if value is None:
            untracked_steps.append(step_key)
            continue
        if value == 0:
            break  # трекается, но дошло 0 -- явный обрыв на этом шаге
        if value < previous_value:
            # Дошла не вся группа, а только часть -- этот шаг становится
            # последним "подтверждённым", но дальше не продолжаем считать
            # следующие шаги пройденными большинством, даже если там
            # формально value > 0 (это уже подмножество, не основной поток).
            last_known_step_key = step_key
            previous_value = value
            break
        last_known_step_key = step_key
        previous_value = value

    causes = list(_DROPOFF_CAUSES.get(last_known_step_key, []))
    actions = list(_DROPOFF_ACTIONS.get(last_known_step_key, []))

    if untracked_steps:
        untracked_labels = ", ".join(untracked_steps)
        notes.append(f"События не трекаются пока: {untracked_labels}.")

    step_label = dict(_ONBOARDING_STEP_ORDER).get(last_known_step_key, last_known_step_key)

    if last_known_step_key == "registered":
        dropoff_summary = (
            f"{registrations} пользователь(ей) зарегистрировались. "
            f"Дальше регистрации никто не продвинулся (по доступным данным)."
        )
    else:
        reached_count = previous_value
        dropoff_summary = (
            f"{registrations} пользователь(ей) зарегистрировались, из них {reached_count} "
            f"дошли до шага «{step_label}»."
        )

    return OnboardingDiagnosticsResult(
        status="ok",
        registrations=registrations,
        last_known_step=last_known_step_key,
        dropoff_summary=dropoff_summary,
        probable_causes=causes,
        recommended_actions=actions,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Landing Funnel Diagnostics
# ---------------------------------------------------------------------------
#
# Третий тип read-only диагностики, симметричный Direct Deep Diagnostics и
# Product Onboarding Diagnostics выше. Разница: воронка тут полностью
# последовательная (Direct clicks -> landing_views -> CTA -> bot_starts ->
# register_success -> activation_1), поэтому правила A-F проверяются по
# порядку, и при первом найденном разрыве остальные шаги дальше по цепочке
# не диагностируются отдельно -- если человек не открыл лендинг, бессмысленно
# отдельно говорить "и до регистрации не дошёл" как самостоятельную проблему,
# это следствие первой найденной причины, не вторая независимая проблема.
#
# Ключевое правило из задачи: агент НЕ должен предлагать менять лендинг/
# рекламу, если проблема локализована ПОСЛЕ клика по CTA (правила C, D, E) --
# это технические/продуктовые проблемы (Mini App, registration flow,
# onboarding), не вопрос текста лендинга или качества трафика.


@dataclass
class LandingFunnelStepResult:
    """Один найденный разрыв в воронке лендинга."""

    rule_id: str  # "a_clicks_no_views" | "b_views_no_cta" | "c_cta_no_bot_start" | "d_bot_start_no_register" | "e_register_no_activation"
    step_label: str
    severity: str  # "P1" | "P2" | "info"
    detail: str
    probable_cause: str
    recommended_action: str
    metric_to_recheck: str
    affects_landing_or_ads: bool  # False для правил C/D/E -- проблема ПОСЛЕ клика, не повод трогать лендинг/рекламу


@dataclass
class LandingFunnelDiagnosticsResult:
    """
    status: "ok" (есть данные, см. main_finding) | "insufficient_data"
    (landing_views < MIN_LANDING_VIEWS_FOR_FUNNEL_DIAGNOSTICS) | "error".
    """

    status: str
    main_finding: Optional[LandingFunnelStepResult] = None
    instrumentation_warnings: list = field(default_factory=list)  # правило F, отдельно от воронки
    no_critical_issue: bool = False  # явный сигнал "критической проблемы нет" (acceptance criteria #5)
    funnel_snapshot: dict = field(default_factory=dict)  # сырые числа воронки для отображения
    error_detail: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "main_finding": vars(self.main_finding) if self.main_finding else None,
            "instrumentation_warnings": self.instrumentation_warnings,
            "no_critical_issue": self.no_critical_issue,
            "funnel_snapshot": self.funnel_snapshot,
            "error_detail": self.error_detail,
        }


def _check_raw_vs_unique_warnings(landing_result: dict) -> list:
    """
    Правило F: если raw сильно выше unique -- instrumentation warning, не
    бизнес-проблема. Возвращает список текстовых предупреждений (может
    быть пустым). Используется отдельно от основной цепочки A-E, потому
    что это не разрыв воронки, а сигнал "тречкингу есть дубли, относитесь
    к unique-числам с долей осторожности".
    """
    from app.config import RAW_VS_UNIQUE_WARNING_MULTIPLIER

    warnings = []
    raw_values = landing_result.get("_raw_values", {})

    field_labels = {
        "landing_views": "просмотры лендинга",
        "cta_hero_bot_clicks": "клики по CTA (бот)",
        "cta_hero_app_clicks": "клики по CTA (приложение)",
        "bot_starts_from_landing": "запуски бота с лендинга",
        "web_register_opened": "открытие формы регистрации",
        "register_success": "успешные регистрации",
        "activation_1": "активация (шаг 1)",
    }

    for field, label in field_labels.items():
        unique_value = landing_result.get(field)
        raw_value = raw_values.get(field)
        if unique_value is None or raw_value is None or unique_value == 0:
            continue
        if raw_value > unique_value * RAW_VS_UNIQUE_WARNING_MULTIPLIER:
            warnings.append(
                f"«{label}»: raw-счётчик ({raw_value}) заметно выше unique ({unique_value}) -- "
                f"возможны дубли событий в трекинге. Для анализа используются unique-числа."
            )

    return warnings


def analyze_landing_funnel(
    landing_result: dict,
    direct_clicks: Optional[int] = None,
) -> LandingFunnelDiagnosticsResult:
    """
    landing_result -- результат connectors/landing.fetch_landing_funnel_diagnostics()
    (уже успешный; ошибки/недоступность обрабатываются ДО вызова этой
    функции, на уровне scheduler.py, как и для остальных диагностик).

    direct_clicks -- клики из Director за ТОТ ЖЕ период (NormalizedMetrics.clicks),
    передаются отдельно, не достаются из landing_result, потому что они
    приходят из другого источника (Direct connector, не TruePost) -- этой
    функции явно даются оба входа, не угадывается связь между коннекторами
    внутри неё.
    """
    from app.config import (
        LANDING_VIEWS_VS_CLICKS_MIN_RATIO,
        CTA_CLICKS_VS_VIEWS_MIN_RATIO,
        BOT_STARTS_VS_CTA_MIN_RATIO,
        REGISTER_VS_BOT_STARTS_MIN_RATIO,
        MIN_LANDING_VIEWS_FOR_FUNNEL_DIAGNOSTICS,
    )

    landing_views = landing_result.get("landing_views")
    cta_bot = landing_result.get("cta_hero_bot_clicks")
    cta_app = landing_result.get("cta_hero_app_clicks")
    cta_total = (cta_bot or 0) + (cta_app or 0)
    bot_starts = landing_result.get("bot_starts_from_landing")
    register_success = landing_result.get("register_success")
    activation_1 = landing_result.get("activation_1")

    instrumentation_warnings = _check_raw_vs_unique_warnings(landing_result)

    funnel_snapshot = {
        "direct_clicks": direct_clicks,
        "landing_views": landing_views,
        "cta_clicks": cta_total,
        "bot_starts_from_landing": bot_starts,
        "register_success": register_success,
        "activation_1": activation_1,
    }

    # Правило A: Direct clicks есть, landing_views сильно меньше -- проблема
    # ДО лендинга (переход из рекламы / загрузка / tracking). Проверяется
    # первой, до проверки MIN_LANDING_VIEWS -- даже 0 просмотров при наличии
    # кликов это сильный сигнал сам по себе, не "недостаточно данных".
    if direct_clicks is not None and direct_clicks > 0 and landing_views is not None:
        ratio = landing_views / direct_clicks if direct_clicks > 0 else 0
        if ratio < LANDING_VIEWS_VS_CLICKS_MIN_RATIO:
            finding = LandingFunnelStepResult(
                rule_id="a_clicks_no_views",
                step_label="Переход с рекламы на лендинг",
                severity="P1" if landing_views == 0 else "P2",
                detail=(
                    f"За период {direct_clicks} кликов из Директа, но только {landing_views} "
                    f"просмотров лендинга ({round(ratio * 100)}% от кликов)."
                ),
                probable_cause="Проблема в переходе из рекламы, загрузке лендинга, либо в трекинге просмотров.",
                recommended_action="Проверить, открывается ли лендинг по рекламной ссылке, и установлен ли счётчик корректно.",
                metric_to_recheck="landing_views относительно Direct clicks",
                affects_landing_or_ads=True,
            )
            return LandingFunnelDiagnosticsResult(
                status="ok", main_finding=finding,
                instrumentation_warnings=instrumentation_warnings, funnel_snapshot=funnel_snapshot,
            )

    if landing_views is None or landing_views < MIN_LANDING_VIEWS_FOR_FUNNEL_DIAGNOSTICS:
        return LandingFunnelDiagnosticsResult(
            status="insufficient_data",
            instrumentation_warnings=instrumentation_warnings,
            funnel_snapshot=funnel_snapshot,
        )

    # Правило B: landing_views есть, но мало CTA-кликов -- проблема в первом
    # экране/оффере/CTA. Это и есть зона ответственности самого лендинга.
    cta_ratio = cta_total / landing_views if landing_views > 0 else 0
    if cta_ratio < CTA_CLICKS_VS_VIEWS_MIN_RATIO:
        finding = LandingFunnelStepResult(
            rule_id="b_views_no_cta",
            step_label="Клик по CTA на лендинге",
            severity="P1" if cta_total == 0 else "P2",
            detail=(
                f"{landing_views} просмотров лендинга, но только {cta_total} кликов по CTA "
                f"({round(cta_ratio * 100, 1)}%)."
            ),
            probable_cause="Проблема в первом экране, оффере или заметности CTA.",
            recommended_action="Проверить, виден ли главный оффер и кнопка действия в первые секунды на мобильном.",
            metric_to_recheck="cta_hero_bot_clicks + cta_hero_app_clicks относительно landing_views",
            affects_landing_or_ads=True,
        )
        return LandingFunnelDiagnosticsResult(
            status="ok", main_finding=finding,
            instrumentation_warnings=instrumentation_warnings, funnel_snapshot=funnel_snapshot,
        )

    # Правило C: CTA кликают, но bot_starts_from_landing сильно меньше --
    # проблема В TELEGRAM-OPEN PATH (interstitial, iOS prompt, startapp,
    # Mini App), НЕ в самом лендинге. affects_landing_or_ads=False.
    if cta_total > 0 and bot_starts is not None:
        bot_start_ratio = bot_starts / cta_total if cta_total > 0 else 0
        if bot_start_ratio < BOT_STARTS_VS_CTA_MIN_RATIO:
            finding = LandingFunnelStepResult(
                rule_id="c_cta_no_bot_start",
                step_label="Открытие Telegram-бота после клика",
                severity="P1" if bot_starts == 0 else "P2",
                detail=(
                    f"{cta_total} кликов по CTA, но только {bot_starts} запусков бота "
                    f"({round(bot_start_ratio * 100)}%)."
                ),
                probable_cause=(
                    "Проблема в пути открытия Telegram: промежуточный экран t.me, "
                    "запрос подтверждения на iOS, параметр startapp, или загрузка Mini App."
                ),
                recommended_action="Проверить сам путь открытия бота из CTA на мобильном устройстве, включая iOS.",
                metric_to_recheck="bot_starts_from_landing относительно cta_hero_bot_clicks",
                affects_landing_or_ads=False,
            )
            return LandingFunnelDiagnosticsResult(
                status="ok", main_finding=finding,
                instrumentation_warnings=instrumentation_warnings, funnel_snapshot=funnel_snapshot,
            )

    # Правило D: бот открыт, но мало register_success -- проблема в Mini App
    # onboarding / registration flow, не в лендинге и не в рекламе.
    if bot_starts is not None and bot_starts > 0 and register_success is not None:
        register_ratio = register_success / bot_starts if bot_starts > 0 else 0
        if register_ratio < REGISTER_VS_BOT_STARTS_MIN_RATIO:
            finding = LandingFunnelStepResult(
                rule_id="d_bot_start_no_register",
                step_label="Регистрация после открытия бота",
                severity="P1" if register_success == 0 else "P2",
                detail=(
                    f"{bot_starts} запусков бота, но только {register_success} успешных регистраций "
                    f"({round(register_ratio * 100)}%)."
                ),
                probable_cause="Проблема в процессе регистрации внутри Mini App или в самом registration flow.",
                recommended_action="Пройти регистрацию самостоятельно от старта бота до конца, проверить на ошибки.",
                metric_to_recheck="register_success относительно bot_starts_from_landing",
                affects_landing_or_ads=False,
            )
            return LandingFunnelDiagnosticsResult(
                status="ok", main_finding=finding,
                instrumentation_warnings=instrumentation_warnings, funnel_snapshot=funnel_snapshot,
            )

    # Правило E: регистрация есть, активации нет -- проблема ПОСЛЕ
    # регистрации (подключение канала, первый пост, publishing bot).
    # Это та же зона, что Product Onboarding Diagnostics выше -- здесь
    # фиксируется как находка landing funnel, чтобы не заставлять
    # пользователя коррелировать два разных отчёта вручную.
    if register_success is not None and register_success > 0 and activation_1 is not None and activation_1 == 0:
        finding = LandingFunnelStepResult(
            rule_id="e_register_no_activation",
            step_label="Активация после регистрации",
            severity="P2",
            detail=f"{register_success} успешных регистраций, но 0 прошли первый шаг активации.",
            probable_cause="Проблема после регистрации: подключение канала, создание первого поста, добавление publishing-бота.",
            recommended_action="Проверить путь от регистрации до первого поста -- см. также /check_onboarding для детальной диагностики.",
            metric_to_recheck="activation_1 относительно register_success",
            affects_landing_or_ads=False,
        )
        return LandingFunnelDiagnosticsResult(
            status="ok", main_finding=finding,
            instrumentation_warnings=instrumentation_warnings, funnel_snapshot=funnel_snapshot,
        )

    # Ни одно правило не сработало -- воронка технически в порядке.
    # Явный позитивный сигнал (acceptance criteria #5), не пустота.
    return LandingFunnelDiagnosticsResult(
        status="ok", main_finding=None, no_critical_issue=True,
        instrumentation_warnings=instrumentation_warnings, funnel_snapshot=funnel_snapshot,
    )
