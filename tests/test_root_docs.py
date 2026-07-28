"""
Документы в корне репозитория (задача C9).

23.07.2026 при загрузке через веб-интерфейс GitHub файлы перемешались:
в `PROJECT_STATE_GROWTHAGENT.md` попала копия `.env.example`, в
`CONTRACT.md` — тесты `app/ask.py`, в `SPEC_TRUEPOST_QUEUE_OFFER.md` —
текст, который должен был лежать в PROJECT_STATE, а в
`SPEC_TRUEPOST_GENERATOR_STYLE.md` — одна строка Procfile. Порчу заметили
через четыре дня и разбирали тремя отдельными задачами (C3, C8, C9),
потому что документы ничем не проверялись.

Эти тесты — дешёвая страховка от повторения: документ должен быть
документом, а не чужим кодом или копией конфига.
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOCS = sorted(p for p in REPO.glob("*.md"))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _lines_outside_code_blocks(text: str) -> list[str]:
    lines, inside = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if not inside:
            lines.append(line)
    return lines


def test_docs_are_found():
    """Сам список не должен опустеть незаметно: пустой параметризованный
    тест «проходит» и не проверяет ничего."""
    assert len(DOCS) >= 5, f"в корне подозрительно мало документов: {[p.name for p in DOCS]}"


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
class TestEachDocIsADocument:
    def test_starts_with_heading(self, path):
        assert _read(path).lstrip().startswith("#"), f"{path.name} начинается не с заголовка"

    def test_is_not_source_code(self, path):
        """Ровно так выглядела порча: в документ попадал питоновский файл.

        Примеры кода внутри ```-блоков -- нормальная часть документации,
        поэтому считаем только строки вне блоков."""
        offenders = [
            line for line in _lines_outside_code_blocks(_read(path))
            if line.startswith(('"""', "import ", "from app", "def ", "class ", "async def "))
        ]
        assert not offenders, f"{path.name}: похоже на исходник — {offenders[:3]}"

    def test_is_not_a_config_file(self, path):
        """Procfile и .env.example тоже уже подменяли собой документы."""
        first = _read(path).lstrip().splitlines()[0]
        assert not first.startswith("web:"), f"{path.name} начинается со строки Procfile"
        assert "=" not in first or first.startswith("#"), f"{path.name} похож на .env"

    def test_is_not_empty(self, path):
        assert len(_read(path).strip()) > 200, f"{path.name} подозрительно пуст"


class TestNoDecoys:
    def test_no_two_docs_have_the_same_content(self):
        """Файл-обманка — это документ, внутри которого лежит чужой текст.
        Полное совпадение содержимого ловит самый грубый случай."""
        seen: dict[str, str] = {}
        for path in DOCS:
            body = _read(path).strip()
            assert body not in seen, f"{path.name} дословно повторяет {seen.get(body)}"
            seen[body] = path.name

    def test_no_doc_copies_a_config(self):
        for name in ("Procfile", ".env.example", "requirements.txt"):
            config = REPO / name
            if not config.exists():
                continue
            body = config.read_text(encoding="utf-8").strip()
            for path in DOCS:
                assert _read(path).strip() != body, f"{path.name} — это копия {name}"

    def test_decoy_specs_are_gone(self):
        """Две спеки, от которых осталась только обманка, удалены осознанно:
        содержимое восстанавливать было неоткуда, а файл с чужим текстом
        внутри хуже, чем его отсутствие."""
        for name in ("SPEC_TRUEPOST_QUEUE_OFFER.md", "SPEC_TRUEPOST_GENERATOR_STYLE.md"):
            assert not (REPO / name).exists(), f"{name} снова появился в корне"


class TestProjectStatePointsAtTheTruth:
    def test_state_is_marked_as_a_snapshot(self):
        """Старый снимок состояния легко принять за текущую правду --
        поэтому он обязан сам сказать, что он снимок."""
        text = _read(REPO / "PROJECT_STATE_GROWTHAGENT.md")
        assert "снимок" in text.lower()
        assert "PRODUCT_ROADMAP.md" in text and "CLAUDE.md" in text

    def test_restored_content_is_there(self):
        """Ради этого текста файл и восстанавливали: в нём разбор прод-
        инцидентов, которого больше нигде нет."""
        text = _read(REPO / "PROJECT_STATE_GROWTHAGENT.md")
        assert "DIRECT_TSV_MAX_ROWS" in text, "потерян разбор OOM на Railway"
        assert "growth_loop" in text
