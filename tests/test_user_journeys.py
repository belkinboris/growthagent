"""
Тесты для GET /api/internal/user-journeys (per-user воронка для Growth Agent).

Запуск:

    DATABASE_URL=sqlite:///test_uj.db TRUEPOST_INTERNAL_API_TOKEN=test-token SECRET_KEY=testsecret \\
        python3 -m uvicorn main:app --port 8306 --log-level error &
    sleep 3
    BASE_URL=http://localhost:8306 TRUEPOST_INTERNAL_API_TOKEN=test-token \\
        python3 test_user_journeys.py
"""

import asyncio
import os

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
INTERNAL_TOKEN = os.environ.get("TRUEPOST_INTERNAL_API_TOKEN", "test-token")

_counter = 0


def _email(prefix: str) -> str:
    global _counter
    _counter += 1
    # Намеренно нижний регистр -- /api/register лоуверкейсит email при
    # сохранении (main.py: email = data.email.strip().lower()), используем
    # тот же регистр в тестах чтобы не запутаться при последующих lookup.
    return f"{prefix}_{_counter}@uj.test".lower()


async def _register(client: httpx.AsyncClient, email: str, **extra) -> dict:
    payload = {"email": email, "password": "test12345", **extra}
    r = await client.post(f"{BASE_URL}/api/register", json=payload)
    r.raise_for_status()
    return r.json()


async def _product_event(client: httpx.AsyncClient, token: str, event: str, package_id: str = ""):
    r = await client.post(
        f"{BASE_URL}/api/product-event",
        json={"event": event, "package_id": package_id},
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    return r.json()


async def _journeys(client: httpx.AsyncClient, period_hours: int = 24, limit: int | None = None) -> dict:
    params = {"period_hours": period_hours}
    if limit is not None:
        params["limit"] = limit
    r = await client.get(
        f"{BASE_URL}/api/internal/user-journeys",
        params=params,
        headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
    )
    r.raise_for_status()
    return r.json()


def _write_payment(email: str, status: str):
    """Пишет Payment напрямую в БД (имитация платежа без реального YooKassa)."""
    import database
    from database import Payment, User
    from sqlmodel import select

    with database.session() as s:
        user = s.exec(select(User).where(User.email == email)).first()
        assert user is not None, f"Пользователь {email} не найден для записи Payment"
        s.add(Payment(user_id=user.id, package_id="p1", label=f"test-{status}-{email}", rub=990.0, tokens=100, status=status))
        s.commit()


# ── 1. endpoint требует INTERNAL_API_TOKEN ──────────────────────────────────

async def test_requires_internal_token(client):
    r = await client.get(f"{BASE_URL}/api/internal/user-journeys")
    assert r.status_code == 401, f"Без токена ожидается 401, получили {r.status_code}"

    r2 = await client.get(
        f"{BASE_URL}/api/internal/user-journeys",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r2.status_code == 401, f"С неверным токеном ожидается 401, получили {r2.status_code}"
    print("  endpoint требует корректный INTERNAL_API_TOKEN (401 без/с неверным) ✓")


# ── 2. endpoint не отдаёт персональные данные ───────────────────────────────

async def test_no_pii_leaked(client):
    email = _email("pii_check")
    await _register(client, email)
    data = await _journeys(client)

    serialized = str(data).lower()
    assert email.lower() not in serialized, "Email не должен присутствовать в ответе endpoint'а"
    assert "password" not in serialized
    assert "tg_username" not in serialized
    assert "tg_chat_id" not in serialized

    # user_key не должен быть похож на email/число напрямую
    found = False
    for j in data["journeys"]:
        if j["user_key"].lower() == email.lower():
            found = True
    assert not found, "user_key не должен совпадать с email"

    # Структурная проверка: каждый journey должен иметь user_key, не должен
    # иметь полей с именем email/username/phone/password
    for j in data["journeys"]:
        for forbidden in ["email", "password", "username", "phone", "tg_chat_id"]:
            assert forbidden not in j, f"Поле '{forbidden}' не должно быть в journey"
        assert j["user_key"].startswith("u_"), "user_key должен иметь формат u_<hash>"

    print("  endpoint не отдаёт email/password/username/tg_chat_id, user_key анонимен ✓")


# ── 3. journey содержит source attribution ──────────────────────────────────

async def test_journey_contains_source_attribution(client):
    email = _email("attr_check")
    await _register(
        client, email,
        lp_session="sess_" + email,
        utm_source="telegram_ads", utm_medium="cpc",
        utm_campaign="test", utm_content="test_ad",
    )
    data = await _journeys(client)

    matching = [j for j in data["journeys"] if j["utm_campaign"] == "test" and j["utm_content"] == "test_ad"]
    assert len(matching) >= 1, "Должна найтись хотя бы одна journey с campaign=test, content=test_ad"
    j = matching[0]
    assert j["source"] == "telegram_ads"
    assert j["utm_source"] == "telegram_ads"
    print("  journey содержит source/utm_source/utm_campaign/utm_content ✓")


# ── 4. pricing_viewed без payment_started -> stuck_at=tariff_screen ────────

async def test_stuck_at_tariff_screen(client):
    email = _email("stuck_tariff")
    reg = await _register(client, email)
    await _product_event(client, reg["token"], "pricing_viewed")

    data = await _journeys(client)
    matching = [j for j in data["journeys"] if j["pricing_viewed_at"] is not None and j["payment_started_at"] is None]
    found = any(j["stuck_at"] == "tariff_screen" for j in matching)
    assert found, "Хотя бы одна journey с pricing_viewed без payment_started должна иметь stuck_at='tariff_screen'"
    print("  pricing_viewed без payment_started -> stuck_at='tariff_screen' ✓")


# ── 5. payment_started без payment_success -> stuck_at=payment_path ────────

async def test_stuck_at_payment_path(client):
    email = _email("stuck_payment")
    await _register(client, email)
    _write_payment(email, "pending")

    data = await _journeys(client)
    matching = [j for j in data["journeys"] if j["payment_started_at"] is not None and j["payment_success_at"] is None]
    found = any(j["stuck_at"] == "payment_path" for j in matching)
    assert found, "Journey с payment_started без payment_success должна иметь stuck_at='payment_path'"
    print("  payment_started без payment_success -> stuck_at='payment_path' ✓")


# ── 6. payment_success -> stuck_at=paid ─────────────────────────────────────

async def test_stuck_at_paid(client):
    email = _email("stuck_paid")
    await _register(client, email)
    _write_payment(email, "paid")

    data = await _journeys(client)
    matching = [j for j in data["journeys"] if j["payment_success_at"] is not None]
    found = any(j["stuck_at"] == "paid" and j["last_step"] == "payment_success" for j in matching)
    assert found, "Journey с payment_success должна иметь stuck_at='paid' и last_step='payment_success'"
    print("  payment_success -> stuck_at='paid', last_step='payment_success' ✓")


# ── 7. raw post_generations не используется для last_step/stuck_at ─────────

async def test_post_generations_not_used_for_state(client):
    """
    Endpoint не должен импортировать/читать Post вообще -- проверяем это
    статически (по исходному коду модуля), а не только поведенчески,
    потому что поведенческая проверка легко даёт ложный 'зелёный' если
    Post просто не было создано в этом тесте.
    """
    import inspect
    import internal_user_journeys as uj

    source = inspect.getsource(uj)
    assert "from database import" in source
    import_line = [l for l in source.split("\n") if l.strip().startswith("from database import")][0]
    assert "Post" not in import_line, (
        f"internal_user_journeys.py не должен импортировать Post -- "
        f"raw post_generations не должен влиять на last_step/stuck_at. "
        f"Найдена строка импорта: {import_line}"
    )
    print("  internal_user_journeys.py не импортирует Post (post_generations не используется) ✓")


# ── 8. старый payment-path diagnostics endpoint не ломается ────────────────

async def test_legacy_payment_path_diagnostics_unaffected(client):
    r = await client.get(
        f"{BASE_URL}/api/internal/payment-path-diagnostics",
        headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
    )
    assert r.status_code == 200, f"payment-path-diagnostics должен продолжать работать, получили {r.status_code}"
    data = r.json()
    for field in ["registrations", "source_breakdown", "post_generations_breakdown", "event_map"]:
        assert field in data, f"Поле '{field}' должно остаться в payment-path-diagnostics"
    print("  payment-path-diagnostics endpoint не сломан, все поля на месте ✓")


# ── Runner ───────────────────────────────────────────────────────────────

async def main():
    print(f"\nБаза: {os.environ.get('DATABASE_URL', 'sqlite:///./postbot.db')}")
    print(f"Сервер: {BASE_URL}\n")

    tests = [
        test_requires_internal_token,
        test_no_pii_leaked,
        test_journey_contains_source_attribution,
        test_stuck_at_tariff_screen,
        test_stuck_at_payment_path,
        test_stuck_at_paid,
        test_post_generations_not_used_for_state,
        test_legacy_payment_path_diagnostics_unaffected,
    ]

    passed = 0
    failed = 0
    async with httpx.AsyncClient(timeout=15.0) as client:
        for test in tests:
            try:
                await test(client)
                passed += 1
            except Exception as e:
                print(f"  FAIL {test.__name__}: {e}")
                failed += 1

    print(f"\n{'='*50}")
    print(f"Результат: {passed} прошли, {failed} упали")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
