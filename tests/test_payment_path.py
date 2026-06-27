"""
Тесты для payment-path diagnostics:
1. _format_payment_path_block в owner_report.py -- stage-aware логика
2. fetch_payment_path_diagnostics в connectors/payment_path.py -- парсинг
3. Тест что build_owner_report принимает payment_path_diagnostics и не ломается
4. Тест что одна payment_started без success -- не P1
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.owner_report import _format_payment_path_block, build_owner_report
from app.rules import NormalizedMetrics


# -----------------------------------------------------------------------
# Хелперы
# -----------------------------------------------------------------------

def _metrics(
    signup=25, activation_1=20, activation_2=56,
    payment_started=1, payment_success=0,
    spend=3157, clicks=594,
) -> NormalizedMetrics:
    return NormalizedMetrics(
        period_key="7d",
        signup=signup,
        activation_1=activation_1,
        activation_2=activation_2,
        payment_started=payment_started,
        payment_success=payment_success,
        spend=spend,
        clicks=clicks,
        sources_ok={"product", "direct"},
    )


def _payment_path(
    registrations=25,
    channels_created=20,
    post_generations=56,
    pricing_viewed=0,
    payment_cta_clicked=0,
    payment_started=0,
    payment_success=0,
    payment_failed=0,
    payment_returned=0,
    missing_data=None,
) -> dict:
    return {
        "registrations": registrations,
        "channels_created": channels_created,
        "post_generations": post_generations,
        "pricing_viewed": pricing_viewed,
        "payment_cta_clicked": payment_cta_clicked,
        "payment_started": payment_started,
        "payment_success": payment_success,
        "payment_failed": payment_failed,
        "payment_returned": payment_returned,
        "missing_data": missing_data or [],
    }


# -----------------------------------------------------------------------
# Тесты _format_payment_path_block
# -----------------------------------------------------------------------

class TestFormatPaymentPathBlock:
    def test_none_returns_none(self):
        assert _format_payment_path_block(None) is None

    def test_not_configured_returns_none(self):
        assert _format_payment_path_block({"status": "not_configured"}) is None

    def test_error_shows_message(self):
        block = _format_payment_path_block({"status": "error", "error": "timeout 20s"})
        assert block is not None
        assert "недоступна" in block
        assert "timeout 20s" in block

    def test_not_available_shows_message(self):
        block = _format_payment_path_block({"status": "not_available"})
        assert block is not None
        assert "не подключён" in block

    def test_pricing_viewed_zero_correct_message(self):
        """pricing_viewed=0 (трекируется, но никто не смотрел) -- пишем что не доходят до тарифов."""
        block = _format_payment_path_block(_payment_path(pricing_viewed=0))
        assert block is not None
        assert "не доходят до тарифного экрана" in block
        # Не должно быть выводов про "тарифы плохие" / "текст тарифов"
        assert "текст тарифов" not in block

    def test_pricing_viewed_none_no_tracking(self):
        """pricing_viewed=None (событие не трекируется) -- честно пишем что трекинга нет."""
        data = _payment_path()
        data["pricing_viewed"] = None
        block = _format_payment_path_block(data)
        assert block is not None
        assert "не трекируется" in block or "трекинг события" in block

    def test_pricing_viewed_but_no_cta(self):
        """Люди видят тарифы, но не нажимают оплату -- вероятная зона: ценность/цена/доверие."""
        block = _format_payment_path_block(_payment_path(
            pricing_viewed=10,
            payment_cta_clicked=0,
        ))
        assert block is not None
        assert "видят тарифы" in block or "видят" in block
        assert "не нажимают" in block or "не нажимают оплату" in block

    def test_cta_clicked_but_no_payment_started(self):
        """CTA нажат, но Payment не создан -- техническая проблема backend."""
        block = _format_payment_path_block(_payment_path(
            pricing_viewed=10,
            payment_cta_clicked=5,
            payment_started=0,
        ))
        assert block is not None
        assert "Payment не создаётся" in block or "payment flow" in block

    def test_one_payment_started_no_success_is_not_p1(self):
        """1 payment_started без success -- ранний сигнал, не P1."""
        block = _format_payment_path_block(_payment_path(
            pricing_viewed=10,
            payment_cta_clicked=5,
            payment_started=1,
            payment_success=0,
        ))
        assert block is not None
        assert "ранний сигнал" in block
        assert "не P1" in block or "не P1" in block

    def test_three_payment_started_no_success_is_signal(self):
        """3+ payment_started без success -- достаточно для сигнала."""
        block = _format_payment_path_block(_payment_path(
            pricing_viewed=10,
            payment_cta_clicked=5,
            payment_started=3,
            payment_success=0,
        ))
        assert block is not None
        assert "достаточно для сигнала" in block or "достаточно" in block

    def test_payment_returned_not_confused_with_success(self):
        """payment_returned показывается отдельно и не смешивается с payment_success."""
        block = _format_payment_path_block(_payment_path(
            pricing_viewed=10,
            payment_cta_clicked=5,
            payment_started=3,
            payment_success=0,
            payment_returned=2,
        ))
        assert block is not None
        assert "возврат" in block.lower() or "вернулись" in block.lower()
        # Не должно говорить "оплатили" вместо возврата
        assert "успешно оплатили: 0" in block

    def test_payment_success_positive_message(self):
        """Есть успешные оплаты -- позитивное сообщение."""
        block = _format_payment_path_block(_payment_path(
            pricing_viewed=10,
            payment_cta_clicked=5,
            payment_started=3,
            payment_success=2,
        ))
        assert block is not None
        assert "работает" in block or "успешных оплат" in block

    def test_payment_failed_shown_as_separate_signal(self):
        """payment_failed показывается отдельным сигналом."""
        block = _format_payment_path_block(_payment_path(
            pricing_viewed=10,
            payment_cta_clicked=5,
            payment_started=3,
            payment_success=0,
            payment_failed=2,
        ))
        assert block is not None
        assert "ошибкой" in block or "отказов" in block

    def test_missing_data_shown(self):
        """missing_data из endpoint выводится в конце блока."""
        data = _payment_path()
        data["missing_data"] = ["pricing_viewed", "payment_cta_clicked"]
        block = _format_payment_path_block(data)
        assert block is not None
        assert "pricing_viewed" in block or "Не хватает" in block

    def test_zero_registrations_message(self):
        """0 регистраций -- пишем что начинать надо с привлечения."""
        block = _format_payment_path_block(_payment_path(registrations=0))
        assert block is not None
        assert "регистраций" in block
        assert "привлечен" in block.lower()

    def test_activations_zero_message(self):
        """Регистрации есть, но нет канала и постов -- онбординг."""
        block = _format_payment_path_block(_payment_path(
            registrations=10,
            channels_created=0,
            post_generations=0,
            pricing_viewed=0,
        ))
        assert block is not None
        assert "онбординг" in block or "первое действие" in block

    def test_block_header_present(self):
        """Блок всегда начинается с заголовка."""
        block = _format_payment_path_block(_payment_path())
        assert block is not None
        assert block.startswith("Путь до оплаты:")

    def test_registrations_and_channels_in_block(self):
        """Числа регистраций и каналов присутствуют в блоке."""
        block = _format_payment_path_block(_payment_path(
            registrations=25,
            channels_created=20,
        ))
        assert block is not None
        assert "25" in block
        assert "20" in block


# -----------------------------------------------------------------------
# Тесты build_owner_report с payment_path_diagnostics
# -----------------------------------------------------------------------

class TestBuildOwnerReportWithPaymentPath:
    def test_report_with_payment_path_renders_block(self):
        """build_owner_report принимает payment_path_diagnostics и включает блок."""
        pp = _payment_path(pricing_viewed=0)
        report = build_owner_report(
            "АвтоПост",
            _metrics(),
            payment_path_diagnostics=pp,
        )
        assert report is not None
        assert "Путь до оплаты:" in report

    def test_report_without_payment_path_no_block(self):
        """Если payment_path_diagnostics=None, блок не появляется."""
        report = build_owner_report(
            "АвтоПост",
            _metrics(),
            payment_path_diagnostics=None,
        )
        assert report is not None
        assert "Путь до оплаты:" not in report

    def test_report_payment_path_error_shows_unavailable(self):
        """Если endpoint недоступен, в отчёте есть соответствующее сообщение."""
        pp = {"status": "error", "error": "timeout"}
        report = build_owner_report(
            "АвтоПост",
            _metrics(),
            payment_path_diagnostics=pp,
        )
        assert report is not None
        assert "недоступна" in report

    def test_report_payment_path_not_configured_no_block(self):
        """not_configured не добавляет блок в отчёт."""
        pp = {"status": "not_configured"}
        report = build_owner_report(
            "АвтоПост",
            _metrics(),
            payment_path_diagnostics=pp,
        )
        assert report is not None
        assert "Путь до оплаты:" not in report

    def test_report_one_payment_started_not_p1_in_full_report(self):
        """В полном отчёте одна начатая оплата не создаёт P1."""
        pp = _payment_path(
            pricing_viewed=5,
            payment_cta_clicked=2,
            payment_started=1,
            payment_success=0,
        )
        report = build_owner_report(
            "АвтоПост",
            _metrics(payment_started=1),
            payment_path_diagnostics=pp,
        )
        assert report is not None
        # Оба места должны говорить "ранний сигнал, не P1"
        assert "ранний сигнал" in report

    def test_report_backward_compat_no_payment_path_param(self):
        """build_owner_report работает без payment_path_diagnostics (обратная совместимость)."""
        report = build_owner_report("АвтоПост", _metrics())
        assert report is not None


# -----------------------------------------------------------------------
# Тесты connector (unit, без HTTP)
# -----------------------------------------------------------------------

class TestPaymentPathConnector:
    @pytest.mark.asyncio
    async def test_not_configured_raises(self):
        from app.connectors.payment_path import fetch_payment_path_diagnostics, NotConfiguredError
        with pytest.raises(NotConfiguredError):
            await fetch_payment_path_diagnostics(base_url=None, api_token=None)

    @pytest.mark.asyncio
    async def test_not_configured_empty_string_raises(self):
        from app.connectors.payment_path import fetch_payment_path_diagnostics, NotConfiguredError
        with pytest.raises(NotConfiguredError):
            await fetch_payment_path_diagnostics(base_url="", api_token="")

    @pytest.mark.asyncio
    async def test_valid_response_parsed(self):
        from app.connectors.payment_path import fetch_payment_path_diagnostics
        import httpx
        from datetime import timezone

        mock_response_data = {
            "as_of": "2026-06-27T10:00:00Z",
            "registrations": 25,
            "channels_created": 20,
            "post_generations": 56,
            "pricing_viewed": 0,
            "payment_cta_clicked": 0,
            "payment_started": 1,
            "payment_success": 0,
            "payment_failed": 0,
            "payment_returned": 0,
            "quota_warning_seen": 3,
            "limit_reached": 1,
            "biggest_dropoff": "registrations -> channels_created",
            "likely_explanation": "онбординг",
            "missing_data": [],
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_response_data

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            result = await fetch_payment_path_diagnostics(
                base_url="https://example.com",
                api_token="test-token",
                period_hours=168,
            )

        assert result["registrations"] == 25
        assert result["channels_created"] == 20
        assert result["payment_started"] == 1
        assert result["payment_success"] == 0
        assert result["as_of"] is not None
        assert result["as_of"].tzinfo is not None
        assert result["biggest_dropoff"] == "registrations -> channels_created"
        assert result["missing_data"] == []

    @pytest.mark.asyncio
    async def test_missing_as_of_raises(self):
        from app.connectors.payment_path import fetch_payment_path_diagnostics, PaymentPathConnectorError
        import httpx

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"registrations": 25}  # нет as_of

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            with pytest.raises(PaymentPathConnectorError, match="as_of"):
                await fetch_payment_path_diagnostics(
                    base_url="https://example.com",
                    api_token="test-token",
                )

    @pytest.mark.asyncio
    async def test_http_500_raises(self):
        from app.connectors.payment_path import fetch_payment_path_diagnostics, PaymentPathConnectorError

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            with pytest.raises(PaymentPathConnectorError, match="HTTP 500"):
                await fetch_payment_path_diagnostics(
                    base_url="https://example.com",
                    api_token="test-token",
                )

    @pytest.mark.asyncio
    async def test_partial_fields_dont_crash(self):
        """Если endpoint не вернул некоторые поля -- не падаем, возвращаем None для них."""
        from app.connectors.payment_path import fetch_payment_path_diagnostics

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # Только минимум -- as_of + registrations, остальное отсутствует
        mock_resp.json.return_value = {
            "as_of": "2026-06-27T10:00:00Z",
            "registrations": 25,
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            result = await fetch_payment_path_diagnostics(
                base_url="https://example.com",
                api_token="test-token",
            )

        assert result["registrations"] == 25
        assert result["payment_started"] is None
        assert result["payment_success"] is None

    @pytest.mark.asyncio
    async def test_field_aliases_resolved(self):
        """Если AutoPost вернёт posts_generated вместо post_generations -- алиас разрешается."""
        from app.connectors.payment_path import fetch_payment_path_diagnostics

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "as_of": "2026-06-27T10:00:00Z",
            "registrations": 25,
            "posts_generated": 56,       # alias для post_generations
            "pricing_views": 10,         # alias для pricing_viewed
            "payment_cta_clicks": 5,     # alias для payment_cta_clicked
            "payments_started": 2,       # alias для payment_started
            "payments_success": 0,       # alias для payment_success
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            result = await fetch_payment_path_diagnostics(
                base_url="https://example.com",
                api_token="test-token",
            )

        assert result["post_generations"] == 56
        assert result["pricing_viewed"] == 10
        assert result["payment_cta_clicked"] == 5
        assert result["payment_started"] == 2
        assert result["payment_success"] == 0


class TestSchedulerImports:
    def test_force_refresh_landing_importable(self):
        """force_refresh_landing_funnel_diagnostics должна быть доступна после вставки новой функции."""
        from app.scheduler import force_refresh_landing_funnel_diagnostics
        import asyncio
        assert asyncio.iscoroutinefunction(force_refresh_landing_funnel_diagnostics)

    def test_run_payment_path_importable(self):
        """run_payment_path_diagnostics_for_project должна быть async функцией."""
        from app.scheduler import run_payment_path_diagnostics_for_project
        import asyncio
        assert asyncio.iscoroutinefunction(run_payment_path_diagnostics_for_project)
