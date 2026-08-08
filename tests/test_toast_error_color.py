"""
Тосты об ошибке должны быть красными (задача из почасовой рутины, 08.08.2026).

`toast(text, kind)` красит сообщение по `kind`: без него тост серый и
неотличим от нейтрального/успешного на глаз (см. комментарий у самой
функции в index.html — раньше "Сохранено" и "Не удалось сохранить"
выглядели одинаково). У всех обработчиков ошибок в файле toast вызывается
с `'error'`, кроме `actOnProblem` (кнопки "Сделаю"/"Не буду" и
принять/отклонить на доске, F2) — там `kind` был потерян, и неудачное
действие по карточке показывало бесцветный тост.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HTML = (REPO / "app" / "static" / "platform" / "index.html").read_text(encoding="utf-8")

# Каждый catch-блок, показывающий владельцу e.message, обязан покрасить
# тост в 'error' -- иначе ошибка выглядит как нейтральное сообщение.
TOAST_ERROR_CALLS = re.findall(r"toast\(e\.message[^)]*\)", HTML)


def test_every_error_toast_call_exists():
    # Если разметка вокруг toast(e.message...) в файле поменяется настолько,
    # что регэксп перестанет находить вызовы, тест должен упасть заметно,
    # а не молча "пройти" по нулю найденных мест.
    assert len(TOAST_ERROR_CALLS) >= 8


def test_every_error_toast_is_colored_red():
    uncolored = [call for call in TOAST_ERROR_CALLS if "'error'" not in call]
    assert not uncolored, (
        "toast(e.message) без 'error' показывает ошибку бесцветным тостом, "
        f"неотличимым от успеха: {uncolored}"
    )
