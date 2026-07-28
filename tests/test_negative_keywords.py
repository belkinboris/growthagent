"""
Готовый список минус-фраз (задача D1).

Глубокая проверка Директа уже находила запросы, которые жгут бюджет и не
приводят людей, но лежало это внутри длинного текстового отчёта: чтобы
воспользоваться, надо было выписывать фразы руками.

Сознательное ограничение продукта: аналитик минус-фразы НЕ применяет.
Правку в рекламном кабинете делает человек — платформа только собирает
список и объясняет, почему каждая фраза в нём оказалась.
"""

import pytest

from app.service import DIRECT_INTELLIGENCE_CACHE_PERIOD_KEY, save_diagnostics_cache
from tests.test_platform_api import _client, _login

PAYLOAD = {
    "period_label": "7д",
    "safe_negatives": [
        {"query": "скачать бесплатно", "clicks": 34, "cost": 512.4,
         "reason": "34 клика, 0 регистраций — ищут бесплатное", "campaign_name": "Поиск"},
        {"query": "вакансия", "clicks": 12, "cost": 180.0,
         "reason": "ищут работу, а не продукт", "campaign_name": "Поиск"},
    ],
    "has_registration_attribution": True,
    "total_queries_analyzed": 210,
}


def _cache(session_factory, payload=PAYLOAD, ok=True):
    with session_factory() as session:
        save_diagnostics_cache(session, 1, DIRECT_INTELLIGENCE_CACHE_PERIOD_KEY,
                               "test", payload, ok=ok)


class TestList:
    def test_phrases_and_ready_text(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory)

        body = client.get("/growth/api/ads/negative-keywords").json()
        assert body["ok"] is True
        assert [p["query"] for p in body["phrases"]] == ["скачать бесплатно", "вакансия"]
        # Текст для вставки в Директ -- по фразе на строку, без лишнего.
        assert body["text"] == "скачать бесплатно\nвакансия"
        assert body["total_cost"] == 692.4

    def test_every_phrase_explains_itself(self, monkeypatch, tmp_path):
        """Список без причин -- просьба доверять вслепую, а решение
        принимает человек."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory)

        for phrase in client.get("/growth/api/ads/negative-keywords").json()["phrases"]:
            assert phrase["reason"], f"нет причины у фразы {phrase['query']}"

    def test_no_check_yet_is_explained(self, monkeypatch, tmp_path):
        """Пустота объясняется словами и говорит, что нажать."""
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)

        body = client.get("/growth/api/ads/negative-keywords").json()
        assert body["ok"] is False and body["phrases"] == []
        assert "Проверить глубже" in body["hint"]

    def test_nothing_to_cut_is_not_an_error(self, monkeypatch, tmp_path):
        """«Резать нечего» -- нормальный результат, а не поломка."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory, {**PAYLOAD, "safe_negatives": []})

        body = client.get("/growth/api/ads/negative-keywords").json()
        assert body["ok"] is True and body["phrases"] == []
        assert "не нашёл" in body["hint"]

    def test_failed_check_is_treated_as_no_data(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory, PAYLOAD, ok=False)

        body = client.get("/growth/api/ads/negative-keywords").json()
        assert body["ok"] is False

    def test_rows_without_query_are_skipped(self, monkeypatch, tmp_path):
        """Битая строка не должна попасть в текст для вставки пустой
        строкой: в Директе это мусорная минус-фраза."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory, {**PAYLOAD, "safe_negatives": [
            {"query": "", "clicks": 1, "cost": 1.0, "reason": "битая строка"},
            {"query": "вакансия", "clicks": 12, "cost": 180.0, "reason": "ищут работу"},
        ]})

        body = client.get("/growth/api/ads/negative-keywords").json()
        assert body["text"] == "вакансия"


class TestHonesty:
    def test_unreliable_attribution_is_flagged(self, monkeypatch, tmp_path):
        """Без надёжной атрибуции «нет регистраций» может означать
        «мы их не увидели» -- и вырезать фразу по такому выводу опасно."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory, {**PAYLOAD, "has_registration_attribution": False,
                         "registration_attribution_note": "цель регистрации не указана"})

        body = client.get("/growth/api/ads/negative-keywords").json()
        assert body["has_registration_attribution"] is False
        assert "цель регистрации" in body["attribution_note"]

    def test_reliable_attribution_is_reported_as_such(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory)
        assert client.get("/growth/api/ads/negative-keywords").json()[
            "has_registration_attribution"] is True

    def test_nothing_is_applied_automatically(self, monkeypatch, tmp_path):
        """Аналитик не трогает рекламу: у endpoint'а нет и не должно быть
        побочных эффектов, а в ответе -- только данные для человека."""
        client, factory = _client(monkeypatch, tmp_path)
        _login(client)
        _cache(factory)

        body = client.get("/growth/api/ads/negative-keywords").json()
        assert set(body) == {
            "ok", "phrases", "text", "total_cost", "period_label", "checked_at",
            "attribution_note", "has_registration_attribution", "hint",
        }, "в ответе появилось что-то, кроме списка и пояснений"

    def test_requires_auth(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        assert client.get("/growth/api/ads/negative-keywords").status_code == 401
