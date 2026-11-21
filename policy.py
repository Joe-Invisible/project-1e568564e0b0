"""Select a tactical mode from match features. A learned policy can replace choose()."""

from dataclasses import dataclass
from enum import Enum, auto
from math import isfinite


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


@dataclass(frozen=True)
class RecoveryProfile:
    exposed_s: float = 2.5
    max_s: float = 7.0
    retry_s: float = 10.0
    ammo_fraction: float = 0.25
    pressure_health: float = 0.65

    def __post_init__(self):
        times = (self.exposed_s, self.max_s, self.retry_s)
        fractions = (self.ammo_fraction, self.pressure_health)
        if not (all(isfinite(t) and t > 0 for t in times)
                and self.exposed_s <= self.max_s
                and all(isfinite(f) and 0 <= f <= 1 for f in fractions)):
            raise ValueError("Invalid recovery profile")


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

    def __init__(self, profile=None):
        self.profile = profile
        self.started = None
        self.last_exchange = float("-inf")
        self.retry_after = 0

    def update(self, now, state, clear_threats, reload_needed, regen_delay):
        profile = self.profile
        if self.started is not None:
            elapsed = now - self.started
            quiet = now - max(self.started, self.last_exchange)
            recovered = quiet >= regen_delay and state.health >= 0.78 and not reload_needed
            if profile is None:
                done = recovered or elapsed >= 14
            else:
                exposed = clear_threats > 0 or state.under_fire
                reloaded = not reload_needed and state.health >= 0.78 and elapsed >= 0.5
                done = (recovered or reloaded or elapsed >= profile.max_s
                        or (exposed and elapsed >= profile.exposed_s))
            if done:
                self.started = None
                self.retry_after = now + (4 if profile is None else profile.retry_s)
            return

        finishing = state.can_shoot and state.target_health < 0.12 and state.health > 0.3
        pressure_health = 1.0 if profile is None else profile.pressure_health
        pressed = ((clear_threats >= 2 and state.health <= pressure_health)
                   or (clear_threats > 0 and state.health < min(0.50, pressure_health)))
        if (now >= self.retry_after and not state.zone_urgent
                and ((pressed and not finishing) or reload_needed and state.can_shoot)):
            self.started = now
