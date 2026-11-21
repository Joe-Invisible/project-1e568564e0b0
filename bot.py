"""Placement-focused mechfight/1 bot."""

import json
from math import cos, dist, inf, pi, sin, sqrt
from os import environ
from time import perf_counter

import mechfight as mf
from arena import Arena
from combat import Combat, angle_delta, bearing
from economy import Economy, WEAPON_VALUE
from policy import Features, Mode, Recovery, RecoveryProfile, choose


UPGRADE_RANK = {key: rank for rank, key in enumerate((
    "armor", "nanites", "gun_damage", "plating", "gun_rate", "servos",
    "laser_charge", "laser_damage", "reactor", "capacitor", "gun_velocity",
))}
VARIANTS = ("baseline", "reset", "bounded", "loot-first", "economy", "combat", "moving-farm")
ECONOMY_VARIANTS = ("economy", "combat", "moving-farm")


def direction(start, end):
    length = dist(start[:2], end[:2])
    return [(end[i] - start[i]) / length for i in range(2)] if length > 0.01 else [0, 0]


def inside(point, center, radius, margin=0):
    return dist(point[:2], center) <= max(0, radius - margin)


class Bot:
    name = "oval-tactics"
    policy = staticmethod(choose)

    def __init__(self, hello, variant="baseline", *, simulation=False, profile=None):
        if variant not in VARIANTS:
            raise ValueError(f"unknown bot policy: {variant}")
        if profile is not None and variant not in ("bounded", "loot-first"):
            raise ValueError("Recovery settings require the bounded or loot-first policy")
        if variant in ("bounded", "loot-first"):
            profile = RecoveryProfile(**(profile or {}))
        self.loot_first = variant == "loot-first"
        self.combat_matchups = variant == "combat"
        self.moving_farm = variant == "moving-farm"
        self.last_target = None
        self.recovery = Recovery(profile) if variant in ("reset", "bounded", "loot-first") else None
        self.simulation = simulation
        if variant != "baseline":
            self.name = f"oval-tactics-{variant}"
        self.ammo_fraction = profile.ammo_fraction if profile is not None else 0.25
        self.config = hello["config"]
        self.arena = Arena(hello["map"]["dir"], self.config["mech"])
        self.combat = Combat(self.arena, self.config)
        self.economy = Economy(self.arena, self.config) if variant in ECONOMY_VARIANTS else None
        self.tracks = {}
        self.last_damage = None
        self.route = []
        self.route_goal = None
        self.next_route_time = 0
        self.cover = None
        self.next_cover_time = 0
        self.jet_until = 0
        self.scan = (hello["seed"] % 360) * pi / 180

    def act(self, obs):
        deadline = inf if self.simulation else perf_counter() + 0.027
        me, now, zone = obs["self"], obs["t"], obs["zone"]
        pos, stats = me["pos"], me["stats"]
        enemies = sorted((m for m in obs["mechs"] if m["kind"] == "mech"),
                         key=lambda m: dist(m["pos"], pos))
        self.update_tracks(obs, enemies)
        center, radius = zone["next_center"], zone["next_radius"]
        edge_distance = dist(pos[:2], center) - radius
        unsafe_soon = edge_distance > -min(12, radius / 2) and (
            zone["shrink_end"] - now < max(0, edge_distance) / stats["walk_speed"] * 1.8 + 12
        )
        urgent = not me["in_zone"] or unsafe_soon
        if self.economy:
            urgent = self.economy.urgent(obs, urgent)
        pressure = sum(dist(m["pos"], pos) < 72 for m in enemies)
        under_fire = self.last_damage is not None and now - self.last_damage[0] < 2.5
        loot = self.economy.choose(obs, self.combat) if self.economy else self.loot_goal(obs)
        target, shots = self.target(me, enemies)

        remembered = None
        if target is None and self.tracks:
            remembered = min(self.tracks.values(), key=lambda track: dist(track[1], pos))[1]
        chase = target["pos"] if target else remembered
        preferred_range = self.preferred_range(me)
        target_health = target.get("hp", inf) / target.get("max_hp", 1) if target else inf
        can_pursue = (chase is not None and dist(chase, pos) <= 135
                      and inside(chase, center, radius, 8))
        features = Features(
            zone_urgent=urgent,
            health=me["hp"] / me["max_hp"],
            pressure=pressure,
            under_fire=under_fire,
            target_health=target_health,
            target_distance=dist(chase, pos) if chase is not None else inf,
            preferred_range=preferred_range,
            can_shoot=bool(shots),
            has_loot=loot is not None,
            can_pursue=can_pursue,
        )
        mode = self.policy(features)
        threat = enemies[0]["pos"] if enemies else remembered
        gun = next((w for w in me["weapons"] if w is not None and w["kind"] == "gun"), None)
        attackers = []
        recovering = False
        if self.recovery is not None:
            attackers = self.clear_threats(me, enemies)
            secondary_ready = any(me["weapons"][shot.slot]["kind"] != "gun"
                                  and me["weapons"][shot.slot]["cooldown"] <= 0.15 for shot in shots)
            reload_needed = gun is not None and (gun.get("reload", 0) > 0
                                                 or gun["ammo"] <= gun["stats"]["mag"] * self.ammo_fraction)
            was_recovering = self.recovery.started is not None
            self.recovery.update(now, features, len(attackers), reload_needed and not secondary_ready,
                                 stats["regen_delay"])
            recovering = self.recovery.started is not None
            if recovering and not was_recovering:
                self.cover = None
                self.route.clear()
            if recovering and not urgent:
                mode = Mode.DISENGAGE
            if recovering and attackers:
                threat = attackers[0]["pos"]
        if (self.loot_first and loot is not None and not urgent and not recovering
                and not under_fire and features.target_distance > 85):
            mode = Mode.LOOT
        if self.economy and loot is not None and not urgent and mode is not Mode.DISENGAGE:
            distance_to_loot = dist(pos, loot["pos"])
            farming = now < 75 and me["cubes"] < 6 and features.health > 0.55
            opening = farming and not under_fire and features.target_distance > 65
            close_cube = (loot["kind"] == "cube" and distance_to_loot < (20 if under_fire else 34)
                          and features.health > 0.55)
            capture = (loot["kind"] == "drop" and loot["holder"] == me["id"]
                       and loot["capture_s"] - loot["progress"] < 1.1 and not under_fire)
            if opening or close_cube or capture:
                mode = Mode.LOOT
        if self.economy:
            self.economy.record_progress(obs, loot if mode is Mode.LOOT else None)
        action = {"tick": obs["tick"]}
        if mode is Mode.ENGAGE:
            if self.combat_matchups:
                goal = self.matchup_move(me, target, zone, now)
            else:
                goal = self.combat_move(pos, target["pos"], preferred_range, now)
        elif mode is Mode.DISENGAGE:
            goal = self.cover_goal(obs, threat, attackers if recovering else ())
        elif mode is Mode.PURSUE and chase is not None:
            goal = chase
        elif mode is Mode.LOOT and loot is not None:
            goal = self.economy.approach(me, loot, self.combat) if self.economy else loot["pos"]
        else:
            goal = self.zone_goal(pos, zone)
            if self.economy and urgent:
                goal = self.economy.rotate(obs, goal)

        move, jump = self.move_to(me, goal, now, deadline, urgent)
        conserve = self.economy and self.economy.conserve_rotation_energy(obs, goal, urgent)
        if conserve and me["grounded"] and me["energy"] >= 36 and now >= self.jet_until:
            jump = True
        dodge = self.dodge(me, obs["projectiles"], move)
        if dodge is not None and not urgent:
            move = dodge
        dodge_laser = (self.combat_matchups and mode is Mode.ENGAGE and target.get("charging", False)
                       and abs(angle_delta(target["aim"][0], bearing(target["pos"], pos))) < 0.21)
        action.update(move=move, jump=jump)
        moving = move != [0, 0]
        if jump:
            self.jet_until = now + 1.6
        elif me["grounded"]:
            self.jet_until = 0
        if now < self.jet_until:
            action["boost"] = me["energy"] > 12
        elif me["grounded"]:
            action["boost"] = bool(moving and not conserve
                                   and (urgent or mode is Mode.DISENGAGE or dodge is not None or dodge_laser)
                                   and me["energy"] > (35 if dodge_laser else 20))

        if mode is Mode.LOOT and loot is not None and "hp" in loot:
            if not self.economy or not shots or (not under_fire and features.target_distance > 90):
                farm_shots = self.combat.solutions(me, loot)
                if farm_shots or not self.economy:
                    shots = farm_shots
        if (self.moving_farm and not shots and mode is not Mode.DISENGAGE
                and not urgent and not under_fire and features.target_distance > 65):
            shots = self.economy.passing_shot(obs, self.combat, move)
        firing = shots and mode is not Mode.DISENGAGE and not recovering
        if firing:
            self.combat.triggers(me, shots, action)
        else:
            action["aim"] = [self.look_direction(obs, threat, move), 0]
        if gun is not None and not firing:
            reload_fraction = 1.0 if recovering else 0.6
            action["reload"] = 0 < gun.get("ammo", 0) < gun["stats"]["mag"] and (
                gun["ammo"] <= gun["stats"]["mag"] * reload_fraction
            )
        self.collect(obs, action)
        return action

    def update_tracks(self, obs, enemies):
        now = obs["t"]
        for enemy in enemies:
            self.tracks[enemy["id"]] = (now, enemy["pos"])
        for event in obs["events"]:
            if self.recovery is not None and event["type"] in ("hit", "damaged"):
                self.recovery.last_exchange = now
            if event["type"] == "kill":
                self.tracks.pop(event["target"], None)
            elif (event["type"] == "damaged" and event.get("bearing") is not None
                  and (not self.economy or event.get("weapon") != "npc")):
                self.last_damage = (now, event["bearing"])
        self.tracks = {key: track for key, track in self.tracks.items() if now - track[0] < 7}

    def enemy_muzzle(self, enemy):
        pos = enemy["pos"]
        scale = 1 + self.config["cubes"]["size_per_cube"] * enemy["cubes"]
        return (*pos[:2], pos[2] + self.config["mech"]["muzzle_height"] * scale)

    def clear_threats(self, me, enemies):
        center = (*me["pos"][:2], me["pos"][2] + me["stats"]["height"] / 2)
        return [enemy for enemy in enemies if dist(enemy["pos"], me["pos"]) <= 110
                and self.arena.clear_shot(self.enemy_muzzle(enemy), center)]

    def target(self, me, enemies):
        pos = me["pos"]
        def cost(enemy):
            distance = dist(pos, enemy["pos"])
            if self.combat_matchups:
                hp = enemy.get("hp", enemy.get("max_hp", 1400))
                return distance + hp * 0.052 - 24 * (enemy["id"] == self.last_target)
            return distance + 45 * enemy.get("hp", 1) / enemy.get("max_hp", 1)

        ranked = sorted(enemies, key=cost)
        for target in ranked[:4]:
            shots = self.combat.solutions(me, target)
            if self.combat_matchups:
                shots = self.combat.counterplay(me, target, shots)
            if shots:
                self.last_target = target["id"]
                return target, shots
        return (ranked[0] if ranked else None), []

    def matchup_move(self, me, target, zone, now):
        pos, foe = me["pos"], target["pos"]
        kinds = {w["kind"] for w in me["weapons"] if w is not None}
        theirs = target.get("weapons", ())
        lightning_range = self.config["weapons"]["lightning"]["range"]
        if "lightning" in kinds and me["energy"] >= 25 and ("lightning" not in theirs or "laser" in theirs):
            low, high = 24, lightning_range - 4
        elif "laser" in kinds:
            low, high = (88, 123) if "lightning" in theirs else (72, 105)
        elif "lightning" in theirs:
            low, high = lightning_range + 15, lightning_range + 36
        elif "laser" in theirs:
            low, high = 34, 59
        else:
            low, high = 52, 78
        yaw = bearing(pos, foe)
        distance = dist(pos[:2], foe[:2])
        radial = 0
        if distance > high:
            radial = min(1, (distance - high) / 18)
        elif distance < low:
            radial = max(-1, (distance - low) / 16)
        lateral = 8 if int(now / 1.4) % 2 else -8
        dx = radial * 11 * cos(yaw) - lateral * sin(yaw)
        dy = radial * 11 * sin(yaw) + lateral * cos(yaw)
        goal = (pos[0] + dx, pos[1] + dy)
        if not inside(goal, zone["center"], zone["radius"], 5):
            inward = direction(pos, zone["center"])
            return (goal[0] + inward[0] * 10, goal[1] + inward[1] * 10)
        return goal

    def preferred_range(self, me):
        kinds = {weapon["kind"] for weapon in me["weapons"] if weapon is not None}
        if "lightning" in kinds:
            return 33
        if "mortar" in kinds or "laser" in kinds:
            return 85
        return 100 if self.loot_first else 65

    def loot_goal(self, obs):
        me, zone = obs["self"], obs["zone"]
        pos = me["pos"]
        candidates = []
        if None in me["weapons"]:
            candidates.extend((dist(pos, p["pos"]) - (34 if self.loot_first else 18), p)
                              for p in obs["pickups"]
                              if p["kind"] != "gun" and dist(pos, p["pos"]) < 75)
        candidates.extend((dist(pos, p["pos"]) - 25, p) for p in obs["cubes"]
                          if dist(pos, p["pos"]) < 55)
        candidates.extend((dist(pos, p["pos"]) + (6 if self.loot_first else 18), p)
                          for p in obs["crates"]
                          if dist(pos, p["pos"]) < (70 if self.loot_first else 45) and p["hp"] > 0
                          and self.combat.visible_shot(me, p))
        for drop in obs["drops"]:
            if drop["holder"] in (None, me["id"]) and drop["lands_in"] < 4:
                distance = dist(pos, drop["pos"])
                if distance < 70:
                    candidates.append((distance - (55 if me["hp"] < me["max_hp"] * 0.7 else 30), drop))
        for _, item in sorted(candidates, key=lambda candidate: candidate[0]):
            point = item["pos"]
            next_edge = dist(point[:2], zone["next_center"]) - zone["next_radius"]
            travel = (dist(pos, point) + 1.8 * max(0, next_edge)) / me["stats"]["walk_speed"]
            if (abs(pos[2] - point[2]) < 7
                    and inside(point, zone["center"], zone["radius"], 4)
                    and (next_edge <= -4 or zone["shrink_end"] - obs["t"] > travel + 16)):
                return item
        return None

    def collect(self, obs, action):
        me = obs["self"]
        offer = obs["offer"]
        if offer:
            action["pick"] = min(range(len(offer["choices"])),
                                 key=lambda i: UPGRADE_RANK.get(offer["choices"][i]["id"], len(UPGRADE_RANK)))
        if self.economy:
            swaps = [(WEAPON_VALUE[p["kind"]], p["id"], self.economy.wants(me["weapons"], p["kind"]))
                     for p in obs["pickups"] if
                     dist(p["pos"][:2], me["pos"][:2]) <= self.config["loot"]["pickup_radius"]
                     and abs(p["pos"][2] - me["pos"][2]) <= 4]
            best = max((swap for swap in swaps if swap[2] is not None), default=None)
            if best is not None:
                _, action["take"], slot = best
                if slot >= 0:
                    action["drop"] = slot
            return
        if None in me["weapons"]:
            choices = [p for p in obs["pickups"] if p["kind"] != "gun"
                       and dist(p["pos"][:2], me["pos"][:2]) <= self.config["loot"]["pickup_radius"]
                       and abs(p["pos"][2] - me["pos"][2]) <= 4]
            if choices:
                priority = {"laser": 0, "lightning": 1, "mortar": 2}
                action["take"] = min(choices, key=lambda p: priority[p["kind"]])["id"]

    def zone_goal(self, pos, zone):
        center, radius = zone["next_center"], zone["next_radius"]
        inward = direction(center, pos)
        staging_radius = max(0, radius - min(48, radius * 0.65))
        goal = tuple(center[i] + inward[i] * staging_radius for i in range(2))
        if dist(pos[:2], center) <= staging_radius:
            return pos[:2]
        return goal

    @staticmethod
    def combat_move(pos, target, preferred, now):
        yaw = bearing(pos, target)
        distance = dist(pos[:2], target[:2])
        lateral = 1 if int(now / 1.4) % 2 else -1
        radial = max(-1, min(1, (distance - preferred) / 22))
        return (pos[0] + 9 * (radial * cos(yaw) - lateral * sin(yaw)),
                pos[1] + 9 * (radial * sin(yaw) + lateral * cos(yaw)))

    def cover_goal(self, obs, threat, attackers=()):
        me, zone, now = obs["self"], obs["zone"], obs["t"]
        pos = me["pos"]
        if threat is None and self.last_damage and now - self.last_damage[0] < 4:
            yaw = self.last_damage[1]
            threat = (pos[0] + 65 * cos(yaw), pos[1] + 65 * sin(yaw), pos[2])
        if threat is None:
            return self.zone_goal(pos, zone)
        if self.cover is not None and now < self.next_cover_time:
            return self.cover

        self.next_cover_time = now + 0.7
        yaw = bearing(threat, pos)
        threat_eye = (*threat[:2], threat[2] + self.config["mech"]["muzzle_height"])
        firing_points = [self.enemy_muzzle(enemy) for enemy in attackers[:3]] or [threat_eye]
        my_center = (*pos[:2], pos[2] + me["stats"]["height"] / 2)
        self.cover = pos[:2]
        if not any(self.arena.clear_shot(point, my_center) for point in firing_points):
            return self.cover
        best_score = -inf
        for turn in (0, 0.6, -0.6, 1.2, -1.2, 1.8, -1.8):
            x, y = pos[0] + 24 * cos(yaw + turn), pos[1] + 24 * sin(yaw + turn)
            z = self.arena.ground(x, y)
            if not inside((x, y), zone["center"], zone["radius"], 3):
                continue
            stats = me["stats"]
            if not self.arena.clear_body(x, y, z, stats["radius"], stats["height"]):
                continue
            body = (x, y, z + stats["height"] / 2)
            blocked = sum(not self.arena.clear_shot(point, body) for point in firing_points)
            score = dist((x, y), threat[:2]) + 65 * blocked
            score -= max(0, dist((x, y), zone["next_center"]) - zone["next_radius"])
            if score > best_score:
                self.cover, best_score = (x, y), score
        return self.cover

    def move_to(self, me, goal, now, deadline, urgent):
        pos, stats = me["pos"], me["stats"]
        distance = dist(pos[:2], goal[:2])
        if distance < 1.5:
            return [0, 0], False
        radius = round(stats["radius"] + 0.22, 1)
        height = round(stats["height"], 1)
        roof = pos[2] - self.arena.ground(*pos[:2]) > 2
        direct = direction(pos, goal)
        probe = tuple(pos[i] + direct[i] * min(24, distance) for i in range(2))
        # A route can steer away from the goal before going around a long wall.
        if self.route_goal is not None and dist(self.route_goal, goal[:2]) > 18:
            self.route.clear()
        while self.route and dist(pos[:2], self.route[0]) < 2.6:
            self.route.pop(0)
        if not self.route and self.arena.walk_line(pos, probe, radius, height, supported=roof):
            return direct, False

        if not roof and not self.route and now >= self.next_route_time and perf_counter() < deadline:
            self.route = self.arena.route(
                pos, goal, radius, height, deadline, max_expansions=180 if self.simulation else None
            )
            self.route_goal = tuple(goal[:2])
            self.next_route_time = now + 0.3
            while self.route and dist(pos[:2], self.route[0]) < 2.6:
                self.route.pop(0)
        if self.route:
            waypoint = self.route[0]
            if self.arena.walk_line(pos, waypoint, radius, height):
                return direction(pos, waypoint), False
            self.route.clear()

        yaw = bearing(pos, goal)
        length = min(7, distance)
        for angle in (0, 0.55, -0.55, 1.1, -1.1, 1.65, -1.65, pi):
            move = [cos(yaw + angle), sin(yaw + angle)]
            step = tuple(pos[i] + move[i] * length for i in range(2))
            if self.arena.walk_line(pos, step, radius, height, supported=roof):
                return move, False
        return direct, me["grounded"] and me["energy"] > (25 if urgent else 55)

    def dodge(self, me, projectiles, move):
        pos, vel = me["pos"], me["vel"]
        stats = me["stats"]
        for projectile in projectiles:
            if projectile["kind"] == "mortar" or dist(projectile["pos"], pos) > 90:
                continue
            x, y, z = projectile["pos"]
            vx, vy, vz = projectile["vel"]
            dx, dy = x - pos[0], y - pos[1]
            rx, ry = vx - vel[0], vy - vel[1]
            speed_sq = rx * rx + ry * ry
            if speed_sq < 1:
                continue
            t = -(dx * rx + dy * ry) / speed_sq
            if not 0.09 < t < 0.55:
                continue
            if not -0.5 < z + (vz - vel[2]) * t - pos[2] < stats["height"] + 0.5:
                continue
            if dist((dx + rx * t, dy + ry * t), (0, 0)) > stats["radius"] + 0.6:
                continue
            speed = sqrt(speed_sq)
            side = [-ry / speed, rx / speed]
            if sum(a * b for a, b in zip(move, side)) < 0:
                side = [-side[0], -side[1]]
            for sign in (1, -1):
                step = tuple(pos[i] + sign * side[i] * 5 for i in range(2))
                if self.arena.walk_line(pos, step, stats["radius"], stats["height"]):
                    return [sign * part for part in side]
        return None

    def look_direction(self, obs, threat, move):
        if threat is not None:
            return bearing(obs["self"]["pos"], threat)
        if self.last_damage and obs["t"] - self.last_damage[0] < 3:
            return self.last_damage[1]
        for sound in obs["sounds"]:
            if sound["dist"] in ("near", "mid") and sound["kind"] in (
                "gun", "laser", "lightning", "boost", "step", "laser_charge"
            ):
                return sound["bearing"]
        if move != [0, 0]:
            return bearing((0, 0), move)
        return (self.scan + obs["t"] * 0.75 + pi) % (2 * pi) - pi


if __name__ == "__main__":
    selection = json.loads(environ["MECHFIGHT_PROFILE"]) if environ.get("MECHFIGHT_PROFILE") else {}
    variant = selection.get("variant", environ.get("MECHFIGHT_POLICY", "baseline"))
    profile = selection.get("profile")
    if variant not in VARIANTS:
        raise SystemExit(f"Unknown MECHFIGHT_POLICY: {variant}")
    if profile is not None:
        RecoveryProfile(**profile)
        if variant not in ("bounded", "loot-first"):
            raise SystemExit("Recovery settings require the bounded or loot-first policy")
    mf.run(lambda hello: Bot(hello, variant, profile=profile))
