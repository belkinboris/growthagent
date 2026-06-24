"""
Тесты для app/connectors/direct.py на мок-ответах Reports Service.

Сценарии: нормальный TSV, пустой TSV, 401/403, malformed TSV,
report processing / retry, несколько кампаний, фильтр по campaign_ids,
cost/clicks/impressions aggregate.
"""

import asyncio
from unittest.mock import patch

import httpx

from app.connectors import direct


def run(coro):
    return asyncio.run(coro)


class FakeResponse:
    def __init__(self, status_code, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def _patch_post_sequence(responses):
    """responses -- список FakeResponse, возвращаются по очереди при последовательных вызовах post()."""
    call_index = {"i": 0}

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, *a, **kw):
            r = responses[min(call_index["i"], len(responses) - 1)]
            call_index["i"] += 1
            return r

    return patch("httpx.AsyncClient", return_value=FakeAsyncClient())


# ---------------------------------------------------------------------------
# 1. Нормальный TSV, несколько кампаний
# ---------------------------------------------------------------------------


def test_normal_tsv_multiple_campaigns():
    tsv = (
        "CampaignId\tCampaignName\tImpressions\tClicks\tCost\tCtr\tAvgCpc\n"
        "111\tКампания А\t1000\t50\t500000000\t5.0\t10000000\n"
        "222\tКампания Б\t2000\t80\t800000000\t4.0\t10000000\n"
    )
    with _patch_post_sequence([FakeResponse(200, tsv)]):
        result = run(direct.fetch_metrics(
            oauth_token="tok", client_login="login", campaign_ids=[], period_hours=24,
        ))

    # Cost в микро-единицах: (500000000 + 800000000) / 1_000_000 = 1300.0 руб
    assert result["spend"] == 1300.0
    assert result["clicks"] == 130
    assert result["impressions"] == 3000
    assert result["campaigns_count"] == 2
    # CTR пересчитан из агрегатов, не усреднён: 130/3000*100 = 4.33
    assert abs(result["ctr"] - 4.33) < 0.01
    print(f"test_normal_tsv_multiple_campaigns: OK (spend={result['spend']}, clicks={result['clicks']}, ctr={result['ctr']})")


# ---------------------------------------------------------------------------
# 2. Пустой TSV (нет данных за период, не ошибка)
# ---------------------------------------------------------------------------


def test_empty_tsv():
    with _patch_post_sequence([FakeResponse(200, "")]):
        result = run(direct.fetch_metrics(
            oauth_token="tok", client_login="login", campaign_ids=[], period_hours=3,
        ))
    assert result["spend"] == 0.0
    assert result["clicks"] == 0
    assert result["impressions"] == 0
    print("test_empty_tsv: OK")


def test_header_only_no_data_rows():
    """Заголовок есть, но строк с данными нет -- тоже нули, не ошибка."""
    tsv = "CampaignId\tCampaignName\tImpressions\tClicks\tCost\tCtr\tAvgCpc\n"
    with _patch_post_sequence([FakeResponse(200, tsv)]):
        result = run(direct.fetch_metrics(
            oauth_token="tok", client_login="login", campaign_ids=[], period_hours=3,
        ))
    assert result["spend"] == 0.0
    assert result["campaigns_count"] == 0
    print("test_header_only_no_data_rows: OK")


# ---------------------------------------------------------------------------
# 3. 401/403
# ---------------------------------------------------------------------------


def test_401_unauthorized():
    error_body = '{"error": {"error_code": 53, "error_string": "Authorization error", "error_detail": "Invalid OAuth token"}}'
    with _patch_post_sequence([FakeResponse(401, error_body)]):
        try:
            run(direct.fetch_metrics(oauth_token="bad", client_login="login", campaign_ids=[], period_hours=24))
            assert False, "Should have raised"
        except direct.DirectConnectorError as exc:
            assert "401" in str(exc)
            assert "Authorization error" in str(exc)
            print(f"test_401_unauthorized: OK ({exc})")


def test_403_no_client_login_access():
    error_body = '{"error": {"error_code": 152, "error_string": "Client access denied", "error_detail": "No access to client login"}}'
    with _patch_post_sequence([FakeResponse(403, error_body)]):
        try:
            run(direct.fetch_metrics(oauth_token="tok", client_login="not-mine", campaign_ids=[], period_hours=24))
            assert False, "Should have raised"
        except direct.DirectConnectorError as exc:
            assert "403" in str(exc)
            print(f"test_403_no_client_login_access: OK ({exc})")


# ---------------------------------------------------------------------------
# 4. Malformed TSV
# ---------------------------------------------------------------------------


def test_malformed_tsv_row_skipped():
    """Одна строка с неправильным числом колонок не должна ломать весь отчёт."""
    tsv = (
        "CampaignId\tCampaignName\tImpressions\tClicks\tCost\tCtr\tAvgCpc\n"
        "111\tКампания А\t1000\t50\t500000000\t5.0\t10000000\n"
        "222\tБитая строка без всех колонок\n"
        "333\tКампания В\t500\t20\t200000000\t4.0\t10000000\n"
    )
    with _patch_post_sequence([FakeResponse(200, tsv)]):
        result = run(direct.fetch_metrics(oauth_token="tok", client_login="login", campaign_ids=[], period_hours=24))

    # Битая строка пропущена, но валидные 2 кампании посчитаны
    assert result["campaigns_count"] == 2
    assert result["clicks"] == 70
    print(f"test_malformed_tsv_row_skipped: OK (campaigns_count={result['campaigns_count']}, clicks={result['clicks']})")


def test_completely_broken_tsv():
    """Полностью мусорный текст вместо TSV -- не должен крашить, просто не даст полезных данных."""
    with _patch_post_sequence([FakeResponse(200, "это не tsv вообще никак \x00\x01")]):
        result = run(direct.fetch_metrics(oauth_token="tok", client_login="login", campaign_ids=[], period_hours=24))
    # Первая строка станет header, дальше нет валидных data rows (нет строк с тем же кол-вом колонок)
    assert result["clicks"] == 0
    print("test_completely_broken_tsv: OK (не упал, вернул нули)")


# ---------------------------------------------------------------------------
# 5. Report processing / retry
# ---------------------------------------------------------------------------


def test_report_processing_then_success():
    """Первый запрос -- 202 (формируется), второй -- 200 (готово)."""
    tsv = "CampaignId\tCampaignName\tImpressions\tClicks\tCost\tCtr\tAvgCpc\n111\tA\t100\t10\t100000000\t10.0\t10000000\n"
    responses = [
        FakeResponse(202, "", headers={"retryIn": "0"}),  # retryIn=0 чтобы тест не ждал реально
        FakeResponse(200, tsv),
    ]
    with _patch_post_sequence(responses):
        result = run(direct.fetch_metrics(oauth_token="tok", client_login="login", campaign_ids=[], period_hours=24))

    assert result["clicks"] == 10
    assert result["_diagnostics"]["attempt_statuses"] == [202, 200]
    print(f"test_report_processing_then_success: OK (attempts={result['_diagnostics']['attempt_statuses']})")


def test_report_processing_exceeds_max_retries():
    """Отчёт никогда не готов -- после max_retries попыток должна быть понятная ошибка, не зависание."""
    responses = [FakeResponse(202, "", headers={"retryIn": "0"})] * 10
    with _patch_post_sequence(responses):
        try:
            run(direct.fetch_metrics(
                oauth_token="tok", client_login="login", campaign_ids=[], period_hours=24,
                max_retries=3,
            ))
            assert False, "Should have raised after max_retries"
        except direct.DirectConnectorError as exc:
            assert "still processing" in str(exc)
            assert "3" in str(exc) or "[202, 202, 202]" in str(exc)
            print(f"test_report_processing_exceeds_max_retries: OK ({exc})")


def test_201_also_triggers_retry():
    """201 (отчёт принят в очередь) обрабатывается так же, как 202."""
    tsv = "CampaignId\tCampaignName\tImpressions\tClicks\tCost\tCtr\tAvgCpc\n111\tA\t50\t5\t50000000\t10.0\t10000000\n"
    responses = [FakeResponse(201, "", headers={"RetryIn": "0"}), FakeResponse(200, tsv)]
    with _patch_post_sequence(responses):
        result = run(direct.fetch_metrics(oauth_token="tok", client_login="login", campaign_ids=[], period_hours=24))
    assert result["clicks"] == 5
    print("test_201_also_triggers_retry: OK")


# ---------------------------------------------------------------------------
# 6-7. Несколько кампаний + фильтр по campaign_ids
# ---------------------------------------------------------------------------


def test_campaign_ids_filter_included_in_request():
    """Проверяем, что переданные campaign_ids реально попадают в report definition (Filter)."""
    captured_payload = {}

    class CapturingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, headers=None, json=None):
            captured_payload["json"] = json
            tsv = "CampaignId\tCampaignName\tImpressions\tClicks\tCost\tCtr\tAvgCpc\n111\tA\t10\t1\t10000000\t10.0\t10000000\n"
            return FakeResponse(200, tsv)

    with patch("httpx.AsyncClient", return_value=CapturingClient()):
        run(direct.fetch_metrics(
            oauth_token="tok", client_login="login", campaign_ids=["111", "222"], period_hours=24,
        ))

    selection = captured_payload["json"]["params"]["SelectionCriteria"]
    assert "Filter" in selection
    assert selection["Filter"][0]["Field"] == "CampaignId"
    assert selection["Filter"][0]["Values"] == ["111", "222"]
    print("test_campaign_ids_filter_included_in_request: OK")


def test_no_campaign_ids_means_no_filter():
    """Пустой campaign_ids -- отчёт по всем кампаниям, без Filter в запросе."""
    captured_payload = {}

    class CapturingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, headers=None, json=None):
            captured_payload["json"] = json
            return FakeResponse(200, "CampaignId\tCampaignName\tImpressions\tClicks\tCost\tCtr\tAvgCpc\n")

    with patch("httpx.AsyncClient", return_value=CapturingClient()):
        run(direct.fetch_metrics(oauth_token="tok", client_login="login", campaign_ids=[], period_hours=24))

    selection = captured_payload["json"]["params"]["SelectionCriteria"]
    assert "Filter" not in selection
    print("test_no_campaign_ids_means_no_filter: OK")


# ---------------------------------------------------------------------------
# 8. Cost/clicks/impressions aggregate -- покрыто test_normal_tsv_multiple_campaigns,
#    добавим явный тест с известными "круглыми" числами для проверки CPC.
# ---------------------------------------------------------------------------


def test_cpc_calculated_from_aggregates():
    tsv = (
        "CampaignId\tCampaignName\tImpressions\tClicks\tCost\tCtr\tAvgCpc\n"
        "111\tA\t1000\t100\t1000000000\t10.0\t10000000\n"  # 1000 руб / 100 кликов = 10 руб/клик
    )
    with _patch_post_sequence([FakeResponse(200, tsv)]):
        result = run(direct.fetch_metrics(oauth_token="tok", client_login="login", campaign_ids=[], period_hours=24))
    assert result["spend"] == 1000.0
    assert result["clicks"] == 100
    assert result["cpc"] == 10.0
    print(f"test_cpc_calculated_from_aggregates: OK (cpc={result['cpc']})")


# ---------------------------------------------------------------------------
# Конфигурация и таймаут
# ---------------------------------------------------------------------------


def test_not_configured():
    try:
        run(direct.fetch_metrics(oauth_token=None, client_login="login", campaign_ids=[], period_hours=24))
        assert False
    except direct.NotConfiguredError:
        print("test_not_configured: OK")


def test_timeout():
    class TimeoutClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, *a, **kw):
            raise httpx.TimeoutException("timed out")

    with patch("httpx.AsyncClient", return_value=TimeoutClient()):
        try:
            run(direct.fetch_metrics(oauth_token="tok", client_login="login", campaign_ids=[], period_hours=24))
            assert False
        except direct.DirectConnectorError as exc:
            assert "Timeout" in str(exc)
            print(f"test_timeout: OK ({exc})")


def test_debug_connection_ok():
    tsv = "CampaignId\tCampaignName\tImpressions\tClicks\tCost\tCtr\tAvgCpc\n111\tA\t10\t1\t10000000\t10.0\t10000000\n"
    with _patch_post_sequence([FakeResponse(200, tsv)]):
        result = run(direct.test_direct_connection(oauth_token="tok", client_login="login"))
    assert result["ok"] is True
    assert result["clicks"] == 1
    print("test_debug_connection_ok: OK")


if __name__ == "__main__":
    test_normal_tsv_multiple_campaigns()
    test_empty_tsv()
    test_header_only_no_data_rows()
    test_401_unauthorized()
    test_403_no_client_login_access()
    test_malformed_tsv_row_skipped()
    test_completely_broken_tsv()
    test_report_processing_then_success()
    test_report_processing_exceeds_max_retries()
    test_201_also_triggers_retry()
    test_campaign_ids_filter_included_in_request()
    test_no_campaign_ids_means_no_filter()
    test_cpc_calculated_from_aggregates()
    test_not_configured()
    test_timeout()
    test_debug_connection_ok()
    print()
    print("Все тесты Direct connector пройдены.")
