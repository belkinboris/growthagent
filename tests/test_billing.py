"""
Тарифы платформы (задачи E1/E2).

Платформа пока обслуживает одного владельца — продавать некому. COMMERCIAL_MODE
выключен по умолчанию, и это должно означать «лимитов и оплаты нет вообще»,
а не «функция недоделана»: главное, что здесь проверяется, — выключенный
режим НИЧЕГО не меняет для текущего использования, а включённый действительно
ограничивает бесплатный тариф и не начисляет платный без подтверждённой оплаты.
"""

import pytest

from tests.test_platform_api import _client, _login, _register


def _wizard_body(name="Второй проект"):
    return {"name": name, "type": "web_app", "base_url": "https://ok.example",
            "internal_api_token": "tok"}


class TestCommercialModeOff:
    def test_second_project_is_not_blocked_by_default(self, monkeypatch, tmp_path):
        """Сейчас платформа обслуживает только владельца -- лимитов не должно
        быть вообще, пока COMMERCIAL_MODE не включён явно."""
        client, factory = _client(monkeypatch, tmp_path)
        _register(client, "ivan@example.com")  # усыновляет env-проект (уже 1)

        resp = client.post("/growth/api/projects", json=_wizard_body())
        assert resp.status_code == 422, "должно упасть на probe (недоступный адрес), не на лимите"

    def test_plans_endpoint_reports_mode_off(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _login(client)
        body = client.get("/growth/api/billing/plans").json()
        assert body["commercial_mode"] is False

    def test_checkout_is_closed_when_mode_off(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path)
        _register(client, "ivan@example.com")
        resp = client.post("/growth/api/billing/checkout", json={"plan": "pro"})
        assert resp.status_code == 404


class TestCommercialModeOn:
    def test_free_plan_is_limited_to_one_project(self, monkeypatch, tmp_path):
        client, factory = _client(monkeypatch, tmp_path, COMMERCIAL_MODE="true")
        _register(client, "ivan@example.com")  # уже владеет env-проектом (1 из 1)

        resp = client.post("/growth/api/projects", json=_wizard_body())
        assert resp.status_code == 402
        assert "Pro" in resp.json()["detail"]

    def test_env_owner_is_not_limited(self, monkeypatch, tmp_path):
        """Вход по паролю из окружения -- это сам владелец платформы,
        ограничивать себя же самого бессмысленно."""
        client, _ = _client(monkeypatch, tmp_path, COMMERCIAL_MODE="true")
        _login(client)
        resp = client.post("/growth/api/projects", json=_wizard_body())
        assert resp.status_code == 422, "владелец платформы не должен упереться в лимит"

    def test_usage_and_current_plan_are_reported(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path, COMMERCIAL_MODE="true")
        _register(client, "ivan@example.com")

        body = client.get("/growth/api/billing/plans").json()
        assert body["commercial_mode"] is True
        assert body["current_plan"] == "free"
        assert body["usage"]["projects"] == 1

    def test_checkout_requires_an_account(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path, COMMERCIAL_MODE="true")
        _login(client)  # вход владельца платформы, без аккаунта
        resp = client.post("/growth/api/billing/checkout", json={"plan": "pro"})
        assert resp.status_code == 400

    def test_checkout_rejects_free_plan(self, monkeypatch, tmp_path):
        client, _ = _client(monkeypatch, tmp_path, COMMERCIAL_MODE="true")
        _register(client, "ivan@example.com")
        resp = client.post("/growth/api/billing/checkout", json={"plan": "free"})
        assert resp.status_code == 400

    def test_checkout_without_yookassa_credentials_is_a_clear_error(self, monkeypatch, tmp_path):
        """ЮKassa не настроена -- честная ошибка, а не заглушка с фальшивой ссылкой."""
        client, _ = _client(monkeypatch, tmp_path, COMMERCIAL_MODE="true")
        _register(client, "ivan@example.com")
        resp = client.post("/growth/api/billing/checkout", json={"plan": "pro"})
        assert resp.status_code == 502
        assert "не настроена" in resp.json()["detail"]


class TestCheckoutAndWebhook:
    def _configured_client(self, monkeypatch, tmp_path):
        return _client(
            monkeypatch, tmp_path, COMMERCIAL_MODE="true",
            PLATFORM_YOOKASSA_SHOP_ID="shop", PLATFORM_YOOKASSA_SECRET_KEY="secret",
        )

    def test_successful_payment_activates_pro(self, monkeypatch, tmp_path):
        from app import billing_platform

        client, factory = self._configured_client(monkeypatch, tmp_path)
        _register(client, "ivan@example.com")

        async def fake_create(settings, **kwargs):
            return {"id": "pay_1", "confirmation": {"confirmation_url": "https://yookassa.ru/pay_1"}}

        async def fake_get(settings, payment_id):
            assert payment_id == "pay_1"
            return {"status": "succeeded", "paid": True}

        monkeypatch.setattr(billing_platform, "create_checkout", fake_create)
        monkeypatch.setattr(billing_platform, "get_payment", fake_get)

        checkout = client.post("/growth/api/billing/checkout", json={"plan": "pro"})
        assert checkout.status_code == 200
        assert checkout.json()["confirmation_url"] == "https://yookassa.ru/pay_1"

        webhook = client.post("/growth/api/billing/yookassa/notify",
                              json={"object": {"id": "pay_1"}})
        assert webhook.status_code == 200

        body = client.get("/growth/api/billing/plans").json()
        assert body["current_plan"] == "pro"

        # После оплаты лимит free-тарифа больше не действует.
        resp = client.post("/growth/api/projects", json=_wizard_body())
        assert resp.status_code == 422, "оплаченный тариф должен снять лимит проектов"

    def test_webhook_is_not_trusted_blindly(self, monkeypatch, tmp_path):
        """Вебхук можно подделать -- активация идёт только по ответу самой
        ЮKassa на повторный запрос статуса, не по телу вебхука."""
        from app import billing_platform

        client, factory = self._configured_client(monkeypatch, tmp_path)
        _register(client, "ivan@example.com")

        async def fake_create(settings, **kwargs):
            return {"id": "pay_2", "confirmation": {"confirmation_url": "https://yookassa.ru/pay_2"}}

        async def fake_get_not_paid(settings, payment_id):
            return {"status": "pending", "paid": False}

        monkeypatch.setattr(billing_platform, "create_checkout", fake_create)
        monkeypatch.setattr(billing_platform, "get_payment", fake_get_not_paid)

        client.post("/growth/api/billing/checkout", json={"plan": "pro"})
        client.post("/growth/api/billing/yookassa/notify", json={"object": {"id": "pay_2", "status": "succeeded"}})

        assert client.get("/growth/api/billing/plans").json()["current_plan"] == "free"

    def test_webhook_does_not_double_activate(self, monkeypatch, tmp_path):
        """Повторный вебхук на уже активную подписку не должен упасть
        и не должен ничего пересчитывать."""
        from app import billing_platform

        client, factory = self._configured_client(monkeypatch, tmp_path)
        _register(client, "ivan@example.com")

        async def fake_create(settings, **kwargs):
            return {"id": "pay_3", "confirmation": {"confirmation_url": "https://yookassa.ru/pay_3"}}

        calls = {"n": 0}

        async def fake_get(settings, payment_id):
            calls["n"] += 1
            return {"status": "succeeded", "paid": True}

        monkeypatch.setattr(billing_platform, "create_checkout", fake_create)
        monkeypatch.setattr(billing_platform, "get_payment", fake_get)

        client.post("/growth/api/billing/checkout", json={"plan": "pro"})
        client.post("/growth/api/billing/yookassa/notify", json={"object": {"id": "pay_3"}})
        client.post("/growth/api/billing/yookassa/notify", json={"object": {"id": "pay_3"}})

        assert calls["n"] == 2  # обе проверки статуса были, повторной активации не было
        assert client.get("/growth/api/billing/plans").json()["current_plan"] == "pro"

    def test_unknown_payment_id_does_not_crash(self, monkeypatch, tmp_path):
        client, _ = self._configured_client(monkeypatch, tmp_path)
        resp = client.post("/growth/api/billing/yookassa/notify",
                           json={"object": {"id": "not-ours"}})
        assert resp.status_code == 200
