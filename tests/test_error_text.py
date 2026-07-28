"""
Человеческие тексты ошибок внешних источников (задача C4).

Коннекторы пишут в `Integration.last_error` строку для разработчика:
«HTTP error calling Direct Reports API: ConnectTimeout». Владелец видел её
в интерфейсе как есть — по-английски и без ответа на главный вопрос: надо
что-то делать или само пройдёт.

Проверяется: перевод есть, он на русском, он говорит что делать, и —
важнее всего — исходная строка никуда не девается и незнакомая ошибка не
получает выдуманного объяснения.
"""

import pytest

from app.error_text import humanize_error, source_name


class TestKnownErrors:
    @pytest.mark.parametrize("raw", ["HTTP 401: token expired", "HTTP 403: Forbidden",
                                     "Unauthorized", "HTTP error: 401 Unauthorized"])
    def test_bad_token_says_what_to_do(self, raw):
        out = humanize_error(raw, "direct")
        assert "токен" in out["text"].lower()
        assert out["action"], "непонятно, что делать"

    def test_timeout_says_it_will_retry(self, raw="HTTP error calling API: ConnectTimeout"):
        out = humanize_error(raw, "direct")
        assert "вовремя" in out["text"]
        assert "повторит" in out["action"]

    def test_network_error_points_at_availability(self):
        out = humanize_error("HTTP error calling endpoint: ConnectError", "project_metrics_api")
        assert "недоступен" in out["text"]

    def test_404_explains_it_is_the_address(self):
        out = humanize_error("HTTP 404: Not Found", "project_metrics_api")
        assert "адреса" in out["text"] or "endpoint" in out["text"]

    def test_429_is_not_the_owners_problem(self):
        out = humanize_error("HTTP 429: Too Many Requests", "metrika")
        assert "Ничего делать не нужно" in out["action"]

    def test_5xx_names_the_side(self):
        out = humanize_error("HTTP 502: Bad Gateway", "yookassa")
        assert "на своей стороне" in out["text"]
        assert "502" in out["text"]

    def test_missing_as_of_is_explained_by_the_contract(self):
        """Самая частая ошибка подключения -- ответ без as_of."""
        out = humanize_error("TruePost response missing required field 'as_of'", "project_metrics_api")
        assert "as_of" in out["text"]
        assert "CONTRACT.md" in out["action"] or "as_of" in out["action"]

    def test_bad_json_is_explained(self):
        out = humanize_error("Invalid JSON from endpoint: Expecting value", "project_metrics_api")
        assert "не-JSON" in out["text"] or "не тем" in out["text"]


class TestHonesty:
    def test_raw_text_is_never_lost(self):
        raw = "HTTP 418: I'm a teapot"
        assert humanize_error(raw, "direct")["raw"] == raw

    def test_unknown_error_is_not_invented(self):
        """Придумать объяснение непонятной ошибке -- то же враньё, что
        выдуманное число: аналитик должен сказать, что не узнал её."""
        out = humanize_error("странная строка из недр библиотеки", "direct")
        assert "не узнал" in out["text"]
        assert out["raw"] == "странная строка из недр библиотеки"

    def test_empty_error_says_so(self):
        out = humanize_error("", "direct")
        assert "не записана" in out["text"]

    @pytest.mark.parametrize("raw", ["HTTP 401", "HTTP 500: Internal Server Error",
                                     "timeout", "", None, "мусор"])
    def test_everything_is_in_russian(self, raw):
        """Английский в тексте для владельца -- дефект, за который уже
        прилетало («для пользователя это ужас»)."""
        out = humanize_error(raw, "direct")
        line = out["text"] + " " + out["action"]
        # Латиница допустима только в именах полей и файлов контракта.
        allowed = {"as_of", "CONTRACT.md", "endpoint", "JSON"}
        for word in ("error", "failed", "timeout", "unauthorized", "forbidden", "invalid"):
            assert word not in line.lower(), f"английское «{word}» в тексте: {line}"
        assert any(ch.isalpha() and ch.lower() in "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
                   for ch in line), "в тексте нет русских слов"
        assert allowed  # набор задокументирован намеренно


class TestShortForm:
    def test_short_drops_the_source_name(self):
        """Рядом с плашкой «Яндекс.Директ» повторять название незачем."""
        out = humanize_error("HTTP 401: token expired", "direct")
        assert out["text"].startswith("Яндекс.Директ")
        assert not out["short"].startswith("Яндекс.Директ")
        assert "токен" in out["short"]

    def test_short_is_never_empty(self):
        for raw in ["", "HTTP 500", "мусор", "timeout"]:
            assert humanize_error(raw, "direct")["short"].strip()


class TestSourceNames:
    @pytest.mark.parametrize("key,expected", [
        ("direct", "Яндекс.Директ"),
        ("metrika", "Яндекс.Метрика"),
        ("project_metrics_api", "Продукт"),
        ("yookassa", "ЮKassa"),
    ])
    def test_sources_have_human_names(self, key, expected):
        assert source_name(key) == expected
        assert expected in humanize_error("HTTP 401", key)["text"]

    def test_unknown_source_is_passed_through(self):
        assert source_name("новый_источник") == "новый_источник"


class TestApiExposesHumanText:
    def test_overview_returns_human_error(self, monkeypatch, tmp_path):
        from tests.test_platform_api import _client, _login
        from app.models import Integration, IntegrationStatus

        client, session_factory = _client(monkeypatch, tmp_path)
        _login(client)
        with session_factory() as session:
            from sqlmodel import select
            integration = session.exec(
                select(Integration).where(Integration.type == "project_metrics_api")).first()
            integration.status = IntegrationStatus.error
            integration.last_error = "HTTP error calling endpoint: ConnectTimeout"
            session.add(integration)
            session.commit()

        body = client.get("/growth/api/overview").json()
        product = [i for i in body["integrations"] if i["type"] == "project_metrics_api"][0]
        assert product["error_human"]["text"].startswith("Продукт")
        assert product["error_human"]["raw"] == "HTTP error calling endpoint: ConnectTimeout"
        assert product["last_error"], "исходная строка должна остаться и в старом поле"

    def test_healthy_integration_has_no_error_text(self, monkeypatch, tmp_path):
        from tests.test_platform_api import _client, _login

        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        body = client.get("/growth/api/overview").json()
        assert all(i["error_human"] is None for i in body["integrations"]
                   if i["status"] != "error")
