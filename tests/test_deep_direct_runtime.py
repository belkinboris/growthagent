from app.config import TELEGRAM_MESSAGE_CHUNK_SIZE
from app.telegram_bot import _split_telegram_text


def test_deep_direct_long_message_is_split_under_telegram_limit():
    text = ("строка\n" * 1200).strip()

    chunks = _split_telegram_text(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= TELEGRAM_MESSAGE_CHUNK_SIZE for chunk in chunks)
    assert "".join(chunk.replace("\n", "") for chunk in chunks) == text.replace("\n", "")


def test_deep_direct_short_message_is_not_split():
    text = "короткая диагностика"

    assert _split_telegram_text(text) == [text]
