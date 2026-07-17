"""
Подключение к БД и инициализация.

init_db() при старте сервиса:
1. создаёт таблицы, если их нет;
2. создаёт или обновляет ОДИН Project из настроек .env -- это и есть
   "текущий подключённый проект" в v1. Сама модель Project не ограничивает
   их количество, но практика v1 -- один активный проект.
"""

from sqlmodel import SQLModel, Session, create_engine, select

from app.config import get_settings, DEFAULT_METRIKA_GOAL_MAPPING
from app.models import Project, Integration, IntegrationType, IntegrationStatus


settings = get_settings()

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

# Пул с жёсткими границами. Без них SQLAlchemy держал соединения без предела:
# при 8 циклах в сутки + вебхуках Telegram память росла до Out of memory
# (7 падений за 3 дня, 2026-07-09..12). pool_recycle -- Railway рвёт idle-
# соединения молча, pre_ping ловит мёртвые до запроса.
_engine_kwargs: dict = {"echo": False, "connect_args": connect_args}
if not settings.database_url.startswith("sqlite"):
    _engine_kwargs.update(
        pool_size=5,
        max_overflow=2,
        pool_recycle=280,
        pool_pre_ping=True,
        pool_timeout=30,
    )
engine = create_engine(settings.database_url, **_engine_kwargs)


def get_session() -> Session:
    return Session(engine)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _ensure_default_project()


def _ensure_default_project() -> None:
    """
    Создаёт Project из .env, если его ещё нет, либо обновляет базовые поля
    у существующего. Mapping (funnel_mapping) при первом создании берётся
    дефолтным -- АвтоПост-специфичный mapping (signup=users_created и т.д.)
    живёт в connectors/truepost.py как DEFAULT_FUNNEL_MAPPING, не здесь.
    """
    from app.connectors.truepost import DEFAULT_FUNNEL_MAPPING

    with get_session() as session:
        existing = session.exec(
            select(Project).where(Project.name == settings.project_name)
        ).first()

        if existing:
            existing.base_url = settings.project_base_url
            existing.connector_name = settings.project_connector
            session.add(existing)
            session.commit()
            project = existing
        else:
            project = Project(
                name=settings.project_name,
                type=settings.project_type,
                base_url=settings.project_base_url,
                connector_name=settings.project_connector,
                settings_json={
                    "funnel_mapping": DEFAULT_FUNNEL_MAPPING,
                    "metrika_goal_mapping": DEFAULT_METRIKA_GOAL_MAPPING,
                },
            )
            session.add(project)
            session.commit()
            session.refresh(project)

        _ensure_integrations(session, project)


def _ensure_integrations(session: Session, project: Project) -> None:
    needed = [
        IntegrationType.project_metrics_api,
        IntegrationType.metrika,
        IntegrationType.direct,
        IntegrationType.yookassa,
        IntegrationType.telegram,
        IntegrationType.llm,
    ]
    existing_types = set(
        session.exec(
            select(Integration.type).where(Integration.project_id == project.id)
        ).all()
    )
    for itype in needed:
        if itype not in existing_types:
            session.add(
                Integration(
                    project_id=project.id,
                    type=itype,
                    status=IntegrationStatus.not_configured,
                )
            )
    session.commit()
