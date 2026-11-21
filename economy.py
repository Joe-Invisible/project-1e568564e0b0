"""Opening objectives and safe-circle routing for the economy policy."""

from math import atan2, cos, dist, inf, pi, sin


WEAPON_VALUE = {"gun": 0, "mortar": 1, "lightning": 2, "laser": 3}


class Economy:
    def __init__(self, arena, config):
        self.arena = arena
        self.config = config
        self.items = {}
        self.rotation = None
        self.waypoint = None
        self.next_waypoint_time = 0
        self.objective = None
        self.revisit_after = {}
        self.rotation_sample = None
        self.stalled = False

    def update(self, obs):
        now, pos = obs["t"], obs["self"]["pos"]
        seen = {item["id"]: item for item in obs["pickups"]}
        seen.update({c["id"]: {**c, "kind": "cube"} for c in obs["cubes"]})
        self.items = {
            ident: (time, item) for ident, (time, item) in self.items.items()
            if now - time < 18 and dist(pos, item["pos"]) > 17
        }
        self.items.update((ident, (now, item)) for ident, item in seen.items())
        for event in obs["events"]:
            if event["type"] in ("took", "take_failed"):
                self.items.pop(event["id"], None)

    @staticmethod
    def wants(weapons, kind):
        if kind == "gun" or any(w and w["kind"] == kind for w in weapons):
            return None
        if None in weapons:
            return -1
        return next((i for i, w in enumerate(weapons)
                     if w["kind"] != "gun" and w.get("charge", 0) <= 0
                     and WEAPON_VALUE[kind] > WEAPON_VALUE[w["kind"]]), None)

    def choose(self, obs, combat):
        self.update(obs)
        me, now, zone = obs["self"], obs["t"], obs["zone"]
        pos, stats = me["pos"], me["stats"]
        candidates = []
        opening = now < 75 and me["cubes"] < 6
        health = me["hp"] / me["max_hp"]
        for _, item in self.items.values():
            kind = item["kind"]
            distance = dist(pos, item["pos"])
            if kind == "cube":
                if distance <= 85:
                    candidates.append((distance - (63 if opening else 43), item))
            elif distance <= 100 and self.wants(me["weapons"], kind) is not None:
                value = (23, 12, 36, 63)[WEAPON_VALUE[kind]]
                if None in me["weapons"]:
                    value += 14
                candidates.append((distance - value, item))
        if health >= 0.50:
            for crate in obs["crates"]:
                distance = dist(pos, crate["pos"])
                if distance <= (105 if opening else 75) and crate["hp"] > 0:
                    if combat.visible_shot(me, crate) or distance <= 55:
                        cost = distance * 0.65 + 0.06 * crate["hp"]
                        candidates.append((cost - (24 if opening else 6), {**crate, "kind": "crate"}))
        if health >= 0.6:
            for drone in obs["mechs"]:
                distance = dist(pos, drone["pos"])
                if (drone["kind"] == "npc" and drone.get("hp", 350) > 0 and distance <= 112
                        and combat.visible_shot(me, drone)):
                    cost = distance * 0.60 + 0.06 * drone.get("hp", 350)
                    candidates.append((cost - (42 if opening else 20), drone))
        for drop in obs["drops"]:
            distance = dist(pos, drop["pos"])
            if (distance <= 150 and drop["holder"] in (None, me["id"])
                    and drop["lands_in"] <= distance / stats["walk_speed"] + 3):
                value = 70 + 38 * (1 - health) + min(16, drop["progress"] * 4)
                candidates.append((distance - value, {**drop, "kind": "drop"}))
        candidates.sort(key=lambda item: item[0])
        self.revisit_after = {ident: until for ident, until in self.revisit_after.items() if now < until}
        for _, item in candidates:
            if item["id"] in self.revisit_after:
                continue
            point = item["pos"]
            separation = dist(pos, point)
            current_edge = dist(point[:2], zone["center"]) - zone["radius"]
            next_edge = dist(point[:2], zone["next_center"]) - zone["next_radius"]
            travel = (separation + 1.8 * max(0, next_edge)) / stats["walk_speed"]
            if (abs(pos[2] - point[2]) <= 5 and current_edge <= -3
                    and (next_edge <= -4 or zone["shrink_end"] - now > travel + 17)):
                return item
        return None

    def record_progress(self, obs, item):
        if item is None:
            self.objective = None
            return
        now = obs["t"]
        ident = item["id"]
        distance = dist(obs["self"]["pos"], item["pos"])
        hp = item.get("hp", 0)
        capture = item.get("progress", 0)
        if self.objective is not None and self.objective[0] == ident:
            _, then, previous_distance, previous_hp, previous_capture = self.objective
            if distance >= previous_distance - 3 and hp >= previous_hp and capture <= previous_capture:
                if now - then >= 7:
                    self.revisit_after[ident] = now + 12
                    self.objective = None
                return
        self.objective = (ident, now, distance, hp, capture)

    def approach(self, me, item, combat):
        pos, point, stats = me["pos"], item["pos"], me["stats"]
        kind = item["kind"]
        distance = dist(pos[:2], point[:2])
        if kind == "drop":
            return pos[:2] if distance < item["radius"] - 2 else point[:2]
        if kind not in ("crate", "npc"):
            reach = self.config["cubes" if kind == "cube" else "loot"]["pickup_radius"]
            if distance < reach - 0.5 and abs(point[2] - pos[2]) <= 4:
                return pos[:2]
            standoff = max(0, reach - 2.0)
        else:
            standoff = 21 if kind == "crate" else 54
            if standoff - 8 <= distance <= standoff + 12 and combat.visible_shot(me, item):
                return pos[:2]

        yaw = atan2(pos[1] - point[1], pos[0] - point[0])
        fallback = point[:2]
        best = inf
        for delta in (0, 0.6, -0.6, 1.2, -1.2, pi):
            x, y = point[0] + standoff * cos(yaw + delta), point[1] + standoff * sin(yaw + delta)
            z = self.arena.ground(x, y)
            if kind not in ("crate", "npc") and abs(point[2] - z) > 4:
                continue
            if not self.arena.clear_body(x, y, z, stats["radius"] + 0.15, stats["height"]):
                continue
            if kind in ("crate", "npc"):
                target_height = 1.2 if kind == "crate" else 1.75
                if not self.arena.clear_shot((x, y, z + stats["muzzle_height"]),
                                             (*point[:2], point[2] + target_height)):
                    continue
            score = dist(pos[:2], (x, y))
            if score < best:
                best, fallback = score, (x, y)
        if best == inf and kind in ("crate", "npc") and distance < standoff:
            # Continue backing away if the ideal ring is blocked but a closer step is available.
            fallback = (pos[0] + 8 * cos(yaw), pos[1] + 8 * sin(yaw))
        return fallback

    def urgent(self, obs, unsafe):
        me, zone = obs["self"], obs["zone"]
        circle = (*zone["next_center"], zone["next_radius"])
        edge = dist(me["pos"][:2], circle[:2]) - circle[2]
        margin = min(22, circle[2] * 0.5)
        if self.rotation is not None and self.rotation != circle:
            self.rotation = self.waypoint = None
        travel = max(0, edge + margin) / me["stats"]["walk_speed"] * 1.9
        deadline = zone["shrink_start"] if obs["t"] < zone["shrink_start"] else zone["shrink_end"]
        if unsafe or (edge > -margin and deadline - obs["t"] < travel + 12):
            self.rotation = circle
        elif edge <= -margin:
            self.rotation = self.waypoint = None
        return self.rotation is not None

    def rotate(self, obs, fallback):
        if self.rotation is None:
            return fallback
        me, now = obs["self"], obs["t"]
        pos, stats = me["pos"], me["stats"]
        if self.waypoint is not None and now < self.next_waypoint_time:
            return self.waypoint
        self.next_waypoint_time = now + 1.0
        cx, cy, radius = self.rotation
        yaw = atan2(pos[1] - cy, pos[0] - cx)
        ring = max(0, radius - min(27, radius * 0.7))
        best_score = inf
        self.waypoint = fallback
        for delta in (0, 0.2, -0.2, 0.45, -0.45, 0.75, -0.75):
            x, y = cx + ring * cos(yaw + delta), cy + ring * sin(yaw + delta)
            z = self.arena.ground(x, y)
            if not self.arena.clear_body(x, y, z, stats["radius"] + 0.2, stats["height"]):
                continue
            length = dist(pos[:2], (x, y))
            p = min(1, 26 / max(1, length))
            probe = (pos[0] + p * (x - pos[0]), pos[1] + p * (y - pos[1]))
            clear = self.arena.walk_line(pos, probe, stats["radius"] + 0.2, stats["height"])
            score = length + (0 if clear else 42)
            if score < best_score:
                self.waypoint, best_score = (x, y), score
        return self.waypoint

    def conserve_rotation_energy(self, obs, goal, urgent):
        now, me = obs["t"], obs["self"]
        distance = dist(me["pos"][:2], goal[:2])
        if not urgent or distance < 12:
            self.rotation_sample = None
            self.stalled = False
            return False
        if self.rotation_sample is None or dist(goal[:2], self.rotation_sample[2]) > 18:
            self.rotation_sample = (now, distance, goal[:2])
        elif now - self.rotation_sample[0] >= 2:
            self.stalled = distance > self.rotation_sample[1] - 5
            self.rotation_sample = (now, distance, goal[:2])
        return self.stalled
