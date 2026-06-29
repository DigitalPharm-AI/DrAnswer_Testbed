from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from shared.json_utils import parse_json_object
from shared.settings import get_settings
from shared.time_utils import utc_now
from system_app.models import Notification, SimulationClock

settings = get_settings()


def parse_clock_value(value: str) -> datetime:
    return datetime.fromisoformat(value)


def ensure_clock(session: Session) -> SimulationClock:
    clock = session.get(SimulationClock, 1)
    if clock:
        return clock
    initial_time = parse_clock_value(settings.simulation_initial_time)
    clock = SimulationClock(
        id=1,
        current_time=initial_time,
        is_running=False,
        speed_multiplier=0,
        last_processed_sim_time=initial_time,
        last_daily_pattern_sent_date=initial_time.date() - timedelta(days=1),
    )
    session.add(clock)
    session.commit()
    session.refresh(clock)
    return clock


def pause_simulation_clock(session: Session) -> SimulationClock:
    clock = ensure_clock(session)
    clock.is_running = False
    clock.speed_multiplier = 0
    clock.last_tick_real_at = utc_now()
    return clock


def pause_simulation_clock_for_conversation(session: Session) -> tuple[SimulationClock, dict]:
    clock = ensure_clock(session)
    resume_state = {
        "was_running": bool(clock.is_running and clock.speed_multiplier > 0),
        "speed_multiplier": clock.speed_multiplier if clock.speed_multiplier > 0 else 1,
    }
    pause_simulation_clock(session)
    return clock, resume_state


def pause_simulation_clock_at_conversation(session: Session, conversation_time: datetime) -> tuple[SimulationClock, dict]:
    clock, resume_state = pause_simulation_clock_for_conversation(session)
    clock.current_time = conversation_time
    clock.last_tick_real_at = utc_now()
    session.flush()
    return clock, resume_state


def resume_simulation_clock_from_notification(session: Session, notification: Notification) -> None:
    if notification.notification_type != "conversation_alert":
        return
    metadata = parse_json_object(notification.metadata_json)
    resume_clock = metadata.get("resume_clock") if isinstance(metadata.get("resume_clock"), dict) else {}
    if not resume_clock.get("was_running"):
        return
    speed_multiplier = resume_clock.get("speed_multiplier")
    if not isinstance(speed_multiplier, int) or speed_multiplier <= 0:
        speed_multiplier = 1
    clock = ensure_clock(session)
    clock.is_running = True
    clock.speed_multiplier = speed_multiplier
    clock.last_tick_real_at = utc_now()
