"""
Отзывы о результате (экран Продакта).

Данные уже собирались и считались планировщиком, но были видны только
внутри длинных текстовых отчётов -- владелец не мог посмотреть отдельно,
не читая всё остальное. Проверяется: то же самое честно, отдельным JSON,
с тем же порогом малой выборки, что у сравнения недель.
"""

import pytest

from app.service import PAYMENT_PATH_CACHE_PERIOD_KEY, save_diagnostics_cache
from tests.test_platform_api import _client, _login


def _cache(session_factory, payload, ok=True):
    with session_factory() as session:
        save_diagnostics_cache(session, 1, PAYMENT_PATH_CACHE_PERIOD_KEY, "test", payload, ok=ok)


def _body(client):
    return client.get("/growth/api/feedback").json()


class TestFeedback:
    def test_counts_and_reasons(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory, {
            "first_post_feedback_good": 6, "first_post_feedback_bad": 4,
            "first_post_feedback_reasons": {"wrong_style": 3, "too_generic": 1, "other": 0},
        })

        body = _body(client)
        assert body["ok"] is True
        assert (body["good"], body["bad"], body["total"]) == (6, 4, 10)
        assert body["reliable"] is True
        assert body["bad_share_percent"] == 40
        assert body["reasons"][0] == {"key": "wrong_style", "label": "не тот стиль", "count": 3}
        # Нулевые причины не показываем -- они ничего не сообщают.
        assert all(r["key"] != "other" for r in body["reasons"])

    def test_reasons_sorted_by_count(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory, {
            "first_post_feedback_good": 1, "first_post_feedback_bad": 5,
            "first_post_feedback_reasons": {"too_dry": 1, "wrong_topic": 4},
        })
        assert [r["key"] for r in _body(client)["reasons"]] == ["wrong_topic", "too_dry"]

    def test_small_sample_has_no_percent(self, monkeypatch, tmp_path):
        """1 из 2 -- «50%», и показывать так нельзя."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory, {"first_post_feedback_good": 1, "first_post_feedback_bad": 1})

        body = _body(client)
        assert body["reliable"] is False
        assert body["bad_share_percent"] is None
        assert "мало" in body["hint"]

    def test_no_feedback_yet_is_a_normal_result(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory, {"registrations": 10})

        body = _body(client)
        assert body["ok"] is True
        assert body["total"] == 0
        assert "никто не оставил" in body["hint"]

    def test_no_diagnostics_yet_is_explained(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        body = _body(client)
        assert body["ok"] is False
        assert "первого полного цикла" in body["hint"]

    def test_failed_diagnostics_is_treated_as_no_data(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory, {"first_post_feedback_good": 5}, ok=False)
        assert _body(client)["ok"] is False

    def test_requires_auth(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        assert client.get("/growth/api/feedback").status_code == 401
