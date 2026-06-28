from app.config import TELEGRAM_MESSAGE_CHUNK_SIZE
from app.telegram_bot import _split_telegram_text


def test_deep_direct_long_message_is_split_under_telegram_limit():
    text = ("строка\n" * 1200).strip()

    chunks = _split_telegram_text(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= TELEGRAM_MESSAGE_CHUNK_SIZE for chunk in chunks)
    assert "".join(chunk.replace("\n", "") for chunk in chunks) == text.replace("\n", "")


def test_deep_direct_short_message_is_not_split():
    text = "короткая диагностика"

    assert _split_telegram_text(text) == [text]

from app.telegram_bot import format_deep_diagnostics_details


def test_deep_direct_summary_fallback_is_useful_without_granular_cache():
    text = format_deep_diagnostics_details(
        {
            "_fallback_kind": "direct_summary_snapshot",
            "_from_cache": True,
            "_cache_created_at": "2026-06-24 21:39 UTC",
            "period_key": "7d",
            "direct": {"spend": 3157, "clicks": 594, "impressions": 5600, "ctr": 10.6, "cpc": 5.31},
            "product": {"signup": 25},
        },
        "АвтоПост",
    )

    assert "Legacy granular" in text or "Гранулярный" in text or "не готов" in text
    assert "594" in text
    assert "3157" in text
    assert "25" in text
    assert "ориентировочный CPA: 126" in text
    assert "нельзя чистить вслепую" in text


def test_deep_direct_partial_result_marks_missing_source():
    text = format_deep_diagnostics_details(
        {
            "period_key": "7d",
            "attribution_status": "not_available",
            "total_clicks": 100,
            "total_cost": 500,
            "insufficient_data": False,
            "main_finding": None,
            "known_risks": [],
            "_partial": True,
            "_partial_errors": {"ad_group": "отчёт по группам не готов"},
        },
        "АвтоПост",
    )

    assert "Часть данных пришла не полностью" in text
    assert "отчёт по группам не готов" in text
    assert "частичную диагностику" in text
