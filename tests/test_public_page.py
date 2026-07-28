"""
Страница-описание для входящих (задача B6).

Человек, впервые попавший на адрес аналитика, раньше видел одно поле
пароля — ни слова о том, куда он пришёл. Теперь там короткое описание:
что это, что покажет, как подключить и **чего аналитик не делает**.

Последний блок — не украшение: ограничения продукта (не решает за
владельца, не выдумывает числа, не собирает персональные данные, одна
проверка за раз) — это принципы из роадмапа, обещанные вслух. Тест держит
их на странице: тихо исчезнуть они не должны, а если принцип меняется —
пусть меняется осознанно, вместе с тестом.
"""

from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "app" / "static" / "platform" / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    assert INDEX.exists(), f"не найден {INDEX}"
    return INDEX.read_text(encoding="utf-8")


class TestIntroExists:
    def test_intro_block_present(self, html):
        assert 'class="intro"' in html
        assert "Аналитик Воронки" in html

    def test_three_questions_stated(self, html):
        """Обещание продукта -- три вопроса. Если их нет, страница не
        объясняет, зачем платформа нужна."""
        for phrase in ["что происходит", "где теряются люди", "что делать дальше"]:
            assert phrase in html, f"нет обещания «{phrase}»"

    def test_connection_steps_present(self, html):
        assert "/api/internal/metrics" in html
        assert "Заведите аккаунт" in html

    def test_login_form_still_on_the_page(self, html):
        """Описание не должно вытеснить вход: человек с аккаунтом заходит
        с того же экрана."""
        assert 'id="login-card"' in html
        assert 'id="login-btn"' in html


class TestHonestyPromises:
    """Каждый пункт -- принцип из PRODUCT_ROADMAP.md, заявленный вслух."""

    @pytest.mark.parametrize("promise", [
        "кнопку нажимает человек",          # аналитик не решает за владельца
        "Рекламу сам не меняет",            # сознательное ограничение продукта
        "Нет данных — так и пишет",         # честность данных
        "выборка",                          # честность про малую выборку
        "анонимные ключи",                  # приватность
        "Одна проверка за раз",             # одна проверка за раз
    ])
    def test_limit_is_stated(self, html, promise):
        assert promise in html, f"со страницы пропало обещание: «{promise}»"

    def test_no_invented_social_proof(self, html):
        """Отзывов и «тысяч клиентов» у продукта нет, и придумывать их
        нельзя: это то же враньё, что выдуманное число в отчёте."""
        for lie in ["тысячи клиентов", "нам доверяют", "отзыв клиента", "лучший на рынке"]:
            assert lie.lower() not in html.lower(), f"выдуманное обещание: «{lie}»"

    def test_optional_sources_marked_optional(self, html):
        """Директ пугает клиентов больше всего -- на входе должно быть
        сказано, что без него всё работает."""
        assert "Без него всё остальное работает" in html
