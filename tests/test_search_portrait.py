"""
«Кто вас ищет» (задача R13): из поисковых запросов Директа — портрет спроса.

Жалоба владельца: «за 100 регистраций аналитик столько выводов мог бы
сделать — кто ищет, какие минус-слова и ключевые слова вносить, — а у нас
ноль анализа». Разбор показал три разрыва: (1) отчёт по запросам шёл без
целей, конверсий по запросу не было, «победители» не определялись в
принципе; (2) комментарий в планировщике утверждал, что API этого не
умеет, — устарело после R11 (Goals кладутся в КОРЕНЬ params, а не в
SelectionCriteria, где API молча их игнорирует); (3) наружу показывались
только минус-фразы.
"""
import pytest

from app.query_classifier import build_search_portrait, classify_search_queries


def _row(query, clicks=10, cost=200.0, registrations=None, **kw):
    return {"query": query, "clicks": clicks, "cost": cost,
            "impressions": clicks * 20, "campaign_name": "Поиск",
            "ad_group_name": "Группа", "registrations": registrations, **kw}


ROWS = [
    _row("нейросеть для постов в телеграм", registrations=6, cost=900.0),
    _row("автопостинг в телеграм канал", registrations=4, cost=600.0),
    _row("шапка youtube оформление", registrations=0, cost=800.0, clicks=12),
    _row("реферат по истории скачать", registrations=0, cost=300.0, clicks=7),
    _row("бот для канала", registrations=0, cost=90.0, clicks=2),
]


class TestWinnersFromConversions:
    def test_queries_with_registrations_become_winners(self):
        """До R13 это было невозможно: конверсии не доезжали до классификатора."""
        result = classify_search_queries(ROWS, registration_goal_id=111)
        winner_queries = {w.query for w in result.winners}
        assert "нейросеть для постов в телеграм" in winner_queries
        assert "автопостинг в телеграм канал" in winner_queries

    def test_without_goal_id_no_winner_is_invented(self):
        """Без атрибуции «победителей» не выдумываем — честная семантика."""
        rows = [dict(r, registrations=None) for r in ROWS]
        result = classify_search_queries(rows, registration_goal_id=None)
        assert result.winners == []
        assert not result.has_registration_attribution


class TestPortrait:
    def test_converting_words_come_from_registering_queries(self):
        result = classify_search_queries(ROWS, registration_goal_id=111).to_dict()
        p = build_search_portrait(result)
        words = {w["word"] for w in p["converting_words"]}
        assert "телеграм" in words
        assert "youtube" not in words

    def test_telegram_word_sums_registrations_across_queries(self):
        """Слово устойчивее запроса: «телеграм» встречается в двух запросах
        с 6 и 4 регистрациями — портрет обязан сложить их."""
        result = classify_search_queries(ROWS, registration_goal_id=111).to_dict()
        p = build_search_portrait(result)
        tg = next(w for w in p["converting_words"] if w["word"] == "телеграм")
        assert tg["registrations"] == 10
        assert tg["queries"] == 2

    def test_wasting_words_come_from_zero_registration_spend(self):
        result = classify_search_queries(ROWS, registration_goal_id=111).to_dict()
        p = build_search_portrait(result)
        words = {w["word"] for w in p["wasting_words"]}
        assert "youtube" in words or "шапка" in words
        assert "телеграм" not in words

    def test_stop_words_do_not_pollute_the_portrait(self):
        """«для», «в», «по» — не портрет человека, а связки языка."""
        result = classify_search_queries(ROWS, registration_goal_id=111).to_dict()
        p = build_search_portrait(result)
        all_words = {w["word"] for w in p["converting_words"]} | {
            w["word"] for w in p["wasting_words"]}
        assert not all_words & {"для", "в", "по", "скачать"}

    def test_keyword_candidates_are_the_winners_verbatim(self):
        result = classify_search_queries(ROWS, registration_goal_id=111).to_dict()
        p = build_search_portrait(result)
        assert {k["query"] for k in p["keyword_candidates"]} == {
            "нейросеть для постов в телеграм", "автопостинг в телеграм канал"}

    def test_without_attribution_no_converting_words_are_invented(self):
        """Главная честность: без атрибуции «слова, которые приводят людей»
        строить не из чего — пустой список, а не догадка по семантике."""
        rows = [dict(r, registrations=None) for r in ROWS]
        result = classify_search_queries(rows, registration_goal_id=None).to_dict()
        p = build_search_portrait(result)
        assert p["converting_words"] == []
        assert not p["has_attribution"]
        # А заведомый мусор виден и без атрибуции — по семантике.
        assert any(w["word"] in ("youtube", "шапка", "реферат")
                   for w in p["wasting_words"])

    def test_converting_word_is_never_a_minus_candidate(self):
        """
        «Нейросеть» приносит регистрации в одном запросе и стоит денег в
        другом, пустом. Посоветовать её в минус-слова — значит отрезать и
        победителей: конвертящее слово в списке транжир запрещено.
        """
        rows = ROWS + [_row("нейросеть напиши пост про бизнес",
                            registrations=0, cost=250.0, clicks=6)]
        result = classify_search_queries(rows, registration_goal_id=111).to_dict()
        p = build_search_portrait(result, top_n=50)
        wasting = {w["word"] for w in p["wasting_words"]}
        converting = {w["word"] for w in p["converting_words"]}
        assert not wasting & converting
        assert "нейросеть" not in wasting

    def test_portrait_itself_ignores_numbers_when_attribution_is_off(self):
        """
        Защита в глубину: даже если в кэше лежат числа регистраций при
        выключенной атрибуции (старый кэш, другая версия сборщика), портрет
        обязан их игнорировать сам, а не полагаться на дисциплину
        классификатора.
        """
        poisoned = {
            "has_registration_attribution": False,
            "winners": [], "safe_negatives": [], "do_not_touch": [],
            "watch": [{"query": "нейросеть для постов", "cost": 100.0,
                       "registrations": 7, "label": "watch"}],
        }
        p = build_search_portrait(poisoned)
        assert p["converting_words"] == []


class TestQueryReportCarriesConversions:
    def test_search_query_rows_include_per_goal_conversions(self):
        from app.connectors import direct

        tsv = (
            "CampaignId\tCampaignName\tAdGroupId\tAdGroupName\tQuery\t"
            "Impressions\tClicks\tCost\tCtr\tAvgCpc\tConversions_111_LSCCD\n"
            "1\tПоиск\t10\tГруппа\tнейросеть для постов\t200\t10\t900000000\t5.0\t90000000\t6\n"
        )
        header, data_rows = direct._parse_tsv(tsv)
        row = data_rows[0]
        assert direct._conversions_from_row(row) == {"111": 6}

    def test_scheduler_maps_goal_conversions_to_registrations(self):
        """Строка отчёта -> поле registrations, которое читает классификатор."""
        rows = [{"query": "x", "conversions": {"111": 3}},
                {"query": "y", "conversions": {}}]
        goal_key = "111"
        for row in rows:
            row["registrations"] = (row.get("conversions") or {}).get(goal_key)
        assert rows[0]["registrations"] == 3
        assert rows[1]["registrations"] is None


class TestSearchQueriesEndpoint:
    def _seed_cache(self, session_factory, pid):
        from app.query_classifier import classify_search_queries
        from app.service import DIRECT_INTELLIGENCE_CACHE_PERIOD_KEY, save_diagnostics_cache

        result = classify_search_queries(ROWS, registration_goal_id=111,
                                         period_label="7д").to_dict()
        with session_factory() as session:
            save_diagnostics_cache(session, pid, DIRECT_INTELLIGENCE_CACHE_PERIOD_KEY,
                                   "deep_direct", result, ok=True)

    def test_endpoint_returns_portrait(self, monkeypatch, tmp_path):
        from tests.test_platform_api import _client, _login, _project_id

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        self._seed_cache(session_factory, pid)
        _login(client)

        r = client.get("/growth/api/ads/search-queries").json()
        assert r["ok"] is True
        assert r["portrait"]["keyword_candidates"]
        assert r["portrait"]["converting_words"]
        assert r["has_attribution"] is True

    def test_endpoint_is_honest_without_a_check(self, monkeypatch, tmp_path):
        from tests.test_platform_api import _client, _login

        client, session_factory = _client(monkeypatch, tmp_path)
        _login(client)
        r = client.get("/growth/api/ads/search-queries").json()
        assert r["ok"] is False
        assert "Проверить глубже" in r["hint"]
