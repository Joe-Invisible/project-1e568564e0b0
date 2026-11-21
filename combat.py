"""Aim solutions and trigger control for the mech's two weapon slots."""

from dataclasses import dataclass
from math import asin, atan2, cos, dist, hypot, pi, radians, sin, sqrt


@dataclass
class Shot:
    slot: int
    aim: tuple
    value: float
    tolerance: float


def angle_delta(a, b):
    return (a - b + pi) % (2 * pi) - pi


def bearing(start, end):
    return atan2(end[1] - start[1], end[0] - start[0])


def intercept_time(offset, velocity, speed):
    a = sum(v * v for v in velocity) - speed * speed
    b = 2 * sum(o * v for o, v in zip(offset, velocity))
    c = sum(o * o for o in offset)
    if abs(a) < 1e-8:
        return -c / b if b < 0 else None
    discriminant = b * b - 4 * a * c
    if discriminant < 0:
        return None
    root = sqrt(discriminant)
    return min((t for t in ((-b - root) / (2 * a), (-b + root) / (2 * a))
                if t >= 0), default=None)


def target_center(target, mech):
    if target.get("kind") == "mech":
        scale = 1 + mech["size_per_cube"] * target["cubes"]
        return mech["height"] * scale * 0.55, mech["radius"] * scale
    if target.get("kind") == "npc":
        return 1.75, 1.8
    if target.get("kind") == "crate":
        return 1.2, 1.4
    return 1.0, 1.0


class Combat:
    def __init__(self, arena, config):
        self.arena = arena
        self.mech = {**config["mech"], "size_per_cube": config["cubes"]["size_per_cube"]}
        self.lightning_range = config["weapons"]["lightning"]["range"]

    def visible_shot(self, me, target):
        pos = me["pos"]
        height, _ = target_center(target, self.mech)
        origin = (*pos[:2], pos[2] + me["stats"]["muzzle_height"])
        end = (*target["pos"][:2], target["pos"][2] + height)
        return self.arena.clear_shot(origin, end)

    def counterplay(self, me, target, shots):
        if "lightning" not in target.get("weapons", ()):
            return shots
        distance = dist(me["pos"][:2], target["pos"][:2])
        close = 0
        if distance > 0:
            close = sum((a - b) * v for a, b, v in
                        zip(me["pos"][:2], target["pos"][:2], target["vel"][:2])) / distance
        safe = []
        for shot in shots:
            weapon = me["weapons"][shot.slot]
            if weapon["kind"] == "laser" and not weapon.get("charge", 0):
                buffer = 6 + max(0, close) * (weapon["stats"]["charge"] + 0.2)
                if distance <= self.lightning_range + buffer:
                    continue
            safe.append(shot)
        return safe

    def solutions(self, me, target):
        height, radius = target_center(target, self.mech)
        point = (*target["pos"][:2], target["pos"][2] + height)
        velocity = target.get("vel", (0, 0, 0))
        stats = me["stats"]
        shots = []
        for slot, weapon in enumerate(me["weapons"]):
            if weapon is None:
                continue
            kind, w = weapon["kind"], weapon["stats"]
            muzzle_height = stats["height"] + 0.5 if kind == "mortar" else stats["muzzle_height"]
            origin = (*me["pos"][:2], me["pos"][2] + muzzle_height)
            offset = tuple(b - a for a, b in zip(origin, point))
            launch_offset = stats["radius"] + 0.5
            distance = dist(origin, point)
            max_range = w["speed"] ** 2 / w["gravity"] if kind == "mortar" else w["range"]
            if distance > max_range + launch_offset or (kind == "mortar" and distance < 18):
                continue
            aim = self.solve(kind, w, origin, point, offset, velocity, launch_offset)
            if aim is None or not stats["min_pitch"] <= aim[1] <= stats["max_pitch"]:
                continue
            tolerance = max(0.006, asin(min(1, radius / max(distance, radius))) * 0.85)
            value = w["damage"] / (w["cooldown"] + 0.12)
            if kind == "gun" and (not weapon.get("ammo") or weapon.get("reload", 0) > 0):
                value *= 0.08
            if kind == "lightning":
                if me["energy"] < w["energy"]:
                    continue
                tolerance = radians(w["cone_deg"]) / 2
                value *= 1.55
            if kind == "laser":
                value *= 1.25 if target.get("kind") == "mech" else 0.70
                if weapon.get("charge", 0):
                    value *= 2
            if weapon["cooldown"] > 0.15:
                value /= 1 + weapon["cooldown"] * 5
            shots.append(Shot(slot, aim, value, tolerance))
        return shots

    def solve(self, kind, w, origin, point, offset, velocity, launch_offset):
        if kind == "mortar":
            return self.mortar(w, origin, point, velocity)
        end = point
        if kind == "gun":
            flight = intercept_time(offset, velocity, w["speed"])
            if flight is None:
                return None
            flight = max(0, flight - launch_offset / w["speed"])
            end = tuple(p + v * flight for p, v in zip(point, velocity))
        if not self.arena.clear_shot(origin, end):
            return None
        return bearing(origin, end), atan2(end[2] - origin[2], dist(origin[:2], end[:2]))

    def mortar(self, w, origin, point, velocity):
        speed, gravity = w["speed"], w["gravity"]
        flight = dist(origin[:2], point[:2]) / speed
        for _ in range(4):
            end = tuple(p + v * flight for p, v in zip(point, velocity))
            horizontal = dist(origin[:2], end[:2])
            if horizontal < 1:
                return None
            lift = end[2] - origin[2]
            root = speed ** 4 - gravity * (gravity * horizontal ** 2 + 2 * lift * speed ** 2)
            if root < 0:
                return None
            pitch = atan2(speed ** 2 - sqrt(root), gravity * horizontal)
            flight = horizontal / (speed * cos(pitch))
        yaw = bearing(origin, end)
        previous = origin
        for i in range(1, 9):
            t = flight * i / 8
            horizontal = speed * cos(pitch) * t
            sample = (origin[0] + horizontal * cos(yaw), origin[1] + horizontal * sin(yaw),
                      origin[2] + speed * sin(pitch) * t - gravity * t * t / 2)
            if not self.arena.clear_shot(previous, sample):
                return None
            previous = sample
        return yaw, pitch

    def triggers(self, me, shots, action):
        primary = max(shots, key=lambda shot: shot.value)
        action["aim"] = list(primary.aim)
        action["fire"] = [False, False]
        for shot in shots:
            weapon = me["weapons"][shot.slot]
            kind = weapon["kind"]
            aligned = self.aligned(me["aim"], shot, kind)
            compatible = self.aligned(primary.aim, shot, kind)
            if kind == "laser":
                charged = weapon.get("charge", 0) >= weapon["stats"]["charge"] - 0.025
                action["fire"][shot.slot] = (weapon["cooldown"] <= 0.05
                                             and (compatible or weapon.get("charge", 0) > 0)
                                             and not (charged and aligned and compatible))
            elif weapon["cooldown"] <= 0.12:
                action["fire"][shot.slot] = aligned and compatible

    @staticmethod
    def aligned(aim, shot, kind):
        yaw_error = angle_delta(aim[0], shot.aim[0])
        if kind == "lightning":
            return abs(yaw_error) <= shot.tolerance
        return hypot(yaw_error, aim[1] - shot.aim[1]) <= shot.tolerance
