"""Collision detection and grid-based pathfinding."""

import math
import heapq

# ── movement config (mirrors run_pynq.py section 1) ──────────────────────────
COLLISION_R = 2.0


# ── collision ─────────────────────────────────────────────────────────────────
# True if a 5-point bounding circle centred at (x, y) sits entirely in open tiles.
def _walkable(state, x, y):
    tiles = state["tiles"]
    w, h, s = state["map_w"], state["map_h"], state["tile_scale"]
    if not tiles or w <= 0 or h <= 0:
        return True
    for dx, dy in [(0,0),(COLLISION_R,0),(-COLLISION_R,0),(0,COLLISION_R),(0,-COLLISION_R)]:
        col = int(math.floor((x + dx) / s + w / 2.0))
        row = int(math.floor((y + dy) / s + h / 2.0))
        if not (0 <= col < w and 0 <= row < h):
            return False
        if tiles[row * w + col]:
            return False
    return True

# Slide along walls: try full move, then axis-only fallbacks, then cancel.
def _resolve_move(state, nx, ny):
    if _walkable(state, nx, ny):          return nx, ny
    if _walkable(state, nx, state["y"]):  return nx, state["y"]
    if _walkable(state, state["x"], ny):  return state["x"], ny
    return state["x"], state["y"]


# Convert grid cell (col, row) to world-space centre position.
def _cell_to_world(col, row, width, height, tile_scale):
    return (
        (col - width / 2.0 + 0.5) * tile_scale,
        (row - height / 2.0 + 0.5) * tile_scale,
    )


# Convert world position to (col, row); returns None if out of map bounds.
def _world_to_cell(state, x, y):
    map_w = state["map_w"]
    map_h = state["map_h"]
    tile_scale = state["tile_scale"]
    if map_w <= 0 or map_h <= 0 or tile_scale <= 0:
        return None
    col = int(math.floor((x / tile_scale) + (map_w / 2.0)))
    row = int(math.floor((y / tile_scale) + (map_h / 2.0)))
    if col < 0 or row < 0 or col >= map_w or row >= map_h:
        return None
    return (col, row)


# True if the tile at (col, row) is floor (value 0) and within map bounds.
def _cell_is_open(state, col, row):
    map_w = state["map_w"]
    map_h = state["map_h"]
    tiles = state["tiles"]
    if col < 0 or row < 0 or col >= map_w or row >= map_h:
        return False
    if not tiles:
        return True
    return tiles[row * map_w + col] == 0


# BFS outward from world position to find the nearest open (floor) cell.
def _nearest_open_cell(state, x, y):
    origin = _world_to_cell(state, x, y)
    map_w = state["map_w"]
    map_h = state["map_h"]
    tile_scale = state["tile_scale"]
    if map_w <= 0 or map_h <= 0:
        return None

    if origin and _cell_is_open(state, origin[0], origin[1]):
        return origin

    if origin is None:
        guess_col = min(map_w - 1, max(0, int(round((x / tile_scale) + (map_w / 2.0) - 0.5))))
        guess_row = min(map_h - 1, max(0, int(round((y / tile_scale) + (map_h / 2.0) - 0.5))))
        origin = (guess_col, guess_row)

    max_radius = max(map_w, map_h)
    for radius in range(1, max_radius + 1):
        row_min = max(0, origin[1] - radius)
        row_max = min(map_h - 1, origin[1] + radius)
        col_min = max(0, origin[0] - radius)
        col_max = min(map_w - 1, origin[0] + radius)
        for row in range(row_min, row_max + 1):
            for col in range(col_min, col_max + 1):
                if abs(col - origin[0]) != radius and abs(row - origin[1]) != radius:
                    continue
                if _cell_is_open(state, col, row):
                    return (col, row)
    return None


def _build_cell_path(state, start_cell, goal_cell):
    if start_cell is None or goal_cell is None:
        return []
    if start_cell == goal_cell:
        return [start_cell]

    map_w = state["map_w"]
    map_h = state["map_h"]
    if map_w <= 0 or map_h <= 0:
        return []

    open_heap = []
    heapq.heappush(open_heap, (0, 0, start_cell))
    came_from = {}
    g_score = {start_cell: 0}
    closed = set()

    while open_heap:
        _, cost_so_far, current = heapq.heappop(open_heap)
        if current in closed:
            continue
        if current == goal_cell:
            path = [current]
            while current in came_from:
                current = came_from[current]
                path.append(current)
            path.reverse()
            return path

        closed.add(current)
        col, row = current
        for next_col, next_row in (
            (col + 1, row),
            (col - 1, row),
            (col, row + 1),
            (col, row - 1),
        ):
            if not _cell_is_open(state, next_col, next_row):
                continue
            neighbour = (next_col, next_row)
            next_cost = cost_so_far + 1
            if next_cost >= g_score.get(neighbour, 1_000_000):
                continue
            came_from[neighbour] = current
            g_score[neighbour] = next_cost
            heuristic = abs(goal_cell[0] - next_col) + abs(goal_cell[1] - next_row)
            heapq.heappush(open_heap, (next_cost + heuristic, next_cost, neighbour))

    return []


def _path_step_target(state, current_x, current_y, target_x, target_y):
    if state["map_w"] <= 0 or not state["tiles"]:
        return (target_x, target_y)

    start_cell = _nearest_open_cell(state, current_x, current_y)
    goal_cell = _nearest_open_cell(state, target_x, target_y)
    path = _build_cell_path(state, start_cell, goal_cell)
    if len(path) >= 2:
        return _cell_to_world(path[1][0], path[1][1], state["map_w"], state["map_h"], state["tile_scale"])
    if len(path) == 1:
        return _cell_to_world(path[0][0], path[0][1], state["map_w"], state["map_h"], state["tile_scale"])
    return (target_x, target_y)
