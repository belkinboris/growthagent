from datetime import datetime, timedelta, timezone

from app.owner_report import build_owner_report, format_snapshot_age, safe_phrase_negative_candidates
from app.rules import NormalizedMetrics


def _current_autopost_metrics() -> NormalizedMetrics:
    return NormalizedMetrics(
        period_key="7d",
        signup=25,
        activation_1=20,
        activation_2=56,
        payment_started=1,
        payment_success=0,
        spend=3157,
        clicks=594,
        impressions=5600,
        ctr=10.6,
        sources_ok={"product", "direct", "metrika"},
    )


def test_owner_report_current_autopost_stage_and_action():
    report = build_owner_report(
        "АвтоПост",
        _current_autopost_metrics(),
        source_statuses={
            "product": {"status": "fresh"},
            "direct": {"status": "fresh"},
            "metrika": {"status": "fresh"},
            "yookassa": {"status": "unavailable"},
        },
        previous_metrics=None,
        deep_diagnostics=None,
    )

    assert report is not None
    assert "Регистрации и активации пошли" in report
    assert "Привлечение начало работать" in report
    assert "25 регистраций" in report
    assert "20 созданных каналов" in report
    assert "56 генераций постов" in report
    assert "платёжный шаг" in report
    assert "данных пока мало" in report or "не P1" in report
    assert "не менять резко рекламу" in report
    assert "не менять резко лендинг" in report
    assert "не менять ставки, бюджет, цены и тарифы" in report
    assert "не менять цены" in report or "цены и тарифы" in report


def test_owner_report_payment_started_one_is_not_p1():
    report = build_owner_report("АвтоПост", _current_autopost_metrics())
    assert "не P1" in report
    # Слово "payment" допустимо в action items; запрещаем только ложный P1 вывод
    assert "P1" not in report or "не P1" in report


def test_safe_phrase_negative_candidates_block_broad_single_words():
    candidates = safe_phrase_negative_candidates([
        "генерация",
        "текст",
        "поста",
        "онлайн",
        "генерация текста онлайн",
        "генерация постов для телеграм",
    ])
    assert "генерация" not in candidates
    assert "текст" not in candidates
    assert "поста" not in candidates
    assert "онлайн" not in candidates
    assert "генерация текста онлайн" in candidates
    assert "генерация постов для телеграм" not in candidates


def test_owner_report_query_layer_does_not_overreact_to_one_off_low_spend():
    deep_diagnostics = {
        "insufficient_data": False,
        "findings": [
            {
                "finding_type": "irrelevant_query_cluster",
                "severity": "info",
                "confidence": "medium",
                "title": "Нерелевантный кластер запросов: Общая генерация текста",
                "payload": {
                    "clicks": 3,
                    "cost": 94.8,
                    "cost_share": 0.03,
                    "top_queries": ["22 июня 1941 сгенерировать текст для поста"],
                },
            }
        ],
        "good_findings": [],
    }
    report = build_owner_report("АвтоПост", _current_autopost_metrics(), deep_diagnostics=deep_diagnostics)
    assert "срочной чистки по кэшу нет" in report
    assert "бизнес-вес низкий" in report
    assert "не делать главным выводом" in report


def test_status_snapshot_age_formatting_does_not_use_period_as_age():
    now = datetime(2026, 6, 24, 21, 39, tzinfo=timezone.utc)
    snap = now - timedelta(minutes=5)
    text = format_snapshot_age(snap, now)
    assert "5 мин назад" in text
    assert "7d" not in text
