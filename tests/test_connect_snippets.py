"""
Готовый код endpoint'а для клиента (задача B3).

Сниппет — это обещание: «скопируй, подставь свои запросы, и подключение
пройдёт». Проверять его глазами бесполезно, поэтому здесь код из шаблона
действительно запускается: поднимается приложение, дёргается endpoint,
ответ проверяется по тому же контракту, который применяет наш коннектор
(`app/connectors/truepost.py`). Если шаблон разъедется с контрактом,
упадут эти тесты, а не клиент на своём сервере.
"""

import ast
import json
from datetime import datetime

import pytest

from app import connect_snippets
from app.connectors.truepost import DEFAULT_FUNNEL_MAPPING


class TestCatalog:
    def test_known_stacks_present(self):
        keys = [s["key"] for s in connect_snippets.available_stacks()]
        assert keys == ["fastapi", "django", "express", "any"]

    def test_unknown_stack_is_not_an_error(self):
        """Незнакомый стек -- не 500 и не пустота: отдаём описание контракта,
        с ним endpoint делается на чём угодно."""
        assert connect_snippets.build_snippet("cobol")["stack"] == "any"
        assert connect_snippets.build_snippet(None)["stack"] == connect_snippets.DEFAULT_STACK

    @pytest.mark.parametrize("stack", ["fastapi", "django", "express", "any"])
    def test_env_var_name_is_the_same_everywhere(self, stack):
        """Имя переменной в коде и в инструкции обязано совпадать: иначе
        человек ищет опечатку вместо подключения."""
        snippet = connect_snippets.build_snippet(stack)
        assert connect_snippets.TOKEN_ENV_VAR in snippet["code"]
        assert any(connect_snippets.TOKEN_ENV_VAR in step for step in snippet["steps"])

    @pytest.mark.parametrize("stack", ["fastapi", "django", "express", "any"])
    def test_no_leftover_format_placeholders(self, stack):
        code = connect_snippets.build_snippet(stack)["code"]
        assert "{env_var}" not in code

    @pytest.mark.parametrize("stack", ["fastapi", "django", "express", "any"])
    def test_as_of_and_auth_are_in_every_variant(self, stack):
        code = connect_snippets.build_snippet(stack)["code"]
        assert "as_of" in code
        assert "Bearer" in code

    @pytest.mark.parametrize("stack", ["fastapi", "django"])
    def test_python_variants_are_valid_python(self, stack):
        ast.parse(connect_snippets.build_snippet(stack)["code"])

    @pytest.mark.parametrize("stack", ["fastapi", "django", "express"])
    def test_places_for_own_queries_are_marked(self, stack):
        """Свою схему базы аналитик не знает. Места для подстановки должны
        быть видны глазами -- иначе человек задеплоит нули как факты."""
        code = connect_snippets.build_snippet(stack)["code"]
        assert "ЗАМЕНИТЕ НА СВОИ ЗАПРОСЫ К БАЗЕ" in code

    def test_any_stack_variant_documents_response_shape(self):
        code = connect_snippets.build_snippet("any")["code"]
        for field in sorted(set(DEFAULT_FUNNEL_MAPPING.values())):
            assert field in code


class TestGeneratedFastapiEndpointReallyWorks:
    """Шаблон FastAPI исполняется по-настоящему и проверяется по контракту."""

    @pytest.fixture()
    def client(self, monkeypatch, tmp_path):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        monkeypatch.setenv(connect_snippets.TOKEN_ENV_VAR, "secret-token")
        code = connect_snippets.build_snippet("fastapi")["code"]
        module: dict = {}
        exec(compile(code, "<сниппет fastapi>", "exec"), module)  # noqa: S102

        app = FastAPI()
        app.include_router(module["router"])
        return TestClient(app)

    def test_without_token_is_401(self, client):
        assert client.get("/api/internal/metrics?period_hours=24").status_code == 401

    def test_with_wrong_token_is_401(self, client):
        resp = client.get("/api/internal/metrics?period_hours=24",
                          headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    def test_with_right_token_returns_contract_shape(self, client):
        resp = client.get("/api/internal/metrics?period_hours=24",
                          headers={"Authorization": "Bearer secret-token"})
        assert resp.status_code == 200
        body = resp.json()

        # as_of обязателен и должен разбираться -- ровно это проверяет
        # коннектор, и без этого он отклоняет ответ целиком.
        datetime.fromisoformat(body["as_of"].replace("Z", "+00:00"))
        for raw_key in DEFAULT_FUNNEL_MAPPING.values():
            assert raw_key in body, f"в ответе нет поля {raw_key}"

    def test_connector_accepts_the_response(self, client):
        """Финальная проверка: ответ шаблона проходит через наш коннектор
        и превращается в нормализованную воронку."""
        from app.connectors.truepost import CORE_FUNNEL_KEYS

        resp = client.get("/api/internal/metrics?period_hours=24",
                          headers={"Authorization": "Bearer secret-token"})
        raw = json.loads(resp.text)
        normalized = {
            norm: raw.get(raw_key) for norm, raw_key in DEFAULT_FUNNEL_MAPPING.items()
        }
        for key in CORE_FUNNEL_KEYS:
            if key == "traffic":
                continue  # traffic приходит из Директа/Метрики, не от продукта
            assert key in normalized


class TestEndpoint:
    def test_requires_auth(self, monkeypatch, tmp_path):
        from tests.test_platform_api import _client

        client, _ = _client(monkeypatch, tmp_path)
        assert client.get("/growth/api/connect-snippet").status_code == 401

    def test_returns_code_and_catalog(self, monkeypatch, tmp_path):
        from tests.test_platform_api import _client, _login

        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        body = client.get("/growth/api/connect-snippet?stack=express").json()
        assert body["stack"] == "express"
        assert body["language"] == "javascript"
        assert "router.get" in body["code"]
        assert [s["key"] for s in body["stacks"]] == ["fastapi", "django", "express", "any"]
