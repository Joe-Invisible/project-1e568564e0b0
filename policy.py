"""Select a tactical mode from match features. A learned policy can replace choose()."""

from dataclasses import dataclass
from enum import Enum, auto


class Mode(Enum):
    ROTATE = auto()
    DISENGAGE = auto()
    ENGAGE = auto()
    PURSUE = auto()
    LOOT = auto()


@dataclass(frozen=True)
class Features:
    zone_urgent: bool
    health: float
    pressure: int
    under_fire: bool
    target_health: float
    target_distance: float
    preferred_range: float
    can_shoot: bool
    has_loot: bool
    can_pursue: bool


def choose(state: Features) -> Mode:
    if state.zone_urgent:
        return Mode.ROTATE
    finishing = state.can_shoot and state.target_health < 0.18
    threatened = state.under_fire or state.can_shoot or state.pressure > 0
    if (threatened and state.health < 0.42) or (state.pressure >= 2 and state.health < 0.72):
        if not (finishing and state.health > 0.25):
            return Mode.DISENGAGE
    if state.can_shoot:
        return Mode.ENGAGE
    if state.has_loot and not state.under_fire and state.target_distance > state.preferred_range:
        return Mode.LOOT
    if state.can_pursue:
        return Mode.PURSUE
    if state.under_fire:
        return Mode.DISENGAGE
    return Mode.LOOT if state.has_loot else Mode.ROTATE


class Recovery:
    """Stay out of combat long enough to reload and begin recovering HP."""

    def __init__(self):
        self.started = None
        self.last_exchange = float("-inf")
        self.retry_after = 0

    def update(self, now, state, clear_threats, reload_needed, regen_delay):
        if self.started is not None:
            quiet = now - max(self.started, self.last_exchange)
            recovered = quiet >= regen_delay and state.health >= 0.78 and not reload_needed
            if recovered or now - self.started >= 14:
                self.started = None
                self.retry_after = now + 4
            return

        finishing = state.can_shoot and state.target_health < 0.12 and state.health > 0.3
        pressed = clear_threats >= 2 or (clear_threats > 0 and state.health < 0.50)
        if (now >= self.retry_after and not state.zone_urgent
                and ((pressed and not finishing) or reload_needed and state.can_shoot)):
            self.started = now
