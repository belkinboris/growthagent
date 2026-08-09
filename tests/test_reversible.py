"""
Обратимость и замкнутый цикл (задачи A1, A2).

Владелец сформулировал цель автономии так: аналитик сам понимает, что
нужно менять, меняет, проверяет результат, **утверждает или откатывает**.
Первые три шага были. Четвёртого не было вовсе: вердикт писал словами
«откатить изменение», `payload_json` исправно хранил `before` — и этот
`before` не читал никто и никогда.

Почему это первое, а не последнее. Пока откат не гарантирован, автономия
— храповик в одну сторону: агент только добавляет правки, и чем дольше
работает, тем больше накапливается изменений, о которых никто не помнит,
зачем они. Обратимость — условие, при котором остальную автономию вообще
можно включать.
"""
import pytest

from app.models import AgentAction, AgentActionStatus
from app.reversible import (
    RevertResult, reversibility_of, revert_action, revertible_actions,
)


class _Res:
    """Ответ write-клиента Директа."""

    def __init__(self, applied=None, skipped=None, warnings=None):
        self.applied = applied or []
        self.skipped = skipped or []
        self.warnings = warnings or []


class TestCostOfError:
    """
    Одинаковая ручка автономии для «добавить минус-фразу» и «поднять
    бюджет» была бы обманом: это разные по цене ошибки поступки.
    """

    def test_negative_keywords_are_cheap_to_get_wrong(self):
        r = reversibility_of("direct_negative_keywords")
        assert r.cost_of_error == "низкая" and r.auto_revertible

    def test_bid_change_is_expensive(self):
        assert reversibility_of("direct_bid").cost_of_error == "высокая"

    def test_product_and_pricing_cannot_be_auto_reverted(self):
        """У платформы нет доступа на запись в чужой продукт и в цены."""
        assert not reversibility_of("product").auto_revertible
        assert not reversibility_of("pricing").auto_revertible

    def test_unknown_domain_is_treated_as_dangerous(self):
        """Осторожность по умолчанию безопаснее оптимизма по умолчанию."""
        r = reversibility_of("что_то_новое")
        assert not r.auto_revertible and r.cost_of_error == "высокая"


def _applied_action(session, pid, domain="direct_negative_keywords", rec_id=None):
    action = AgentAction(
        project_id=pid, agent="marketer", domain=domain,
        action="add_negative_keywords", reasoning="мусорные запросы",
        payload_json={"before": {"ad_group_id": "42"},
                      "after": {"phrases": ["шапка youtube"], "applied": ["шапка youtube"]}},
        status=AgentActionStatus.applied.value,
        related_recommendation_id=rec_id,
    )
    session.add(action)
    session.commit()
    session.refresh(action)
    return action


class TestRevertOneAction:
    def _session(self):
        from sqlmodel import Session, SQLModel, create_engine

        engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(engine)
        return Session(engine)

    def _project(self, session):
        from app.models import Project

        p = Project(name="Тест", type="telegram_saas", connector_name="truepost", is_active=True)
        session.add(p); session.commit(); session.refresh(p)
        return p

    @pytest.mark.asyncio
    async def test_removes_exactly_what_was_applied(self, monkeypatch):
        """
        Откатываем то, что РЕАЛЬНО применилось, а не то, что предлагали:
        часть фраз Директ мог отклонить, и откатывать их нечего.
        """
        from app.connectors import direct_write

        seen = {}

        async def fake_remove(settings, ad_group_id, phrases):
            seen["group"] = ad_group_id
            seen["phrases"] = list(phrases)
            return _Res(applied=list(phrases))

        monkeypatch.setattr(direct_write, "is_configured", lambda s: True)
        monkeypatch.setattr(direct_write, "remove_negative_keywords", fake_remove)

        with self._session() as session:
            project = self._project(session)
            action = _applied_action(session, project.id)
            result = await revert_action(session, action, object(), project)
            assert result.ok
            assert seen["group"] == "42"
            assert seen["phrases"] == ["шапка youtube"]
            assert action.status == AgentActionStatus.reverted.value

    @pytest.mark.asyncio
    async def test_revert_is_idempotent(self, monkeypatch):
        """
        Автооткат может сработать дважды (повтор цикла, перезапуск). Второй
        раз он не должен ничего трогать.
        """
        from app.connectors import direct_write

        calls = []

        async def fake_remove(settings, ad_group_id, phrases):
            calls.append(phrases)
            return _Res(applied=list(phrases))

        monkeypatch.setattr(direct_write, "is_configured", lambda s: True)
        monkeypatch.setattr(direct_write, "remove_negative_keywords", fake_remove)

        with self._session() as session:
            project = self._project(session)
            action = _applied_action(session, project.id)
            await revert_action(session, action, object(), project)
            second = await revert_action(session, action, object(), project)

        assert second.ok
        assert len(calls) == 1, "второй откат не должен ходить в Директ"

    @pytest.mark.asyncio
    async def test_revert_time_goes_into_payload_not_a_new_column(self, monkeypatch):
        """
        Правило репозитория: без ALTER TABLE на живых таблицах. Время
        отката поэтому живёт в payload_json, а не отдельной колонкой.
        """
        from app.connectors import direct_write

        monkeypatch.setattr(direct_write, "is_configured", lambda s: True)
        monkeypatch.setattr(direct_write, "remove_negative_keywords",
                            lambda s, g, p: _async(_Res(applied=list(p))))

        with self._session() as session:
            project = self._project(session)
            action = _applied_action(session, project.id)
            await revert_action(session, action, object(), project)
            assert action.payload_json["revert"]["at"]
        assert not hasattr(AgentAction, "reverted_at")

    @pytest.mark.asyncio
    async def test_not_applied_action_is_not_reverted(self):
        with self._session() as session:
            project = self._project(session)
            action = _applied_action(session, project.id)
            action.status = AgentActionStatus.proposed.value
            result = await revert_action(session, action, object(), project)
            assert not result.ok and "не применялось" in result.message

    @pytest.mark.asyncio
    async def test_unconfigured_write_fails_honestly(self, monkeypatch):
        """Не смогли откатить — говорим прямо, а не молчим «готово»."""
        from app.connectors import direct_write

        monkeypatch.setattr(direct_write, "is_configured", lambda s: False)
        with self._session() as session:
            project = self._project(session)
            action = _applied_action(session, project.id)
            result = await revert_action(session, action, object(), project)
            assert not result.ok
            assert "вручную" in result.message
            assert action.status == AgentActionStatus.applied.value, "статус менять нельзя"

    @pytest.mark.asyncio
    async def test_non_revertible_domain_explains_why(self):
        with self._session() as session:
            project = self._project(session)
            action = _applied_action(session, project.id, domain="pricing")
            result = await revert_action(session, action, object(), project)
            assert not result.ok
            assert "клиент" in result.message.lower()


def _async(value):
    async def _inner(*a, **kw):
        return value
    return _inner()


class TestScopeOfRevert:
    """Откат после проигравшей проверки не должен отменять всё подряд."""

    def _session(self):
        from sqlmodel import Session, SQLModel, create_engine

        engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(engine)
        return Session(engine)

    def test_only_actions_of_this_recommendation(self):
        from app.models import Project

        with self._session() as session:
            p = Project(name="Т", type="telegram_saas", connector_name="truepost", is_active=True)
            session.add(p); session.commit(); session.refresh(p)
            mine = _applied_action(session, p.id, rec_id=7)
            _applied_action(session, p.id, rec_id=9)          # чужая проверка
            found = revertible_actions(session, p.id, 7)
            assert [a.id for a in found] == [mine.id]

    def test_already_reverted_is_not_offered_again(self):
        from app.models import Project

        with self._session() as session:
            p = Project(name="Т", type="telegram_saas", connector_name="truepost", is_active=True)
            session.add(p); session.commit(); session.refresh(p)
            action = _applied_action(session, p.id, rec_id=7)
            action.status = AgentActionStatus.reverted.value
            session.add(action); session.commit()
            assert revertible_actions(session, p.id, 7) == []
