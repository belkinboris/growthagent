"""
Электронный сбор ответов (задача R14).

Возражение владельца, из которого выросла задача: R12 нашёл застой, но
закончил советом «сходите и поговорите с пятью незаплатившими». Это
ручной труд фаундера — прямая противоположность тому, ради чего платформа
делается. Причину неоплаты знают только сами люди, и спросить их должен
продукт, электронно.

Проверяется:
1. Вопрос предлагается ТОЛЬКО когда есть о чём спрашивать.
2. Формулировки — про прожитый опыт, не гипотетические.
3. Продукт забирает вопрос сам по токену; аналитик не получает доступа
   к людям (приватность).
4. Ответы не задваиваются, набранный вопрос закрывается сам.
"""
import pytest

from app.questions import DEFAULT_TARGET_ANSWERS, PLANS, plan_for_course, summarize_answers


def _course(stalled=True, headline="Регистрации есть, деньги не появляются", ok=True):
    return {"ok": ok, "stalled": stalled, "headline": headline}


class TestWhenToAsk:
    def test_no_question_while_product_grows(self):
        """Растущий продукт не дёргаем: вопрос тратит терпение человека."""
        assert plan_for_course(_course(stalled=False, headline="Продукт движется")) is None

    def test_no_question_without_a_verdict(self):
        assert plan_for_course(_course(ok=False)) is None
        assert plan_for_course(None) is None

    def test_money_stall_asks_those_who_saw_pricing(self):
        plan = plan_for_course(_course())
        assert plan is not None
        assert plan.segment == "pricing"

    def test_demand_stall_asks_those_who_tried_the_product(self):
        plan = plan_for_course(_course(headline="Продукт стоит на месте: новых людей почти нет"))
        assert plan.segment == "first_post"

    def test_general_stall_has_its_own_question(self):
        plan = plan_for_course(_course(headline="Продукт стоит на месте 5 недель"))
        assert plan is not None
        assert plan.question


class TestQuestionWording:
    @pytest.mark.parametrize("key", list(PLANS))
    def test_question_is_about_lived_experience_not_hypothesis(self, key):
        """
        «Вы бы заплатили, если…» собирает вежливые фантазии. Вопрос обязан
        спрашивать про то, что уже произошло.
        """
        q = PLANS[key].question.lower()
        assert "вы бы " not in q, "гипотетический вопрос собирает фантазии, а не факты"

    @pytest.mark.parametrize("key", list(PLANS))
    def test_only_one_question_mark(self, key):
        """Анкета из нескольких вопросов даёт отказ, а не ответы."""
        assert PLANS[key].question.count("?") == 1

    @pytest.mark.parametrize("key", list(PLANS))
    def test_no_answer_options_are_offered(self, key):
        """Варианты ответа вернут наши же гипотезы вместо слов людей."""
        q = PLANS[key].question
        assert "1)" not in q and "а)" not in q

    @pytest.mark.parametrize("key", list(PLANS))
    def test_russian_and_polite(self, key):
        q = PLANS[key].question
        assert not any(c.isascii() and c.isalpha() for c in q), "английского в вопросе быть не должно"


class TestAnswerSummary:
    def test_two_answers_are_not_a_conclusion(self):
        s = summarize_answers(["дорого", "не понял ценность"])
        assert s["count"] == 2
        assert "мало" in s["note"]

    def test_enough_answers_are_direction_not_measurement(self):
        s = summarize_answers(["дорого"] * DEFAULT_TARGET_ANSWERS)
        assert "направление, а не измерение" in s["note"]

    def test_empty_answers_are_dropped(self):
        assert summarize_answers(["", "  ", "дорого"])["count"] == 1


class TestProductPullsQuestion:
    """Продукт забирает вопрос сам — у аналитика нет доступа к людям."""

    def _project_with_token(self, client):
        rows = client.get("/growth/api/projects").json()
        if isinstance(rows, dict):
            rows = rows.get("projects") or []
        pid = rows[0]["id"]
        token = client.post(f"/growth/api/projects/{pid}/inbound-token").json()["token"]
        return pid, token

    def _ask(self, client):
        return client.post("/growth/api/questions/decide", json={
            "action": "ask", "question": "Что вас остановило?",
            "segment": "pricing", "reason": "тест", "target_answers": 2,
        }).json()["id"]

    def test_wrong_token_is_refused(self, monkeypatch, tmp_path):
        from tests.test_platform_api import _client, _login, _project_id

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        _login(client)
        self._project_with_token(client)
        r = client.get(f"/growth/api/public/projects/{pid}/questions",
                       headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_product_receives_only_approved_questions(self, monkeypatch, tmp_path):
        """
        Спросить живого человека — действие наружу, поэтому оно проходит
        через ту же ручку автономии, что и правки рекламы: пока владелец не
        нажал кнопку, продукт вопроса не видит и никого не беспокоит.
        """
        from app.models import UserQuestion, UserQuestionStatus
        from tests.test_platform_api import _client, _login

        client, session_factory = _client(monkeypatch, tmp_path)
        _login(client)
        pid, token = self._project_with_token(client)
        auth = {"Authorization": f"Bearer {token}"}

        # Предложенный, но НЕ разрешённый вопрос лежит в базе...
        with session_factory() as session:
            session.add(UserQuestion(
                project_id=pid, segment="pricing",
                question="Ещё не разрешённый вопрос?",
                status=UserQuestionStatus.proposed, target_answers=5,
            ))
            session.commit()
        # ...и наружу не уходит.
        assert client.get(f"/growth/api/public/projects/{pid}/questions",
                          headers=auth).json()["questions"] == []

        self._ask(client)
        got = client.get(f"/growth/api/public/projects/{pid}/questions",
                         headers=auth).json()["questions"]
        assert len(got) == 1
        assert got[0]["segment"] == "pricing"
        assert "Ещё не разрешённый" not in got[0]["question"]

    def test_rejected_question_is_never_asked(self, monkeypatch, tmp_path):
        """Владелец отказался — продукт не должен спросить «на всякий случай»."""
        from tests.test_platform_api import _client, _login

        client, session_factory = _client(monkeypatch, tmp_path)
        _login(client)
        pid, token = self._project_with_token(client)
        auth = {"Authorization": f"Bearer {token}"}
        qid = self._ask(client)
        client.post("/growth/api/questions/decide",
                    json={"action": "reject", "question_id": qid})
        assert client.get(f"/growth/api/public/projects/{pid}/questions",
                          headers=auth).json()["questions"] == []

    def test_answers_flow_back_and_reach_the_owner(self, monkeypatch, tmp_path):
        from tests.test_platform_api import _client, _login

        client, session_factory = _client(monkeypatch, tmp_path)
        _login(client)
        pid, token = self._project_with_token(client)
        auth = {"Authorization": f"Bearer {token}"}
        qid = self._ask(client)

        client.post(f"/growth/api/public/projects/{pid}/answers", headers=auth,
                    json={"question_id": qid, "user_key": "u_1", "text": "дорого для меня"})
        owner = client.get("/growth/api/questions").json()
        assert owner["questions"][0]["count"] == 1
        assert "дорого для меня" in owner["questions"][0]["answers"]

    def test_same_person_counted_once(self, monkeypatch, tmp_path):
        """Пять реплик одного человека — это одно мнение, не пять."""
        from tests.test_platform_api import _client, _login

        client, session_factory = _client(monkeypatch, tmp_path)
        _login(client)
        pid, token = self._project_with_token(client)
        auth = {"Authorization": f"Bearer {token}"}
        qid = self._ask(client)

        for text in ("дорого", "и ещё непонятно"):
            client.post(f"/growth/api/public/projects/{pid}/answers", headers=auth,
                        json={"question_id": qid, "user_key": "u_1", "text": text})
        assert client.get("/growth/api/questions").json()["questions"][0]["count"] == 1

    def test_question_closes_itself_when_enough_collected(self, monkeypatch, tmp_path):
        """Набрали нужное — продукт перестаёт спрашивать людей без команды."""
        from tests.test_platform_api import _client, _login

        client, session_factory = _client(monkeypatch, tmp_path)
        _login(client)
        pid, token = self._project_with_token(client)
        auth = {"Authorization": f"Bearer {token}"}
        qid = self._ask(client)   # target_answers=2

        for key in ("u_1", "u_2"):
            client.post(f"/growth/api/public/projects/{pid}/answers", headers=auth,
                        json={"question_id": qid, "user_key": key, "text": "дорого"})
        assert client.get(f"/growth/api/public/projects/{pid}/questions",
                          headers=auth).json()["questions"] == []
        assert client.get("/growth/api/questions").json()["questions"][0]["status"] == "done"

    def test_empty_answer_is_refused(self, monkeypatch, tmp_path):
        from tests.test_platform_api import _client, _login

        client, session_factory = _client(monkeypatch, tmp_path)
        _login(client)
        pid, token = self._project_with_token(client)
        qid = self._ask(client)
        r = client.post(f"/growth/api/public/projects/{pid}/answers",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"question_id": qid, "user_key": "u_1", "text": "   "})
        assert r.status_code == 400


class TestOwnerScreen:
    def test_no_token_is_stated_before_asking(self, monkeypatch, tmp_path):
        """Без токена продукт не заберёт вопрос — сказать это надо сразу."""
        from tests.test_platform_api import _client, _login

        client, session_factory = _client(monkeypatch, tmp_path)
        _login(client)
        r = client.get("/growth/api/questions").json()
        assert r["delivery_ready"] is False
        assert "токен" in r["delivery_hint"]

    def test_no_second_question_while_one_is_running(self, monkeypatch, tmp_path):
        """Два вопроса разом превращают продукт в анкету."""
        from tests.test_platform_api import _client, _login

        client, session_factory = _client(monkeypatch, tmp_path)
        _login(client)
        client.post("/growth/api/questions/decide", json={
            "action": "ask", "question": "Что вас остановило?", "segment": "pricing"})
        assert client.get("/growth/api/questions").json()["suggestion"] is None
