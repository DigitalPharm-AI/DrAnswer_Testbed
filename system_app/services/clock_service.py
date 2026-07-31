from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from shared.settings import get_settings
from shared.time_utils import utc_now
from system_app.models import SimulationClock

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
