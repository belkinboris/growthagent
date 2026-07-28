"""
Когорты по источнику (задача D3).

Суммарная воронка отвечает «сколько людей теряется», но не отвечает
«кого мы приводим». Канал, который даёт много регистраций и ноль оплат,
в общей сумме выглядит пользой: он поднимает верх воронки и топит
конверсию, а виноватым кажется продукт.

Главное, что здесь проверяется, — честность: доля оплат на пяти пришедших
ничего не значит, и сравнивать источники по таким числам нельзя. И второе:
аналитик не советует отключать канал, решение про рекламу за человеком.
"""

import pytest

from app.service import PAYMENT_PATH_CACHE_PERIOD_KEY, save_diagnostics_cache
from tests.test_platform_api import _client, _login

BREAKDOWN = {
    "yandex_direct": {"registrations": 40, "channels_created": 30,
                      "payment_started": 6, "payment_success": 4},
    "telegram_ads": {"registrations": 20, "channels_created": 5,
                     "payment_started": 1, "payment_success": 1},
}


def _cache(session_factory, breakdown=BREAKDOWN, ok=True):
    with session_factory() as session:
        payload = {"registrations": 60}
        if breakdown is not None:
            payload["source_breakdown"] = breakdown
        save_diagnostics_cache(session, 1, PAYMENT_PATH_CACHE_PERIOD_KEY,
                               "test", payload, ok=ok)


def _body(client):
    return client.get("/growth/api/sources").json()


def _by_label(client):
    return {s["label"]: s for s in _body(client)["sources"]}


class TestCohorts:
    def test_each_source_is_a_row(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory)

        rows = _by_label(client)
        assert set(rows) == {"Яндекс.Директ", "Telegram Ads"}
        assert rows["Яндекс.Директ"]["signup"] == 40
        assert rows["Яндекс.Директ"]["payment_success"] == 4
        assert rows["Яндекс.Директ"]["conversion"] == 10.0

    def test_aliases_of_one_source_are_one_cohort(self, monkeypatch, tmp_path):
        """`yandex_direct` и `direct` -- один канал; двумя строками он
        выглядит вдвое слабее, чем есть."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory, {"yandex_direct": {"registrations": 25, "payment_success": 2},
                         "direct": {"registrations": 15, "payment_success": 2}})

        rows = _by_label(client)
        assert list(rows) == ["Яндекс.Директ"]
        assert rows["Яндекс.Директ"]["signup"] == 40

    def test_biggest_source_goes_first(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory)
        assert _body(client)["sources"][0]["label"] == "Яндекс.Директ"

    def test_step_names_follow_project_names(self, monkeypatch, tmp_path):
        """Экран источников зовёт шаги так же, как остальные экраны."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory)
        client.put("/growth/api/projects/1/stages",
                   json={"titles": {"activation_1": "Создал канал"}})

        assert _body(client)["titles"]["activation_1"] == "Создал канал"

    def test_difference_is_named(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory)

        summary = _body(client)["summary"]
        assert "Яндекс.Директ" in summary and "Telegram Ads" in summary
        assert "10%" in summary


class TestHonestyAboutSmallNumbers:
    def test_small_cohort_has_no_percent(self, monkeypatch, tmp_path):
        """1 из 4 -- это «25%», и показывать так нельзя."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory, {**BREAKDOWN, "telegram_ads": {"registrations": 4, "payment_success": 1}})

        row = _by_label(client)["Telegram Ads"]
        assert row["conversion"] is None
        assert row["reliable"] is False
        assert "случайна" in row["note"]

    def test_single_reliable_source_is_not_compared(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory, {**BREAKDOWN, "telegram_ads": {"registrations": 4, "payment_success": 1}})

        assert "только из одного" in _body(client)["summary"]

    def test_no_reliable_source_says_so(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory, {"yandex_direct": {"registrations": 3, "payment_success": 1},
                         "telegram_ads": {"registrations": 2}})

        assert "ещё рано" in _body(client)["summary"]

    def test_nobody_paid_is_not_a_comparison(self, monkeypatch, tmp_path):
        """Ноль оплат везде -- это не «все источники одинаково хороши»."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory, {"yandex_direct": {"registrations": 40, "payment_success": 0},
                         "telegram_ads": {"registrations": 20, "payment_success": 0}})

        summary = _body(client)["summary"]
        assert "не дошёл никто" in summary
        assert "Лучше всех" not in summary

    def test_equal_conversion_is_named_as_no_difference(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory, {"yandex_direct": {"registrations": 40, "payment_success": 4},
                         "telegram_ads": {"registrations": 20, "payment_success": 2}})

        assert "Разницы между источниками не видно" in _body(client)["summary"]

    def test_analyst_does_not_decide_about_ads(self, monkeypatch, tmp_path):
        """Сознательное ограничение продукта: аналитик рекламу не трогает
        и не советует «отключить канал» -- он показывает разницу."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory)

        summary = _body(client)["summary"]
        assert "решать вам" in summary
        for forbidden in ("отключ", "выключ", "остановите"):
            assert forbidden not in summary.lower()


class TestEmptyStates:
    def test_no_diagnostics_yet_is_explained(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)

        body = _body(client)
        assert body["ok"] is False and body["sources"] == []
        assert "первого полного цикла" in body["hint"]

    def test_missing_breakdown_says_what_to_do(self, monkeypatch, tmp_path):
        """Разбивку знает только продукт -- и он должен узнать, что от него
        требуется, а не увидеть пустоту без объяснения."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory, breakdown=None)

        body = _body(client)
        assert body["ok"] is False
        assert "source_breakdown" in body["hint"]
        assert "utm_source" in body["hint"]

    def test_failed_diagnostics_is_treated_as_no_data(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory, ok=False)
        assert _body(client)["ok"] is False

    def test_empty_technical_bucket_is_not_a_source(self, monkeypatch, tmp_path):
        """«other» с нулями -- корзина для несопоставленных событий,
        а не канал трафика."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory, {**BREAKDOWN, "other": {"registrations": 0, "payment_success": 0}})

        assert "other" not in _by_label(client)

    def test_requires_auth(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        assert client.get("/growth/api/sources").status_code == 401
