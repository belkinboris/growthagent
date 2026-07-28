"""
Кому уходят уведомления по проекту (задача B8).

Раньше адресат был один на всю платформу: `BOT_ADMIN_CHAT_IDS` из
окружения. Пока владелец был один, это работало. С аккаунтами это уже
дефект: сводка по проекту клиента ушла бы владельцу платформы — то есть
данные клиента показали бы постороннему.

Правило:

1. У проекта есть свой список получателей (`notify_chat_ids` в настройках) —
   шлём туда. Это главный путь для клиента.
2. Списка нет — используем `BOT_ADMIN_CHAT_IDS`, но ТОЛЬКО если проект
   принадлежит тому, кто ставил платформу (первый заведённый аккаунт),
   либо не принадлежит никому. Переменная окружения — это канал того, у
   кого есть доступ к серверу; считать её каналом любого клиента нельзя.
3. Иначе получателей нет. Мы не шлём никуда и говорим об этом словами —
   молча отправить чужому хуже, чем не отправить.
"""

from __future__ import annotations

from typing import Optional

from sqlmodel import Session, select

from app.models import PlatformUser, Project, ProjectMember

NO_RECIPIENTS_HINT = (
    "Уведомления по этому проекту никуда не уходят: не указан Telegram chat id. "
    "Добавьте его в настройках проекта."
)


def project_chat_ids(session: Session, project: Project, settings) -> tuple[list[str], Optional[str]]:
    """Возвращает (получатели, причина пустоты). Причина -- текст для владельца."""
    own = [str(c).strip() for c in ((project.settings_json or {}).get("notify_chat_ids") or []) if str(c).strip()]
    if own:
        return own, None

    env_ids = [str(c).strip() for c in (getattr(settings, "admin_chat_ids_list", None) or []) if str(c).strip()]
    if env_ids and _belongs_to_installer(session, project.id):
        return env_ids, None

    if not env_ids and _belongs_to_installer(session, project.id):
        return [], (
            "Уведомления не настроены: нет ни BOT_ADMIN_CHAT_IDS в окружении, "
            "ни Telegram chat id у проекта."
        )
    return [], NO_RECIPIENTS_HINT


def _belongs_to_installer(session: Session, project_id: int) -> bool:
    """Проект ничей или принадлежит тому, кто ставил платформу.

    «Тот, кто ставил» — первый заведённый аккаунт: именно у него есть доступ
    к переменным окружения сервера. Для всех остальных аккаунтов чужой
    канал из окружения адресатом быть не может.
    """
    owner_ids = {
        int(r) for r in session.exec(
            select(ProjectMember.user_id).where(ProjectMember.project_id == project_id)
        ).all()
    }
    if not owner_ids:
        return True

    installer = session.exec(select(PlatformUser.id).order_by(PlatformUser.id)).first()
    return installer is not None and int(installer) in owner_ids
