"""
Ошибки внешних источников человеческим языком (задача C4).

Коннекторы записывают в `Integration.last_error` то, что удобно
разработчику: «HTTP error calling Direct Reports API: ConnectTimeout»,
«HTTP 401: ...». Владелец видел эту строку в интерфейсе как есть —
английскую, техническую и ничего ему не говорящую. Хуже того: по ней
непонятно главное — надо что-то делать или само пройдёт.

Здесь техническая строка превращается в два предложения: что случилось и
что с этим делать. Исходную строку не выбрасываем — она остаётся рядом
(в подсказке), потому что прятать факты от владельца нельзя: если он
понесёт вопрос в поддержку хостинга, нужен будет именно исходный текст.

Ничего не угадываем: незнакомая ошибка так и называется незнакомой, а
исходный текст показывается целиком. Придумать красивое объяснение
непонятной ошибке — то же враньё, что выдуманное число.
"""

from __future__ import annotations

import re
from typing import Optional

# Человеческие названия источников. Ключи -- значения IntegrationType.
SOURCE_NAMES = {
    "project_metrics_api": "Продукт",
    "direct": "Яндекс.Директ",
    "metrika": "Яндекс.Метрика",
    "yookassa": "ЮKassa",
    "telegram": "Telegram",
    "llm": "Ответы аналитика",
}

# Что делать -- зависит от источника: у продукта это адрес и токен в мастере,
# у Яндекса -- OAuth-токен в переменных окружения сервера.
_WHERE_TO_FIX = {
    "project_metrics_api": "Проверьте адрес продукта и токен на вкладке «Проекты» — кнопка «перепроверить».",
    "direct": "Проверьте OAuth-токен Директа и логин клиента в переменных окружения аналитика.",
    "metrika": "Проверьте OAuth-токен Метрики и номер счётчика.",
    "yookassa": "Проверьте ключ ЮKassa в переменных окружения аналитика.",
}


def source_name(source: Optional[str]) -> str:
    return SOURCE_NAMES.get((source or "").strip(), (source or "источник").strip())


def humanize_error(raw: Optional[str], source: Optional[str] = None) -> dict:
    """Возвращает {"text", "short", "action", "raw"}.

    text -- что случилось, одним предложением.
    short -- то же без названия источника: рядом с плашкой «Яндекс.Директ»
             повторять «Яндекс.Директ» дважды незачем.
    action -- что делать; пустая строка, если делать нечего (само пройдёт).
    raw -- исходная строка коннектора, как есть.
    """
    raw_text = (raw or "").strip()
    name = source_name(source)
    fix = _WHERE_TO_FIX.get((source or "").strip(), "")

    def out(text: str, action: str) -> dict:
        return {"text": text, "short": _short(text, name), "action": action, "raw": raw_text}

    if not raw_text:
        return out(f"{name} не ответил, причина не записана.", fix)

    low = raw_text.lower()
    status = _status_code(raw_text)

    # Порядок важен: сначала то, что требует действия владельца, потом то,
    # что проходит само. Иначе таймаут перекроет собой неверный токен.
    if status in (401, 403) or "unauthorized" in low or "forbidden" in low:
        return out(
            f"{name} не принял доступ: токен неверный или истёк.",
            fix or "Обновите доступ к источнику.",
        )
    if status == 404 or "not found" in low:
        return out(
            f"{name} отвечает, но нужного адреса у него нет — endpoint не поднят или называется иначе.",
            fix or "Сверьтесь с CONTRACT.md: какой адрес ожидает аналитик.",
        )
    if status == 429 or "too many requests" in low or "quota" in low or "limit" in low:
        return out(
            f"{name} временно ограничил частоту запросов.",
            "Ничего делать не нужно: аналитик повторит на следующем цикле.",
        )
    if "timeout" in low or "timed out" in low:
        return out(
            f"{name} не ответил вовремя.",
            "Обычно это временно — аналитик повторит на следующем цикле. "
            "Если повторяется несколько часов, источник действительно недоступен.",
        )
    if any(w in low for w in ("connect", "network", "dns", "resolve", "refused", "unreachable")):
        return out(
            f"Не удалось соединиться: {name} недоступен по сети.",
            fix or "Проверьте, что источник работает и открыт для запросов извне.",
        )
    if "ssl" in low or "certificate" in low:
        return out(
            f"{name} отвечает, но сертификат его сайта не проходит проверку.",
            fix or "Проверьте сертификат домена источника.",
        )
    if "json" in low and ("invalid" in low or "decode" in low or "expecting" in low):
        return out(
            f"{name} ответил, но не тем, что ожидалось: вместо данных пришло не-JSON.",
            fix or "Сверьтесь с CONTRACT.md: какой ответ ожидает аналитик.",
        )
    if "as_of" in low:
        return out(
            f"{name} прислал данные без отметки времени (as_of) — аналитик не может "
                    f"отличить свежие данные от залипших и отклонил ответ.",
            "Добавьте поле as_of в ответ — см. CONTRACT.md.",
        )
    if status is not None and 500 <= status < 600:
        return out(
            f"{name} ответил ошибкой на своей стороне (код {status}).",
            "Аналитик повторит на следующем цикле. Если повторяется — смотреть логи источника.",
        )

    # Незнакомая ошибка. Придумывать объяснение нельзя: показываем как есть
    # и честно говорим, что аналитик её не узнал.
    return out(
        f"{name} вернул ошибку, которую аналитик не узнал.",
        fix or "Текст ошибки ниже — с ним можно идти к разработчику источника.",
    )


def _short(text: str, name: str) -> str:
    """Та же фраза без названия источника в начале."""
    if text.startswith(name + " "):
        rest = text[len(name) + 1:]
        return rest[0].lower() + rest[1:] if rest else text
    return text


def _status_code(raw: str) -> Optional[int]:
    """Код ответа из строк вида «HTTP 401: ...» или «HTTP 502»."""
    match = re.search(r"\bHTTP\s+(\d{3})\b", raw, re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"\bstatus[ _]?code[=: ]+(\d{3})\b", raw, re.IGNORECASE)
    return int(match.group(1)) if match else None
