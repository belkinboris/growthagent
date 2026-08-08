"""
После «Принять» владелец должен видеть, что делать руками (задача R11).

Дефект с живого продукта (08.08.2026), словами владельца: «я акцептовал
починку поста, но не знаю что делать... на том месте плашка, что ничего
делать не надо, и я не понимаю, как быть».

Разбор. Рекомендация несёт список конкретных изменений (change_set) --
например, «одна правка инструкций для ИИ-генератора». Платформа внести
их не может: АвтоПост -- чужой продукт, API на запись у него нет (см.
принцип 5 и запись F6 в PRODUCT_ROADMAP.md). Делает их человек. Но после
принятия карточка рекомендации исчезала, а на её месте появлялась плашка
«от вас сейчас ничего не нужно» -- и список пропадал с экрана совсем.
Платформа ждала результат правки, которую никто не сделал, и говорила
владельцу, что всё идёт как надо.

Список берётся по существующей связи recommendation_id, без новой колонки
в таблице эксперимента (правило репозитория: без ALTER TABLE).
"""
from app.models import GrowthExperiment, GrowthRecommendation

CHANGE_SET = [
    "одна правка инструкций для ИИ-генератора под причину «не тот стиль»",
    "десять дней больше ничего не трогаем",
]


def _seed(session_factory, pid) -> int:
    with session_factory() as session:
        rec = GrowthRecommendation(
            project_id=pid, area="first_post", title="Чиним качество первого поста",
            action="Один раз меняем инструкции для ИИ...", hypothesis="...",
            confidence="сигнал", primary_metric="first_post_feedback_good",
            sample_metric="first_post_feedback_total", target_sample=10,
            fingerprint=f"{pid}/first_post/r11", status="accepted",
            change_set_json=CHANGE_SET,
        )
        session.add(rec)
        session.commit()
        session.refresh(rec)
        rec_id = rec.id
        session.add(GrowthExperiment(
            project_id=pid, recommendation_id=rec_id, area="first_post",
            title="Чиним качество первого поста", hypothesis="...", status="running",
            primary_metric="first_post_feedback_good",
            sample_metric="first_post_feedback_total", target_sample=10,
        ))
        session.commit()
    return rec_id


class TestRunningExperimentCarriesTheWork:
    def test_change_set_survives_accepting(self, monkeypatch, tmp_path):
        from tests.test_platform_api import _client, _login, _project_id

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        _seed(session_factory, pid)
        _login(client)

        exp = client.get("/growth/api/growth").json()["experiment"]
        assert exp["change_set"] == CHANGE_SET, (
            "после принятия список работ обязан остаться на экране: "
            "делает эту работу владелец, а не платформа"
        )

    def test_action_text_is_kept_too(self, monkeypatch, tmp_path):
        """Одного списка мало -- нужна и формулировка, что вообще меняем."""
        from tests.test_platform_api import _client, _login, _project_id

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        _seed(session_factory, pid)
        _login(client)

        exp = client.get("/growth/api/growth").json()["experiment"]
        assert exp["action"].startswith("Один раз меняем инструкции")

    def test_experiment_without_recommendation_does_not_crash(self, monkeypatch, tmp_path):
        """
        Эксперимент со ссылкой на удалённую рекомендацию не должен ронять
        весь экран: пустой список честнее, чем 500.
        """
        from tests.test_platform_api import _client, _login, _project_id

        client, session_factory = _client(monkeypatch, tmp_path)
        pid = _project_id(session_factory)
        with session_factory() as session:
            session.add(GrowthExperiment(
                project_id=pid, recommendation_id=99999, area="first_post",
                title="Осиротевшая проверка", hypothesis="...", status="running",
                primary_metric="x", sample_metric="registrations", target_sample=10,
            ))
            session.commit()
        _login(client)

        response = client.get("/growth/api/growth")
        assert response.status_code == 200
        assert response.json()["experiment"]["change_set"] == []


class TestOwnerFacingText:
    def test_ui_shows_the_work_instead_of_nothing_to_do(self):
        """
        Плашка «от вас ничего не нужно» допустима ТОЛЬКО когда работ нет.
        Если change_set непустой, на экране должно стоять обратное.
        """
        from pathlib import Path

        html = Path("app/static/platform/index.html").read_text(encoding="utf-8")
        assert "Это нужно сделать вам, руками" in html
        # Ветка «ничего не нужно» осталась, но теперь она под условием.
        marker = html.index("От вас сейчас ничего не нужно")
        condition = html.rindex("e.change_set && e.change_set.length", 0, marker)
        assert marker - condition < 800, (
            "плашка «ничего не нужно» должна быть в else-ветке проверки change_set"
        )
