"""
Аккаунты платформы (B1, часть 1): пароли, регистрация, владение проектом,
личность внутри сессионного токена.

Часть 1 не включает изоляцию чтения по владельцу — она следующая. Здесь
проверяется фундамент: пароль нельзя восстановить из хэша, почта уникальна
независимо от регистра, личность в токене нельзя подменить, а старый токен
владельца продолжает работать.
"""

import time

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app import accounts
from app.models import PlatformUser, Project, ProjectMember
from app.platform_auth import Identity, issue_session_token, resolve_session_token


@pytest.fixture(autouse=True)
def _clean_settings_cache():
    """Тесты подменяют PLATFORM_SECRET_KEY через окружение. Кэш настроек
    надо сбросить и ПОСЛЕ теста тоже, иначе тестовый секрет утечёт
    в соседние файлы прогона."""
    _reset_settings()
    yield
    _reset_settings()


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _project(session, name="АвтоПост") -> Project:
    p = Project(name=name, type="telegram_saas", is_active=True)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


# ---------------------------------------------------------------------------
# Пароли
# ---------------------------------------------------------------------------


class TestPasswords:
    def test_hash_does_not_contain_password(self):
        stored = accounts.hash_password("очень секретный пароль")
        assert "очень секретный пароль" not in stored
        assert stored.startswith("pbkdf2_sha256$")

    def test_same_password_hashes_differently(self):
        """Соль на каждый пароль своя: одинаковые пароли не должны
        выглядеть одинаково в базе."""
        assert accounts.hash_password("qwerty12") != accounts.hash_password("qwerty12")

    def test_verify_roundtrip(self):
        stored = accounts.hash_password("qwerty12")
        assert accounts.verify_password("qwerty12", stored) is True
        assert accounts.verify_password("qwerty13", stored) is False

    def test_cyrillic_password_works(self):
        """Тот же класс дефекта, что уронил вход владельца в 500."""
        stored = accounts.hash_password("парольпароль")
        assert accounts.verify_password("парольпароль", stored) is True
        assert accounts.verify_password("парольпаролъ", stored) is False

    @pytest.mark.parametrize("broken", [None, "", "мусор", "pbkdf2_sha256$нет$соли$хэш", "md5$1$aa$bb"])
    def test_broken_hash_is_refusal_not_crash(self, broken):
        assert accounts.verify_password("qwerty12", broken) is False


# ---------------------------------------------------------------------------
# Регистрация
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_create_and_authenticate(self, session):
        user = accounts.create_user(session, "Ivan@Example.COM", "qwerty12")
        assert user.email == "ivan@example.com"  # почта нормализована
        assert accounts.authenticate(session, "IVAN@example.com", "qwerty12").id == user.id

    def test_wrong_password_is_none(self, session):
        accounts.create_user(session, "ivan@example.com", "qwerty12")
        assert accounts.authenticate(session, "ivan@example.com", "qwerty13") is None

    def test_unknown_email_is_none(self, session):
        assert accounts.authenticate(session, "нет@такого.рф", "qwerty12") is None

    def test_email_taken_is_typed_error(self, session):
        accounts.create_user(session, "ivan@example.com", "qwerty12")
        with pytest.raises(accounts.EmailTaken):
            accounts.create_user(session, "IVAN@example.com", "другой123")

    @pytest.mark.parametrize("email", ["", "  ", "не почта"])
    def test_bad_email_rejected(self, session, email):
        with pytest.raises(ValueError):
            accounts.create_user(session, email, "qwerty12")

    def test_short_password_rejected(self, session):
        with pytest.raises(ValueError):
            accounts.create_user(session, "ivan@example.com", "1234")

    def test_inactive_user_cannot_log_in(self, session):
        user = accounts.create_user(session, "ivan@example.com", "qwerty12")
        user.is_active = False
        session.add(user)
        session.commit()
        assert accounts.authenticate(session, "ivan@example.com", "qwerty12") is None

    def test_last_login_recorded(self, session):
        user = accounts.create_user(session, "ivan@example.com", "qwerty12")
        assert user.last_login_at is None
        accounts.authenticate(session, "ivan@example.com", "qwerty12")
        assert session.get(PlatformUser, user.id).last_login_at is not None


# ---------------------------------------------------------------------------
# Владение проектами
# ---------------------------------------------------------------------------


class TestProjectAccess:
    def test_grant_is_idempotent(self, session):
        user = accounts.create_user(session, "ivan@example.com", "qwerty12")
        project = _project(session)
        accounts.grant_project(session, project.id, user.id)
        accounts.grant_project(session, project.id, user.id)
        assert len(session.exec(select(ProjectMember)).all()) == 1

    def test_access_check(self, session):
        ivan = accounts.create_user(session, "ivan@example.com", "qwerty12")
        petr = accounts.create_user(session, "petr@example.com", "qwerty12")
        project = _project(session)
        accounts.grant_project(session, project.id, ivan.id)

        assert accounts.user_can_access(session, ivan.id, project.id) is True
        assert accounts.user_can_access(session, petr.id, project.id) is False
        assert accounts.user_project_ids(session, ivan.id) == [project.id]
        assert accounts.user_project_ids(session, petr.id) == []

    def test_revoke(self, session):
        user = accounts.create_user(session, "ivan@example.com", "qwerty12")
        project = _project(session)
        accounts.grant_project(session, project.id, user.id)
        accounts.revoke_project(session, project.id, user.id)
        assert accounts.user_can_access(session, user.id, project.id) is False

    def test_first_user_adopts_orphan_projects(self, session):
        """Проекты, заведённые до аккаунтов (наш живой АвтоПост), не должны
        остаться без владельца и пропасть из интерфейса."""
        _project(session, "АвтоПост")
        _project(session, "Второй")
        user = accounts.create_user(session, "ivan@example.com", "qwerty12")

        assert accounts.adopt_orphan_projects(session, user.id) == 2
        assert len(accounts.user_project_ids(session, user.id)) == 2

    def test_second_user_adopts_nothing(self, session):
        """Усыновление -- одноразовое. Иначе второй зарегистрировавшийся
        клиент получил бы чужой проект."""
        _project(session)
        ivan = accounts.create_user(session, "ivan@example.com", "qwerty12")
        accounts.adopt_orphan_projects(session, ivan.id)

        petr = accounts.create_user(session, "petr@example.com", "qwerty12")
        assert accounts.adopt_orphan_projects(session, petr.id) == 0
        assert accounts.user_project_ids(session, petr.id) == []


# ---------------------------------------------------------------------------
# Личность внутри сессионного токена
# ---------------------------------------------------------------------------


class TestSessionIdentity:
    def test_user_token_carries_id(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_SECRET_KEY", "секрет-для-теста")
        _reset_settings()
        token = issue_session_token(user_id=42)
        assert resolve_session_token(token) == Identity(user_id=42, is_owner=False)

    def test_owner_token_has_no_user(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_SECRET_KEY", "секрет-для-теста")
        _reset_settings()
        identity = resolve_session_token(issue_session_token())
        assert identity.user_id is None and identity.is_owner is True
        assert identity.is_env_owner is True

    def test_identity_cannot_be_forged(self, monkeypatch):
        """Личность подписана вместе со сроком: подменить id, оставив
        подпись, нельзя -- иначе любой клиент стал бы владельцем."""
        monkeypatch.setenv("PLATFORM_SECRET_KEY", "секрет-для-теста")
        _reset_settings()
        payload, signature = issue_session_token(user_id=42).rsplit(".", 1)
        expires = payload.split(":")[0]
        assert resolve_session_token(f"{expires}:u1.{signature}") is None
        assert resolve_session_token(f"{expires}.{signature}") is None

    def test_expired_token_rejected(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_SECRET_KEY", "секрет-для-теста")
        monkeypatch.setenv("PLATFORM_SESSION_TTL_HOURS", "0")
        _reset_settings()
        token = issue_session_token(user_id=42)
        time.sleep(0.01)
        assert resolve_session_token(token) is None

    @pytest.mark.parametrize("token", [None, "", "мусор", "123", "123.", "abc.def"])
    def test_garbage_rejected(self, token, monkeypatch):
        monkeypatch.setenv("PLATFORM_SECRET_KEY", "секрет-для-теста")
        _reset_settings()
        assert resolve_session_token(token) is None


def _reset_settings():
    from app.config import get_settings

    get_settings.cache_clear()
