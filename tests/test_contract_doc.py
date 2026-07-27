"""
CONTRACT.md — единственный документ, который читает сторона клиента, когда
подключает свой продукт. 23.07.2026 при загрузке файлов его содержимое было
затёрто чужим кодом (внутри лежали тесты `app/ask.py`), и это никто не
заметил четыре дня: документ ничем не проверялся.

Тесты держат документ в связи с кодом:
- он должен быть документом, а не исходником;
- в нём должен быть описан каждый endpoint, который платформа спрашивает
  у продукта при подключении;
- в нём должны быть перечислены нормализованные шаги воронки и поля,
  которые реально читают коннекторы.

Это не проверка орфографии: любое расхождение здесь означает, что клиент
получит инструкцию, по которой подключение не заработает.
"""

from pathlib import Path

import pytest

from app.config import CORE_FUNNEL_KEYS
from app.connectors.landing import _FUNNEL_FIELDS as LANDING_FIELDS
from app.connectors.truepost import DEFAULT_FUNNEL_MAPPING
from app.connectors.user_journeys import _EXPECTED_JOURNEY_FIELDS
from app.platform_api import INTERNAL_ENDPOINT_PROBES

CONTRACT = Path(__file__).resolve().parents[1] / "CONTRACT.md"


@pytest.fixture(scope="module")
def text() -> str:
    assert CONTRACT.exists(), f"не найден {CONTRACT}"
    return CONTRACT.read_text(encoding="utf-8")


class TestNotCorruptedAgain:
    def test_starts_as_document(self, text):
        assert text.lstrip().startswith("#"), "CONTRACT.md начинается не с заголовка"

    @pytest.mark.parametrize("marker", ['"""', "import ", "def ", "class ", "assert "])
    def test_no_python_source_at_line_start(self, text, marker):
        """Ровно так выглядела порча: в документ попал питоновский файл.
        Внутри примеров кода такие строки не встречаются -- примеры в
        CONTRACT.md на JSON и curl."""
        offenders = [ln for ln in text.splitlines() if ln.startswith(marker)]
        assert not offenders, f"похоже на исходник в документе: {offenders[:3]}"

    def test_is_in_russian(self, text):
        assert "Аналитик" in text or "аналитик" in text


class TestMatchesCode:
    @pytest.mark.parametrize("endpoint", [name for name, _ in INTERNAL_ENDPOINT_PROBES])
    def test_every_probed_endpoint_documented(self, text, endpoint):
        """Платформа сама проверяет эти endpoint'ы при подключении. Если
        какой-то не описан, клиент про него просто не узнает."""
        assert f"/api/internal/{endpoint}" in text

    def test_required_endpoint_marked_required(self, text):
        assert "Обязательный" in text or "обязательный" in text
        assert "/api/internal/metrics" in text

    @pytest.mark.parametrize("key", [k for k in CORE_FUNNEL_KEYS if k != "traffic"])
    def test_funnel_steps_documented(self, text, key):
        assert key in text, f"шаг воронки {key} не описан"

    def test_traffic_explained_as_external(self, text):
        """traffic продукт не отдаёт -- он из Директа/Метрики. Без этого
        объяснения клиент ищет у себя поле, которого не должно быть."""
        assert "traffic" in text and "Директ" in text

    @pytest.mark.parametrize("raw_key", sorted(set(DEFAULT_FUNNEL_MAPPING.values())))
    def test_default_mapping_fields_documented(self, text, raw_key):
        assert raw_key in text

    @pytest.mark.parametrize("field", LANDING_FIELDS)
    def test_landing_fields_documented(self, text, field):
        assert field in text

    @pytest.mark.parametrize("field", _EXPECTED_JOURNEY_FIELDS)
    def test_journey_fields_documented(self, text, field):
        assert field in text

    def test_auth_header_documented(self, text):
        """Коннекторы ходят только так -- см. Authorization: Bearer в connectors/."""
        assert "Authorization: Bearer" in text

    def test_as_of_requirement_documented(self, text):
        """Ответ без as_of коннектор отклоняет: это должно быть написано."""
        assert "as_of" in text

    def test_privacy_requirement_stated(self, text):
        """Приватность -- принцип продукта: наружу только анонимные ключи."""
        low = text.lower()
        assert "анонимн" in low
        assert "user_key" in text
