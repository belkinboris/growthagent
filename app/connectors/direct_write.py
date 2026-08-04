"""
Запись в Яндекс.Директ через API v5 (задача R4, уровни автономии F6).

Не путать с `app/connectors/direct.py` -- тот строго read-only и работает
с одним-единственным сервисом отчётов (Reports Service). Здесь -- сервисы
управления (`adgroups`, `campaigns`), то есть первое место в проекте, где
платформа МЕНЯЕТ чужой рекламный кабинет. Разделено намеренно: принцип
«аналитик не трогает рекламу» отменяется только осознанным включением
уровня автономии 3 за конкретный проект, и код, который умеет писать,
не должен случайно оказаться на пути чтения.

Три вещи, которыми управляющие сервисы отличаются от Reports и на которых
легко ошибиться:

1. **Ошибка приходит с HTTP 200.** Директ отвечает `{"error": {...}}` в
   теле при совершенно нормальном коде ответа. Проверять `status_code`
   недостаточно -- `direct.py:270` делает именно так, и для отчётов этого
   хватает, а здесь молча проглотило бы отказ.
2. **Пакет применяется частично.** В `result.UpdateResults[]` у каждого
   объекта свои `Errors`/`Warnings`: «применили 3 из 5» -- штатный ответ,
   и владельцу нужно показать именно это, а не «готово».
3. **Минус-фразы перезаписываются целиком.** `adgroups.update` с полем
   `NegativeKeywords` затирает прежний список. Поэтому здесь только
   read-modify-write: сначала `adgroups.get`, потом объединение, потом
   запись. Иначе первое же автоматическое действие стёрло бы всё, что
   владелец накопил руками.

Токен: `DIRECT_WRITE_OAUTH_TOKEN` в окружении, иначе -- общий
`effective_direct_oauth_token`. Отдельная переменная нужна на случай,
когда владелец хочет выдать право записи отдельным токеном; в отличие от
Метрики, у Директа нет отдельного scope на запись, поэтому по умолчанию
работает тот же токен, которым уже читаются отчёты.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger("growth_agent.connectors.direct_write")

API_URL = "https://api.direct.yandex.com/json/v5"
SANDBOX_API_URL = "https://api-sandbox.direct.yandex.com/json/v5"

# Директ ограничивает длину минус-фразы; чересчур длинные -- почти всегда
# мусорный «запрос целиком», который лучше не записывать вовсе.
MAX_NEGATIVE_PHRASE_LEN = 4096
MAX_NEGATIVE_KEYWORDS_PER_GROUP = 1000


class DirectWriteError(RuntimeError):
    pass


class DirectWriteResult:
    """
    Итог одной операции записи.

    `applied` -- что реально применилось, `skipped` -- что Директ отклонил
    (с причиной). Оба списка нужны: «применили 3 из 5» -- нормальный ответ
    API, и показывать его как успех было бы враньём.
    """

    def __init__(self) -> None:
        self.applied: list[str] = []
        self.skipped: list[tuple[str, str]] = []   # (что, почему)
        self.warnings: list[str] = []

    @property
    def ok(self) -> bool:
        return bool(self.applied) and not self.skipped

    def as_dict(self) -> dict:
        return {
            "applied": self.applied,
            "skipped": [{"item": i, "reason": r} for i, r in self.skipped],
            "warnings": self.warnings,
        }


def _write_token(settings) -> Optional[str]:
    return (getattr(settings, "direct_write_oauth_token", None)
            or settings.effective_direct_oauth_token)


def is_configured(settings) -> bool:
    return bool(_write_token(settings))


def _headers(settings) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {_write_token(settings)}",
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }
    if settings.direct_client_login:
        headers["Client-Login"] = settings.direct_client_login
    return headers


def _base_url(settings) -> str:
    return SANDBOX_API_URL if settings.direct_sandbox else API_URL


async def _call(settings, service: str, method: str, params: dict,
                timeout_seconds: float = 30.0) -> dict:
    """Один вызов управляющего сервиса Директа с честным разбором ошибок."""
    if not is_configured(settings):
        raise DirectWriteError(
            "Запись в Директ не настроена: нужен OAuth-токен с доступом к API "
            "Яндекс.Директа (DIRECT_WRITE_OAUTH_TOKEN или YANDEX_OAUTH_TOKEN)."
        )
    url = f"{_base_url(settings)}/{service}"
    payload = {"method": method, "params": params}
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        try:
            resp = await client.post(url, headers=_headers(settings), json=payload)
        except httpx.HTTPError as exc:
            raise DirectWriteError(f"Не удалось обратиться к Директу: {exc}") from exc

    if resp.status_code >= 400:
        raise DirectWriteError(f"Директ ответил HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        body = resp.json()
    except ValueError as exc:
        raise DirectWriteError("Директ вернул не-JSON в ответ на управляющий запрос") from exc

    # Главная ловушка: ошибка приходит С КОДОМ 200. Проверка status_code
    # (как в read-коннекторе) её не поймает.
    if "error" in body:
        err = body["error"] or {}
        raise DirectWriteError(
            f"Ошибка Директа {err.get('error_code', '?')}: "
            f"{err.get('error_string') or err.get('error_detail') or 'без описания'}"
        )
    return body.get("result", {})


def _collect_object_errors(items: list[dict], label_of) -> DirectWriteResult:
    """
    Разбирает `*Results[]`: у каждого объекта свои Errors/Warnings, и
    частичное применение -- штатный ответ Директа, а не исключение.
    """
    out = DirectWriteResult()
    for i, item in enumerate(items or []):
        label = label_of(i, item)
        errors = item.get("Errors") or []
        if errors:
            reason = "; ".join(
                e.get("Message") or e.get("Details") or str(e.get("Code", "")) for e in errors
            )
            out.skipped.append((label, reason))
            continue
        for w in item.get("Warnings") or []:
            out.warnings.append(f"{label}: {w.get('Message') or w.get('Details') or ''}")
        out.applied.append(label)
    return out


async def get_ad_group_negative_keywords(settings, ad_group_id: str) -> list[str]:
    """Текущие минус-фразы группы. Нужны ДО записи: update затирает список
    целиком, и без чтения первое же автодействие стёрло бы то, что владелец
    добавлял руками."""
    result = await _call(settings, "adgroups", "get", {
        "SelectionCriteria": {"Ids": [int(ad_group_id)]},
        "FieldNames": ["Id", "NegativeKeywords"],
    })
    groups = result.get("AdGroups") or []
    if not groups:
        raise DirectWriteError(f"Группа объявлений {ad_group_id} не найдена в Директе")
    negative = groups[0].get("NegativeKeywords") or {}
    return list(negative.get("Items") or [])


def _clean_phrases(phrases: list[str]) -> list[str]:
    seen, out = set(), []
    for p in phrases:
        p = (p or "").strip().lower()
        if not p or len(p) > MAX_NEGATIVE_PHRASE_LEN or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


async def add_negative_keywords(settings, ad_group_id: str, phrases: list[str]) -> DirectWriteResult:
    """
    Добавляет минус-фразы в группу объявлений, сохраняя уже существующие.

    Возвращает DirectWriteResult; если добавлять нечего (все фразы уже
    стоят), результат пустой и `ok` == False -- вызывающий код должен
    отличать «ничего не потребовалось» от «применили».
    """
    fresh = _clean_phrases(phrases)
    if not fresh:
        raise DirectWriteError("Нечего добавлять: список минус-фраз пуст после очистки")

    existing = await get_ad_group_negative_keywords(settings, ad_group_id)
    existing_lower = {e.strip().lower() for e in existing}
    to_add = [p for p in fresh if p not in existing_lower]

    out = DirectWriteResult()
    if not to_add:
        out.warnings.append("Все предложенные фразы уже стоят в минус-словах группы")
        return out

    merged = list(existing) + to_add
    if len(merged) > MAX_NEGATIVE_KEYWORDS_PER_GROUP:
        # Обрезаем НОВЫЕ, а не старые: то, что владелец добавил раньше,
        # трогать нельзя -- это его решения.
        allowed = MAX_NEGATIVE_KEYWORDS_PER_GROUP - len(existing)
        for p in to_add[max(0, allowed):]:
            out.skipped.append((p, f"в группе уже {len(existing)} минус-фраз, лимит Директа исчерпан"))
        to_add = to_add[:max(0, allowed)]
        merged = list(existing) + to_add
        if not to_add:
            return out

    result = await _call(settings, "adgroups", "update", {
        "AdGroups": [{"Id": int(ad_group_id), "NegativeKeywords": {"Items": merged}}],
    })
    api_out = _collect_object_errors(
        result.get("UpdateResults") or [],
        lambda i, item: f"группа {ad_group_id}",
    )
    if api_out.skipped:
        out.skipped.extend(api_out.skipped)
        return out
    out.applied.extend(to_add)
    out.warnings.extend(api_out.warnings)
    return out
