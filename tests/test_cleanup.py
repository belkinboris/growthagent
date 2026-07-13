"""Ретенция данных: лечение Out of memory (июль 2026)."""
import asyncio
from datetime import timedelta

from sqlmodel import Session, SQLModel, create_engine, select

from app.models import (AgentRun, MetricSnapshot, NotificationLog, Project)
from app.service import RETENTION_DAYS, cleanup_old_data, utcnow


def _factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return lambda: Session(engine)


class TestCleanup:

    def test_deletes_old_keeps_fresh(self):
        f = _factory()
        with f() as s:
            p = Project(name="TruePost", type="t", is_active=True)
            s.add(p); s.commit(); s.refresh(p)
            now = utcnow()
            old = now - timedelta(days=RETENTION_DAYS["MetricSnapshot"] + 5)
            fresh = now - timedelta(days=1)
            for created in (old, old, fresh):
                s.add(MetricSnapshot(project_id=p.id, period_key="7d", source="combined",
                                     period_start=created, period_end=created,
                                     metrics_json={}, created_at=created))
            s.add(AgentRun(project_id=p.id, created_at=old))
            s.add(AgentRun(project_id=p.id, created_at=fresh))
            s.commit()

            deleted = cleanup_old_data(s)
            assert deleted["MetricSnapshot"] == 2
            assert deleted["AgentRun"] == 1
            assert len(s.exec(select(MetricSnapshot)).all()) == 1   # свежий цел
            assert len(s.exec(select(AgentRun)).all()) == 1

    def test_dry_run_deletes_nothing(self):
        f = _factory()
        with f() as s:
            p = Project(name="T", type="t", is_active=True)
            s.add(p); s.commit(); s.refresh(p)
            old = utcnow() - timedelta(days=100)
            s.add(MetricSnapshot(project_id=p.id, period_key="7d", source="c",
                                 period_start=old, period_end=old,
                                 metrics_json={}, created_at=old))
            s.commit()
            deleted = cleanup_old_data(s, dry_run=True)
            assert deleted["MetricSnapshot"] == 1
            assert len(s.exec(select(MetricSnapshot)).all()) == 1   # но не удалён

    def test_живые_сущности_не_трогаются(self):
        """Project/эксперименты/базлайны переживают любую чистку."""
        f = _factory()
        with f() as s:
            old = utcnow() - timedelta(days=999)
            p = Project(name="T", type="t", is_active=True, created_at=old)
            s.add(p); s.commit()
            cleanup_old_data(s)
            assert len(s.exec(select(Project)).all()) == 1

    def test_job_never_raises(self):
        from app.scheduler import run_daily_cleanup

        def broken():
            raise RuntimeError("db down")

        out = asyncio.run(run_daily_cleanup(_session_factory=broken))
        assert out == {}   # проглотил ошибку, процесс жив
