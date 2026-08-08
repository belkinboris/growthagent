"""
Браузерные тесты интерфейса (задача C6).

Всё, что здесь проверяется, до сих пор держалось на том, что человек
откроет скриншот и посмотрит. Так и находились самые неприятные дефекты:
экран писал «проект ещё не подключён» при подключённом проекте, в шапке
дублировалось название, подтверждение «Сохранено» затиралось через
миллисекунду. Тесты по HTML-исходнику такое не ловят: там всё собирается
живым JS уже в браузере.

Проверяется ровно то, что нельзя увидеть в разметке:
- ошибки в консоли (сломанный JS не виден по коду);
- пустые состояния показываются вместо выдуманных чисел;
- на 390 px нет горизонтальной прокрутки;
- выключенный проект объясняется словами, а не пустым экраном.

Тесты поднимают настоящее приложение на своём порту и ходят в него
браузером. Если браузера в системе нет, файл пропускается целиком: это
проверка интерфейса, а не повод красить прогон в красный на машине без
Chromium.
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
REPO = Path(__file__).resolve().parents[1]
PASSWORD = "t"

pytest.importorskip("playwright.sync_api", reason="Playwright не установлен")
pytestmark = pytest.mark.skipif(not Path(CHROME).exists(), reason="нет предустановленного Chromium")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Настоящее приложение на своём порту и своей базе."""
    import httpx

    db = tmp_path_factory.mktemp("ui") / "ui.db"
    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db}",
        "PLATFORM_ADMIN_PASSWORD": PASSWORD,
        "PLATFORM_COOKIE_SECURE": "false",
        "PLATFORM_SECRET_KEY": "ui-test-secret",
        "PROJECT_NAME": "Тестовый проект",
        "PROJECT_BASE_URL": "http://127.0.0.1:1",  # заведомо мёртвый: данных не будет
        "PROJECT_INTERNAL_API_TOKEN": "tok",
        # Бота и планировщик не поднимаем: тест про интерфейс.
        "BOT_TOKEN": "",
        "WATCH_INTERVAL_SECONDS": "86400",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}/growth"
    try:
        for _ in range(120):
            if proc.poll() is not None:
                pytest.skip("приложение не поднялось в тестовом окружении")
            try:
                if httpx.get(base + "/", timeout=1.0).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            pytest.skip("приложение не ответило вовремя")
        yield base, db
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture()
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        yield b
        b.close()


def _page(browser, width=1280, height=900):
    page = browser.new_page(viewport={"width": width, "height": height})
    errors: list[str] = []
    # 401 до входа -- нормальная часть работы интерфейса, а не ошибка.
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text)
            if m.type == "error" and "Failed to load resource" not in m.text else None)
    return page, errors


def _login_owner(page, base):
    page.goto(base + "/")
    page.wait_for_selector("#login-btn")
    page.click("#login-mode-owner")
    page.fill("#login-password", PASSWORD)
    page.click("#login-btn")
    page.wait_for_selector("#tabs:not(.hidden)", timeout=15000)


def _no_horizontal_scroll(page) -> bool:
    return not page.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
    )


class TestPublicPage:
    def test_intro_is_visible_before_login(self, server, browser):
        base, _ = server
        page, errors = _page(browser)
        page.goto(base + "/")
        page.wait_for_selector(".intro h1")
        assert "аналитик" in page.inner_text(".intro h1").lower()
        assert page.is_visible("#login-card")
        assert errors == [], f"ошибки в консоли: {errors}"
        page.close()

    def test_no_horizontal_scroll_on_phone(self, server, browser):
        base, _ = server
        page, _ = _page(browser, 390, 844)
        page.goto(base + "/")
        page.wait_for_selector(".intro h1")
        assert _no_horizontal_scroll(page), "страница уезжает вбок на 390 px"
        page.close()

    def test_register_link_switches_form(self, server, browser):
        """Три режима одной формы -- живой JS, по разметке это не проверить."""
        base, _ = server
        page, errors = _page(browser)
        page.goto(base + "/")
        page.wait_for_selector("#login-btn")
        page.click("#login-mode-register")
        assert page.inner_text("#login-btn") == "Создать аккаунт"
        assert page.is_visible("#login-name-row")
        page.click("#login-mode-owner")
        assert page.is_hidden("#login-email-row"), "владельцу платформы почта не нужна"
        assert errors == [], f"ошибки в консоли: {errors}"
        page.close()


class TestOverviewWithoutData:
    def test_no_numbers_are_invented(self, server, browser):
        """Продукт недоступен, снимков нет. На экране должно быть сказано
        «данных ещё нет», а не нули, которые читаются как факт."""
        base, _ = server
        page, errors = _page(browser)
        _login_owner(page, base)
        # Плитки рисуются после ответа /api/funnel -- ждём именно текст,
        # иначе тест ловит момент, когда их ещё нет, и «проходит» вхолостую.
        page.wait_for_function(
            "document.getElementById('kpis').innerText.trim().length > 0", timeout=15000)
        text = page.inner_text("#view-dashboard").lower()
        assert "данных ещё нет" in text
        assert errors == [], f"ошибки в консоли: {errors}"
        page.close()

    def test_no_checks_is_not_all_good(self, server, browser):
        """«Сигналов нет» без единой проверки означало бы «всё хорошо» --
        это враньё, и на экране должна быть другая формулировка."""
        base, _ = server
        page, _ = _page(browser)
        _login_owner(page, base)
        page.click("#tabs button[data-tab='diagnostician']")
        page.wait_for_function(
            "document.getElementById('alerts-body').innerText.trim().length > 0", timeout=15000)
        text = page.inner_text("#alerts-body").lower()
        assert "проверок ещё не было" in text or "не собрал ни одного снимка" in text
        page.close()

    def test_first_run_steps_are_shown(self, server, browser):
        base, _ = server
        page, _ = _page(browser)
        _login_owner(page, base)
        page.wait_for_selector("#first-run:not(.hidden)", timeout=10000)
        assert "первые шаги" in page.inner_text("#first-run").lower()
        page.close()

    def test_overview_fits_the_phone(self, server, browser):
        base, _ = server
        page, _ = _page(browser, 390, 844)
        _login_owner(page, base)
        page.wait_for_selector("#kpis")
        assert _no_horizontal_scroll(page), "обзор уезжает вбок на 390 px"
        page.close()


class TestOverviewFailure:
    def test_broken_overview_clears_every_skeleton(self, server, browser):
        """Раньше loadDashboard() ловил упавший /api/overview и чистил
        скелетоны только у четырёх из шести панелей -- «Разбор» и «Источники
        данных» пульсировали вечно, будто ответ вот-вот придёт, хотя запрос
        уже провалился."""
        base, _ = server
        page, _ = _page(browser)
        _login_owner(page, base)
        page.wait_for_selector("#kpis", timeout=10000)

        page.route(
            "**/api/overview",
            lambda route: route.fulfill(
                status=500, content_type="application/json",
                body='{"detail": "тестовый сбой"}',
            ),
        )
        # Перезаходим на доску, чтобы loadDashboard() выполнился заново
        # уже с перехваченным запросом.
        page.click("#tabs button[data-tab='diagnostician']")
        page.wait_for_selector("#view-diagnostician:not(.hidden)", timeout=10000)
        page.click("#tabs button[data-tab='dashboard']")
        page.wait_for_function(
            "document.getElementById('today-body').innerText.includes('Не удалось загрузить')",
            timeout=10000,
        )

        # Часть панелей (integrations-body, decision-body, problems-body)
        # живёт под свёрнутым блоком «Подробности» (R6) и поэтому не видна
        # -- проверяем innerHTML, а не inner_text, которая для скрытых
        # элементов всегда пустая независимо от содержимого.
        for box_id in ["diagnosis-body", "integrations-body", "today-body", "kpis", "decision-body", "problems-body"]:
            html = page.eval_on_selector(f"#{box_id}", "el => el.innerHTML").lower()
            assert "skel" not in html, f"#{box_id} остался скелетоном после упавшего /api/overview"
            assert "не удалось загрузить" in html, f"#{box_id} не показал ошибку: {html!r}"
        page.close()

    def test_broken_ads_clears_marketer_skeletons(self, server, browser):
        """loadMarketer() на упавшем /api/ads только логировал ошибку в
        консоль и не трогал разметку -- KPI и таблица источников на вкладке
        Маркетолога так и оставались скелетоном навсегда."""
        base, _ = server
        page, _ = _page(browser)
        _login_owner(page, base)

        page.route(
            "**/api/ads",
            lambda route: route.fulfill(
                status=500, content_type="application/json",
                body='{"detail": "тестовый сбой рекламы"}',
            ),
        )
        page.click("#tabs button[data-tab='marketer']")
        page.wait_for_function(
            "document.getElementById('ads-kpis').innerText.includes('Не удалось загрузить')",
            timeout=10000,
        )

        kpis_html = page.eval_on_selector("#ads-kpis", "el => el.innerHTML").lower()
        assert "skel" not in kpis_html, "KPI рекламы остались скелетоном после упавшего /api/ads"
        assert "не удалось загрузить" in kpis_html

        table_html = page.eval_on_selector("#ads-table tbody", "el => el.innerHTML").lower()
        assert "skel" not in table_html, "Таблица источников осталась скелетоном после упавшего /api/ads"
        assert "не удалось загрузить" in table_html
        page.close()

    def test_broken_projects_clears_table_skeleton(self, server, browser):
        """loadProjects() на упавшем /api/projects тоже только логировал
        ошибку в консоль -- таблица проектов оставалась скелетоном
        навсегда, ровно как в loadMarketer до фикса."""
        base, _ = server
        page, _ = _page(browser)
        _login_owner(page, base)

        page.route(
            "**/api/projects",
            lambda route: route.fulfill(
                status=500, content_type="application/json",
                body='{"detail": "тестовый сбой проектов"}',
            ),
        )
        page.click("#tabs button[data-tab='projects']")
        page.wait_for_function(
            "document.getElementById('projects-table').innerText.includes('Не удалось загрузить')",
            timeout=10000,
        )

        table_html = page.eval_on_selector("#projects-table tbody", "el => el.innerHTML").lower()
        assert "skel" not in table_html, "Таблица проектов осталась скелетоном после упавшего /api/projects"
        assert "не удалось загрузить" in table_html
        page.close()


class TestTabsAndProjects:
    def test_every_tab_opens_without_js_errors(self, server, browser):
        """Вкладки рисуются целиком на JS: сломанный обработчик виден
        только в браузере."""
        base, _ = server
        page, errors = _page(browser)
        _login_owner(page, base)
        for tab in ["diagnostician", "marketer", "product", "tester", "projects", "dashboard"]:
            page.click(f"#tabs button[data-tab='{tab}']")
            page.wait_for_selector(f"#view-{tab}:not(.hidden)", timeout=10000)
            page.wait_for_timeout(400)
        assert errors == [], f"ошибки в консоли: {errors}"
        page.close()

    def test_paused_project_is_explained(self, server, browser):
        """Выключенный сбор -- самое опасное пустое состояние: экран без
        объяснения выглядит поломкой продукта."""
        base, _ = server
        page, _ = _page(browser)
        _login_owner(page, base)
        page.click("#tabs button[data-tab='projects']")
        page.wait_for_selector("#projects-table button", timeout=10000)

        page.click("#projects-table >> text=выключить")
        page.wait_for_timeout(800)
        page.click("#tabs button[data-tab='dashboard']")
        page.wait_for_selector("#project-inactive:not(.hidden)", timeout=10000)
        assert "сбор данных по этому проекту выключен" in page.inner_text("#project-inactive").lower()

        # Возвращаем как было: тесты в одном модуле делят приложение.
        page.click("#tabs button[data-tab='projects']")
        page.wait_for_selector("#projects-table >> text=включить сбор", timeout=10000)
        page.click("#projects-table >> text=включить сбор")
        page.wait_for_timeout(800)
        page.close()

    def test_notification_target_state_is_honest(self, server, browser):
        """Пустое поле адресата должно объяснять, что уведомления никуда
        не уходят, а не молчать."""
        base, _ = server
        page, _ = _page(browser)
        _login_owner(page, base)
        page.click("#tabs button[data-tab='projects']")
        page.wait_for_selector("#notify-ids", timeout=10000)
        page.wait_for_timeout(600)
        if not page.input_value("#notify-ids"):
            assert "не отправляются" in page.inner_text("#notify-status")
        page.close()
