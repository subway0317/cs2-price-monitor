from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import sessionmaker

from cs2_price_monitor.services.prices import regenerate_daily_summaries


def build_scheduler(session_factory: sessionmaker) -> BackgroundScheduler:
    """Build daily summary scheduler."""
    scheduler = BackgroundScheduler(timezone="UTC")

    def job() -> None:
        with session_factory() as session:
            regenerate_daily_summaries(session)
            session.commit()

    scheduler.add_job(job, "cron", hour=0, minute=5, id="daily_price_summaries")
    return scheduler
