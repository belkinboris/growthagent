"""
Тесты для app/connectors/metrika.py на мок-ответах Reports API.

Сценарии: нормальный ответ, нули, 401, битый JSON, нет totals,
sampled=true, неизвестная goal id / API error.
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx

from app.connectors import metrika


GOAL_IDS = {
    "signup": 111111,
    "activation_1": 222222,
    "activation_2": 333333,
    "payment_started": 444444,
    "payment_success": 555555,
}


class FakeResponse:
    def __init__(self, status_code, json_data=None, json_raises=False):
        self.status_code = status_code
        self._json_data = json_data
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json_data


def _patch_client(fake_response):
    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def get(self, *a, **kw):
            return fake_response

    return patch("httpx.AsyncClient", return_value=FakeAsyncClient())


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. Нормальный ответ
# ---------------------------------------------------------------------------


def test_normal_response():
    fake_body = {
        "totals": [[150, 140, 10, 8, 6, 3, 1]],  # visits, users, 5 goals
        "data_lag": 60,
        "sampled": False,
        "total_rows": 1,
    }
    with _patch_client(FakeResponse(200, fake_body)):
        result = run(metrika.fetch_metrics(
            oauth_token="tok", counter_id="123", period_hours=24,
            goal_mapping={}, goal_ids=GOAL_IDS,
        ))

    assert result["traffic"] == 150
    assert result["signup"] == 10
    assert result["activation_1"] == 8
    assert result["activation_2"] == 6
    assert result["payment_started"] == 3
    assert result["payment_success"] == 1
    assert result["_diagnostics"]["sampled"] is False
    assert result["_diagnostics"]["data_lag"] == 60
    print("test_normal_response: OK")


# ---------------------------------------------------------------------------
# 2. Нули (нет данных за период, не ошибка)
# ---------------------------------------------------------------------------


def test_zero_data():
    fake_body = {"totals": [[0, 0, 0, 0, 0, 0, 0]], "sampled": False}
    with _patch_client(FakeResponse(200, fake_body)):
        result = run(metrika.fetch_metrics(
            oauth_token="tok", counter_id="123", period_hours=3,
            goal_mapping={}, goal_ids=GOAL_IDS,
        ))

    assert result["traffic"] == 0
    assert result["signup"] == 0
    assert result["payment_success"] == 0
    print("test_zero_data: OK")


def test_empty_totals():
    """totals вообще отсутствует или пустой массив -- тоже трактуется как нули, не ошибка."""
    fake_body = {"totals": [], "sampled": False}
    with _patch_client(FakeResponse(200, fake_body)):
        result = run(metrika.fetch_metrics(
            oauth_token="tok", counter_id="123", period_hours=3,
            goal_mapping={}, goal_ids=GOAL_IDS,
        ))
    assert result["traffic"] == 0
    assert all(result[k] == 0 for k in GOAL_IDS.keys())
    print("test_empty_totals: OK")


# ---------------------------------------------------------------------------
# 3. 401 Unauthorized
# ---------------------------------------------------------------------------


def test_401_unauthorized():
    fake_body = {"message": "Invalid oauth_token", "errors": [{"error_type": "oauth_token_invalid", "message": "Invalid oauth_token"}]}
    with _patch_client(FakeResponse(401, fake_body)):
        try:
            run(metrika.fetch_metrics(
                oauth_token="bad-token", counter_id="123", period_hours=24,
                goal_mapping={}, goal_ids=GOAL_IDS,
            ))
            assert False, "Should have raised MetrikaConnectorError"
        except metrika.MetrikaConnectorError as exc:
            assert "401" in str(exc)
            assert "oauth_token_invalid" in str(exc)
            print(f"test_401_unauthorized: OK ({exc})")


# ---------------------------------------------------------------------------
# 4. Битый JSON
# ---------------------------------------------------------------------------


def test_broken_json():
    with _patch_client(FakeResponse(200, json_raises=True)):
        try:
            run(metrika.fetch_metrics(
                oauth_token="tok", counter_id="123", period_hours=24,
                goal_mapping={}, goal_ids=GOAL_IDS,
            ))
            assert False, "Should have raised MetrikaConnectorError"
        except metrika.MetrikaConnectorError as exc:
            assert "not valid JSON" in str(exc)
            print(f"test_broken_json: OK ({exc})")


# ---------------------------------------------------------------------------
# 5. Нет totals в успешном (200) ответе -- покрыто test_empty_totals выше,
#    но добавим случай когда ключа totals нет вообще (не пустой массив, а None)
# ---------------------------------------------------------------------------


def test_missing_totals_key():
    fake_body = {"sampled": False}  # totals вообще отсутствует
    with _patch_client(FakeResponse(200, fake_body)):
        result = run(metrika.fetch_metrics(
            oauth_token="tok", counter_id="123", period_hours=24,
            goal_mapping={}, goal_ids=GOAL_IDS,
        ))
    assert result["traffic"] == 0
    print("test_missing_totals_key: OK")


# ---------------------------------------------------------------------------
# 6. sampled=true -- данные есть, но это оценка, не точное число
# ---------------------------------------------------------------------------


def test_sampled_response():
    fake_body = {
        "totals": [[10000, 9500, 500, 400, 300, 100, 50]],
        "sampled": True,
        "sample_size": 100000,
        "sample_space": 1000000,
    }
    with _patch_client(FakeResponse(200, fake_body)):
        result = run(metrika.fetch_metrics(
            oauth_token="tok", counter_id="123", period_hours=168,
            goal_mapping={}, goal_ids=GOAL_IDS,
        ))

    assert result["_diagnostics"]["sampled"] is True
    assert result["_diagnostics"]["sample_share"] == 0.1
    assert result["traffic"] == 10000  # данные всё равно возвращаются, не блокируются
    print("test_sampled_response: OK")


# ---------------------------------------------------------------------------
# 7. Неизвестная goal id / API error (400 с описанием невалидного параметра)
# ---------------------------------------------------------------------------


def test_invalid_goal_id_error():
    fake_body = {
        "message": "invalid parameter metrics",
        "errors": [{"error_type": "invalid_parameter", "message": "goal with id 999999999 not found"}],
    }
    with _patch_client(FakeResponse(400, fake_body)):
        try:
            run(metrika.fetch_metrics(
                oauth_token="tok", counter_id="123", period_hours=24,
                goal_mapping={}, goal_ids={"signup": 999999999},
            ))
            assert False, "Should have raised MetrikaConnectorError"
        except metrika.MetrikaConnectorError as exc:
            assert "400" in str(exc)
            assert "999999999" in str(exc) or "invalid_parameter" in str(exc)
            print(f"test_invalid_goal_id_error: OK ({exc})")


def test_counter_not_found():
    fake_body = {"message": "Counter not found", "errors": [{"error_type": "counter_not_found", "message": "Counter not found"}]}
    with _patch_client(FakeResponse(403, fake_body)):
        try:
            run(metrika.fetch_metrics(
                oauth_token="tok", counter_id="000000", period_hours=24,
                goal_mapping={}, goal_ids=GOAL_IDS,
            ))
            assert False, "Should have raised MetrikaConnectorError"
        except metrika.MetrikaConnectorError as exc:
            assert "403" in str(exc)
            print(f"test_counter_not_found: OK ({exc})")


# ---------------------------------------------------------------------------
# Конфигурация: отсутствие токена/counter_id/goal_ids -- NotConfiguredError
# ---------------------------------------------------------------------------


def test_not_configured_no_token():
    try:
        run(metrika.fetch_metrics(oauth_token=None, counter_id="123", period_hours=24, goal_mapping={}, goal_ids=GOAL_IDS))
        assert False
    except metrika.NotConfiguredError:
        print("test_not_configured_no_token: OK")


def test_not_configured_no_goal_ids():
    try:
        run(metrika.fetch_metrics(oauth_token="tok", counter_id="123", period_hours=24, goal_mapping={}, goal_ids={}))
        assert False
    except metrika.NotConfiguredError:
        print("test_not_configured_no_goal_ids: OK")


# ---------------------------------------------------------------------------
# Таймаут
# ---------------------------------------------------------------------------


def test_timeout():
    class TimeoutClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def get(self, *a, **kw):
            raise httpx.TimeoutException("Request timed out")

    with patch("httpx.AsyncClient", return_value=TimeoutClient()):
        try:
            run(metrika.fetch_metrics(oauth_token="tok", counter_id="123", period_hours=24, goal_mapping={}, goal_ids=GOAL_IDS))
            assert False
        except metrika.MetrikaConnectorError as exc:
            assert "Timeout" in str(exc)
            print(f"test_timeout: OK ({exc})")


# ---------------------------------------------------------------------------
# test_metrika_connection (debug-функция)
# ---------------------------------------------------------------------------


def test_debug_connection_ok():
    fake_body = {"totals": [[50, 45, 5, 4, 3, 1, 0]], "sampled": False, "data_lag": 30}
    with _patch_client(FakeResponse(200, fake_body)):
        result = run(metrika.test_metrika_connection(oauth_token="tok", counter_id="123", goal_ids=GOAL_IDS))
    assert result["ok"] is True
    assert result["traffic"] == 50
    assert result["goals_found"]["signup"] == 5
    print("test_debug_connection_ok: OK")


def test_debug_connection_not_configured():
    result = run(metrika.test_metrika_connection(oauth_token=None, counter_id=None, goal_ids=None))
    assert result["ok"] is False
    assert result["stage"] == "config"
    print("test_debug_connection_not_configured: OK")


def test_debug_connection_api_error():
    with _patch_client(FakeResponse(401, {"message": "bad token"})):
        result = run(metrika.test_metrika_connection(oauth_token="bad", counter_id="123", goal_ids=GOAL_IDS))
    assert result["ok"] is False
    assert result["stage"] == "api_call"
    print("test_debug_connection_api_error: OK")


if __name__ == "__main__":
    test_normal_response()
    test_zero_data()
    test_empty_totals()
    test_401_unauthorized()
    test_broken_json()
    test_missing_totals_key()
    test_sampled_response()
    test_invalid_goal_id_error()
    test_counter_not_found()
    test_not_configured_no_token()
    test_not_configured_no_goal_ids()
    test_timeout()
    test_debug_connection_ok()
    test_debug_connection_not_configured()
    test_debug_connection_api_error()
    print()
    print("Все тесты metrika connector пройдены.")
