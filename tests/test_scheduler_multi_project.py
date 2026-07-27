"""
Планировщик обходит ВСЕ включённые проекты (задача B7).

До этого цикл брал `.first()` активный проект: платформа собирала данные
ровно по одному проекту на всю базу. Пока владелец был один, это работало;
со вторым аккаунтом включение своего проекта останавливало сбор у соседа,
и платформе приходилось отказывать в активации (409).

Здесь закреплено поведение обхода: очередь по id, свой таймаут на проект,
падение одного не роняет остальных.
"""

import asyncio

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app import scheduler
from app.models import Project


@pytest.fixture()
def session_factory(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    factory = lambda: Session(engine)  # noqa: E731
    monkeypatch.setattr(scheduler, "get_session", factory)
    return factory


def _project(session, name, active=True) -> int:
    p = Project(name=name, type="telegram_saas", is_active=active)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p.id


class TestActiveProjectIds:
    def test_only_active_and_sorted(self, session_factory):
        with session_factory() as session:
            second = _project(session, "Второй")
            first = _project(session, "Первый")
            _project(session, "Выключенный", active=False)
            # id выдаются по порядку создания, а ожидаем отсортированный список
            assert scheduler.active_project_ids(session) == sorted([second, first])

    def test_empty_when_nothing_enabled(self, session_factory):
        with session_factory() as session:
            _project(session, "Выключенный", active=False)
            assert scheduler.active_project_ids(session) == []


class TestRunCycleForAllActive:
    def test_visits_every_enabled_project(self, session_factory, monkeypatch):
        with session_factory() as session:
            a = _project(session, "А")
            b = _project(session, "Б")
            _project(session, "В", active=False)

        visited = []

        async def fake_cycle(project_id=None):
            visited.append(project_id)
            return _Result()

        monkeypatch.setattr(scheduler, "run_cycle_once", fake_cycle)
        outcomes = asyncio.run(scheduler.run_cycle_for_all_active())

        assert visited == [a, b], "выключенный проект собирать нельзя"
        assert outcomes == {a: "ok", b: "ok"}

    def test_one_broken_project_does_not_stop_others(self, session_factory, monkeypatch):
        """У каждого клиента свой продукт: чужая поломка не повод оставить
        всех остальных без данных."""
        with session_factory() as session:
            a = _project(session, "Сломанный")
            b = _project(session, "Живой")

        visited = []

        async def fake_cycle(project_id=None):
            visited.append(project_id)
            if project_id == a:
                raise RuntimeError("продукт клиента упал")
            return _Result()

        monkeypatch.setattr(scheduler, "run_cycle_once", fake_cycle)
        outcomes = asyncio.run(scheduler.run_cycle_for_all_active())

        assert visited == [a, b]
        assert outcomes == {a: "error", b: "ok"}

    def test_slow_project_times_out_alone(self, session_factory, monkeypatch):
        """Свой таймаут на каждый проект: медленный чужой продукт не должен
        съедать окно у остальных."""
        with session_factory() as session:
            slow = _project(session, "Медленный")
            fast = _project(session, "Быстрый")

        monkeypatch.setattr(scheduler, "RUN_CYCLE_TIMEOUT_SECONDS", 0.05)

        async def fake_cycle(project_id=None):
            if project_id == slow:
                await asyncio.sleep(5)
            return _Result()

        monkeypatch.setattr(scheduler, "run_cycle_once", fake_cycle)
        outcomes = asyncio.run(scheduler.run_cycle_for_all_active())

        assert outcomes == {slow: "timeout", fast: "ok"}

    def test_sequential_not_parallel(self, session_factory, monkeypatch):
        """Обход по очереди -- сознательно: цикл ходит во внешние API и
        держит выгрузки в памяти, параллельный обход уже приводил к OOM."""
        with session_factory() as session:
            a = _project(session, "А")
            b = _project(session, "Б")

        running = 0
        max_running = 0

        async def fake_cycle(project_id=None):
            nonlocal running, max_running
            running += 1
            max_running = max(max_running, running)
            await asyncio.sleep(0.01)
            running -= 1
            return _Result()

        monkeypatch.setattr(scheduler, "run_cycle_once", fake_cycle)
        asyncio.run(scheduler.run_cycle_for_all_active())
        assert max_running == 1

    def test_nothing_enabled_is_not_an_error(self, session_factory, monkeypatch):
        async def fake_cycle(project_id=None):
            raise AssertionError("не должно вызываться")

        monkeypatch.setattr(scheduler, "run_cycle_once", fake_cycle)
        assert asyncio.run(scheduler.run_cycle_for_all_active()) == {}


class _Result:
    has_notifiable_changes = False
    primary_candidate = None
