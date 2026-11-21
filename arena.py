"""Queries against the static arena and a bounded ground route planner."""

from array import array
from functools import lru_cache
from heapq import heappop, heappush
from math import ceil, dist, floor, radians, sqrt, tan
from pathlib import Path
from time import perf_counter
import json
import sys


def in_ring(x, y, ring):
    inside = False
    ax, ay = ring[-2:]
    for i in range(0, len(ring), 2):
        bx, by = ring[i:i + 2]
        if (ay > y) != (by > y) and x < ax + (bx - ax) * (y - ay) / (by - ay):
            inside = not inside
        ax, ay = bx, by
    return inside


class Arena:
    GRID = 6
    BIN = 24

    def __init__(self, map_dir, mech):
        root = Path(map_dir) / "v2"
        world = json.loads((root / "world.json").read_text())
        self.bounds = world["bounds"]
        self.grid = world["terrain"]
        self.heights = array("h", (root / "terrain.i16").read_bytes())
        if sys.byteorder != "little":
            self.heights.byteswap()

        self.bins = {}
        for prism in world["prisms"]:
            ring = prism["ring"]
            xs, ys = ring[::2], ring[1::2]
            box = (min(xs), max(xs), min(ys), max(ys))
            shape = (box, prism["base"], prism["top"], ring, prism["holes"])
            for ix in range(floor(box[0] / self.BIN), floor(box[1] / self.BIN) + 1):
                for iy in range(floor(box[2] / self.BIN), floor(box[3] / self.BIN) + 1):
                    self.bins.setdefault((ix, iy), []).append(shape)
        self.slope = tan(radians(mech["max_slope_deg"]))
        self.step_up = mech["step_up"]
        self.walkable = lru_cache(maxsize=48_000)(self.walkable)

    def ground(self, x, y):
        grid = self.grid
        nx, ny = grid["nx"], grid["ny"]
        u = min(nx - 1, max(0, (x - grid["minX"]) / grid["step"]))
        v = min(ny - 1, max(0, (y - grid["minY"]) / grid["step"]))
        i, j = min(int(u), nx - 2), min(int(v), ny - 2)
        a, b = u - i, v - j
        k = j * nx + i
        h = self.heights
        return grid["scale"] * (
            (1 - b) * ((1 - a) * h[k] + a * h[k + 1])
            + b * ((1 - a) * h[k + nx] + a * h[k + nx + 1])
        )

    def solid(self, x, y, bottom, top):
        for box, base, roof, ring, holes in self.bins.get(
            (floor(x / self.BIN), floor(y / self.BIN)), ()
        ):
            if (bottom <= roof and top >= base
                    and box[0] <= x <= box[1] and box[2] <= y <= box[3]
                    and in_ring(x, y, ring)
                    and not any(in_ring(x, y, hole) for hole in holes)):
                return True
        return False

    def clear_body(self, x, y, z, radius, height):
        b = self.bounds
        if not (b["minX"] + radius <= x <= b["maxX"] - radius
                and b["minY"] + radius <= y <= b["maxY"] - radius):
            return False
        diagonal = radius / sqrt(2)
        offsets = ((0, 0), (radius, 0), (-radius, 0), (0, radius), (0, -radius),
                   (diagonal, diagonal), (-diagonal, diagonal),
                   (diagonal, -diagonal), (-diagonal, -diagonal))
        return all(not self.solid(x + dx, y + dy, z + self.step_up, z + height)
                   for dx, dy in offsets)

    def clear_shot(self, start, end):
        steps = max(1, ceil(dist(start, end) / 1.5))
        dx, dy, dz = ((b - a) / steps for a, b in zip(start, end))
        for i in range(1, steps + 1):
            x, y, z = start[0] + dx * i, start[1] + dy * i, start[2] + dz * i
            if z <= self.ground(x, y) or self.solid(x, y, z, z):
                return False
        return True

    def walkable(self, node, radius, height):
        x, y = self.point(node)
        return self.clear_body(x, y, self.ground(x, y), radius, height)

    def walk_line(self, start, end, radius, height, supported=False):
        length = dist(start[:2], end[:2])
        steps = max(1, ceil(length / 2))
        dx, dy = ((b - a) / steps for a, b in zip(start[:2], end[:2]))
        z = start[2]
        for i in range(1, steps + 1):
            x, y = start[0] + dx * i, start[1] + dy * i
            ground = self.ground(x, y)
            if ground - z > max(self.step_up, self.slope * length / steps):
                return False
            z = max(ground, start[2]) if supported else ground
            if not self.clear_body(x, y, z, radius, height):
                return False
        return True

    def point(self, node):
        return node[0] * self.GRID, node[1] * self.GRID

    def nearest_node(self, point, radius, height, connect=False):
        i, j = (round(v / self.GRID) for v in point[:2])
        candidates = [(i + dx, j + dy) for dx in range(-2, 3) for dy in range(-2, 3)]
        candidates.sort(key=lambda node: dist(point[:2], self.point(node)))
        for node in candidates:
            if self.walkable(node, radius, height):
                if not connect or self.walk_line(point, self.point(node), radius, height):
                    return node
        return None

    @lru_cache(maxsize=64_000)
    def walk_edge(self, current, neighbor, radius, height):
        point = self.point(current)
        return self.walk_line((*point, self.ground(*point)), self.point(neighbor), radius, height)

    def route(self, position, goal, radius, height, deadline):
        start = self.nearest_node(position, radius, height, connect=True)
        end = self.nearest_node(goal, radius, height)
        if start is None or end is None:
            return []

        queue = [(dist(start, end), 0, start)]
        costs = {start: 0}
        parents = {start: None}
        closest = start
        while queue and perf_counter() < deadline:
            _, cost, current = heappop(queue)
            if cost != costs[current]:
                continue
            if dist(current, end) < dist(closest, end):
                closest = current
            if current == end:
                break
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1),
                           (1, 1), (-1, 1), (1, -1), (-1, -1)):
                neighbor = current[0] + dx, current[1] + dy
                total = cost + (sqrt(2) if dx and dy else 1)
                if total >= costs.get(neighbor, float("inf")):
                    continue
                if self.walkable(neighbor, radius, height) and self.walk_edge(
                    current, neighbor, radius, height
                ):
                    costs[neighbor] = total
                    parents[neighbor] = current
                    heappush(queue, (total + 1.15 * dist(neighbor, end), total, neighbor))

        path = []
        while closest is not None:
            path.append(self.point(closest))
            closest = parents[closest]
        return path[::-1] if len(path) > 1 else []
