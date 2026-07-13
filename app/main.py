"""
FastAPI приложение -- точка входа для Railway.

Маршруты:
- GET  /health           -- liveness check, без обращения к БД
- GET  /status            -- состояние проекта, интеграций, последний прогон
- POST /api/run             -- ручной запуск цикла (то же, что /run в Telegram)
- GET  /api/alerts            -- список последних алертов (для веб-интерфейса)
- GET  /api/snapshots           -- последние снэпшоты метрик
- POST /webhook/{secret}          -- Telegram webhook (если используется webhook,
                                      а не polling -- выбор через TELEGRAM_USE_WEBHOOK)
- GET  /                            -- статическая страница (static/index.html)

Запуск планировщика происходит в startup-событии FastAPI, а не отдельным
процессом -- для Railway это означает один процесс/один Procfile entry.
"""

import asyncio
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import select
from telegram import Update

from app.config import BUILD_MARKER, RUN_CYCLE_TIMEOUT_SECONDS, get_settings
from app.db import get_session, init_db
from app.models import Alert, Integration, MetricSnapshot, Project
from app.scheduler import run_cycle_once_sync_with_timeout, start_scheduler, stop_scheduler
from app.telegram_bot import build_application, send_cycle_notification

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("growth_agent.main")

app = FastAPI(title="Growth Agent Watchtower")

_telegram_app = None  # инициализируется в startup, если BOT_TOKEN задан


@app.on_event("startup")
async def on_startup() -> None:
    logger.info("Service startup started. Build: %s", BUILD_MARKER)
    init_db()

    settings = get_settings()

    global _telegram_app
    if settings.bot_token:
        _telegram_app = build_application()
        await _telegram_app.initialize()
        await _telegram_app.start()
        logger.info("Telegram bot polling/dispatcher started. Build: %s", BUILD_MARKER)
    else:
        logger.warning("BOT_TOKEN not set -- Telegram bot disabled, HTTP API still works")

    async def _scheduled_job_with_notification():
        """
        Обёртка вокруг run_cycle_once(), которая после цикла отправляет
        уведомление в Telegram, если есть что отправлять. Сам run_cycle_once
        ничего не знает про Telegram -- это сделано здесь, на уровне main.py,
        где оба компонента (scheduler, telegram_bot) уже собраны вместе.
        """
        try:
            result = await asyncio.to_thread(
                run_cycle_once_sync_with_timeout,
                None,
                RUN_CYCLE_TIMEOUT_SECONDS,
                "scheduled_cycle",
            )
            with get_session() as session:
                project = session.exec(select(Project).where(Project.is_active == True)).first()
                project_name = project.name if project else "Проект"

            if _telegram_app is not None:
                await send_cycle_notification(_telegram_app, result, project_name)

            logger.info(
                "Scheduled cycle done: notifiable=%s primary=%s",
                result.has_notifiable_changes,
                result.primary_candidate.title if result.primary_candidate else None,
            )
        except asyncio.TimeoutError:
            logger.warning("Scheduled cycle timed out after %.1f sec", RUN_CYCLE_TIMEOUT_SECONDS)
        except Exception:
            logger.exception("Scheduled cycle failed")

    scheduler = start_scheduler()
    # Подменяем job на версию с уведомлением -- start_scheduler() в
    # scheduler.py регистрирует "тихую" версию для модульного тестирования
    # без Telegram; здесь, в собранном приложении, нужна версия с отправкой.
    scheduler.remove_job("watch_cycle")
    scheduler.add_job(
        _scheduled_job_with_notification,
        "interval",
        seconds=settings.watch_interval_seconds,
        id="watch_cycle",
    )

    # Ежедневная утренняя сводка владельцу (доска + динамика) -- push раз в
    # день в фиксированный час, даже если изменений нет. Дедуп по дню внутри
    # send_daily_board, так что рестарты процесса дубль не создают.
    if settings.daily_board_enabled:
        from app.scheduler import send_daily_board

        scheduler.add_job(
            send_daily_board,
            "cron",
            hour=settings.daily_board_hour_utc,
            minute=5,
            id="daily_board",
        )

    # Ежедневная очистка старых данных: без неё таблицы растут вечно и
    # процесс умирает по Out of memory (кейс июля 2026). 03:30 UTC = 06:30 МСК,
    # тихие часы, никому не мешает.
    from app.scheduler import run_daily_cleanup

    scheduler.add_job(run_daily_cleanup, "cron", hour=3, minute=30, id="daily_cleanup")

    logger.info("Growth Agent started. Build: %s. Watch interval: %s sec", BUILD_MARKER, settings.watch_interval_seconds)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    stop_scheduler()
    global _telegram_app
    if _telegram_app is not None:
        await _telegram_app.stop()
        await _telegram_app.shutdown()


# ---------------------------------------------------------------------------
# Health / status
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    """
    Чистый liveness check -- не трогает БД и внешние сервисы. Railway/
    любой оркестратор дёргает это часто, важно чтобы это было быстро и
    не зависело от состояния БД или коннекторов.
    """
    return {"status": "ok"}


@app.get("/status")
async def status():
    settings = get_settings()
    from app.config import BUILD_MARKER
    with get_session() as session:
        project = session.exec(select(Project).where(Project.is_active == True)).first()
        if project is None:
            return JSONResponse(status_code=404, content={"error": "No active project", "build_marker": BUILD_MARKER})

        integrations = session.exec(
            select(Integration).where(Integration.project_id == project.id)
        ).all()

        open_alerts = session.exec(
            select(Alert).where(
                Alert.project_id == project.id,
                Alert.status.in_(["open", "sent", "acknowledged", "escalated"]),
            )
        ).all()

        return {
            "build_marker": BUILD_MARKER,
            "project": {
                "name": project.name,
                "type": project.type,
                "connector": project.connector_name,
                "mode": project.settings_json.get("mode", "watch_only"),
            },
            "integrations": [
                {
                    "type": i.type.value,
                    "status": i.status.value,
                    "last_sync_at": i.last_sync_at.isoformat() if i.last_sync_at else None,
                    "last_error": i.last_error,
                }
                for i in integrations
            ],
            "open_alerts_count": len(open_alerts),
            "watch_interval_seconds": settings.watch_interval_seconds,
        }


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.post("/api/run")
async def api_run():
    """
    Ручной запуск цикла. Идентичен /run в Telegram по результату, но
    возвращает JSON, а не текст -- используется веб-интерфейсом (static/).
    """
    try:
        result = await asyncio.to_thread(
            run_cycle_once_sync_with_timeout,
            None,
            RUN_CYCLE_TIMEOUT_SECONDS,
            "api_run",
        )
    except asyncio.TimeoutError:
        logger.warning("API /api/run timed out after %.1f sec", RUN_CYCLE_TIMEOUT_SECONDS)
        raise HTTPException(
            status_code=504,
            detail="Проверка заняла слишком много времени: внешний источник данных не ответил вовремя",
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.exception("API /api/run failed")
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "has_notifiable_changes": result.has_notifiable_changes,
        "primary": _candidate_to_dict(result.primary_candidate),
        "secondary": [_candidate_to_dict(c) for c in result.secondary],
        "changes": [
            {"change_type": c.change_type.value, "alert_id": c.alert.id, "title": c.alert.title}
            for c in result.changes
        ],
        "integration_changes": [
            {"change_type": c.change_type.value, "alert_id": c.alert.id, "title": c.alert.title}
            for c in result.integration_down_changes
        ],
    }


def _candidate_to_dict(candidate) -> dict | None:
    if candidate is None:
        return None
    return {
        "title": candidate.title,
        "category": candidate.category.value,
        "severity": candidate.severity.value,
        "confidence": candidate.confidence.value,
        "hypothesis": candidate.hypothesis,
        "check_action": candidate.check_action,
        "do_not_action": candidate.do_not_action,
        "period_key": candidate.period_key,
    }


@app.get("/api/alerts")
async def api_alerts(limit: int = 20):
    with get_session() as session:
        project = session.exec(select(Project).where(Project.is_active == True)).first()
        if project is None:
            raise HTTPException(status_code=404, detail="No active project")

        alerts = session.exec(
            select(Alert)
            .where(Alert.project_id == project.id)
            .order_by(Alert.last_seen_at.desc())
            .limit(limit)
        ).all()

        return [
            {
                "id": a.id,
                "title": a.title,
                "category": a.category.value,
                "severity": a.severity.value,
                "confidence": a.confidence.value,
                "status": a.status.value,
                "occurrence_count": a.occurrence_count,
                "first_seen_at": a.first_seen_at.isoformat(),
                "last_seen_at": a.last_seen_at.isoformat(),
            }
            for a in alerts
        ]


@app.get("/api/snapshots")
async def api_snapshots(limit: int = 20):
    with get_session() as session:
        project = session.exec(select(Project).where(Project.is_active == True)).first()
        if project is None:
            raise HTTPException(status_code=404, detail="No active project")

        snapshots = session.exec(
            select(MetricSnapshot)
            .where(MetricSnapshot.project_id == project.id)
            .order_by(MetricSnapshot.created_at.desc())
            .limit(limit)
        ).all()

        return [
            {
                "id": s.id,
                "period_key": s.period_key,
                "source": s.source,
                "as_of": s.as_of.isoformat() if s.as_of else None,
                "created_at": s.created_at.isoformat(),
            }
            for s in snapshots
        ]


# ---------------------------------------------------------------------------
# Telegram webhook (опционально -- по умолчанию используется polling,
# который запускается через _telegram_app.start() в startup; webhook --
# альтернатива для production на Railway, где polling может конфликтовать
# с несколькими инстансами)
# ---------------------------------------------------------------------------


@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    settings = get_settings()
    if not settings.bot_token or secret != settings.bot_token.split(":")[-1]:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    if _telegram_app is None:
        raise HTTPException(status_code=503, detail="Telegram bot not initialized")

    data = await request.json()
    update = Update.de_json(data, _telegram_app.bot)
    await _telegram_app.process_update(update)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Статика (веб-интерфейс)
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/")
async def root():
    return FileResponse("app/static/index.html")
