"""Corridas programadas (APScheduler en el mismo proceso; una sola réplica en Render)."""
from __future__ import annotations

import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import db
from .config import settings

_sched: BackgroundScheduler | None = None
_lock = threading.Lock()


def _job_agente1():
    from .agents import consumo, briefing
    try:
        consumo.ejecutar(usuario="programado", disparo="programado")
        briefing.generar(usuario="programado")
    except Exception as e:  # noqa: BLE001
        db.log("error", "scheduler", "Falló la corrida programada del Agente 1", str(e))


def _job_agente2():
    from .agents import demanda, briefing
    try:
        demanda.ejecutar(usuario="programado", disparo="programado")
        briefing.generar(usuario="programado")
    except Exception as e:  # noqa: BLE001
        db.log("error", "scheduler", "Falló la corrida programada del Agente 2", str(e))


def _job_seguimiento():
    from .agents import autonomia
    try:
        autonomia.verificar_ejecutadas()
    except Exception as e:  # noqa: BLE001
        db.log("error", "scheduler", "Falló el seguimiento de acciones", str(e))


def _job_vigilancia():
    from .agents import vigilancia
    try:
        vigilancia.ejecutar()
    except Exception as e:  # noqa: BLE001
        db.log("error", "scheduler", "Falló la vigilancia programada", str(e))


def _job_limpieza():
    """Borra Excels viejos del disco persistente (retención configurable)."""
    import os
    import time
    limite = time.time() - settings.REPORT_RETENTION_DAYS * 86400
    borrados = 0
    for p in settings.REPORTS_DIR.glob("*.xlsx"):
        try:
            if p.stat().st_mtime < limite:
                os.remove(p)
                borrados += 1
        except OSError:
            pass
    if borrados:
        db.log("info", "scheduler", f"Limpieza: {borrados} reportes antiguos eliminados")


def iniciar() -> BackgroundScheduler | None:
    global _sched
    with _lock:
        if _sched or not settings.SCHEDULE_ENABLED:
            return _sched
        s = BackgroundScheduler(timezone=settings.TZ)
        try:
            s.add_job(_job_agente1, CronTrigger.from_crontab(settings.SCHEDULE_AGENT1_CRON, timezone=settings.TZ),
                      id="agente1", replace_existing=True, misfire_grace_time=3600)
            s.add_job(_job_agente2, CronTrigger.from_crontab(settings.SCHEDULE_AGENT2_CRON, timezone=settings.TZ),
                      id="agente2", replace_existing=True, misfire_grace_time=3600)
            s.add_job(_job_limpieza, CronTrigger.from_crontab("15 3 * * *", timezone=settings.TZ), id="limpieza")
            s.add_job(_job_seguimiento, CronTrigger.from_crontab("0 */4 * * *", timezone=settings.TZ), id="seguimiento",
                      misfire_grace_time=3600)
            s.add_job(_job_vigilancia, CronTrigger.from_crontab(settings.SCHEDULE_VIGILANCIA_CRON, timezone=settings.TZ), id="vigilancia",
                      misfire_grace_time=3600)
        except ValueError as e:
            db.log("error", "scheduler", "Expresión cron inválida", str(e))
            return None
        s.start()
        _sched = s
        db.log("info", "scheduler", "Programación activa",
               f"A1={settings.SCHEDULE_AGENT1_CRON} A2={settings.SCHEDULE_AGENT2_CRON} tz={settings.TZ}")
        return s


def estado() -> dict:
    if not _sched:
        return {"activo": False, "trabajos": []}
    return {"activo": True, "trabajos": [{"id": j.id, "proxima": str(j.next_run_time)} for j in _sched.get_jobs()]}
