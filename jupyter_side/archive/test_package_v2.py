#!/usr/bin/env python3

import argparse
import heapq
import math
import os
import socket
import struct
import time

try:
    from pynq import Overlay
    from pynq.ps import Clocks
except ImportError:
    Overlay = None
    Clocks = None

import protocol


# Keep protocol.py beside this file on the board.
#
# Copy to board:
#   scp jupyter_side/test_package_v2.py pynq_full/interfacing/protocol.py \
#       xilinx@<PYNQ_IP>:/home/xilinx/jupyter_notebooks/
#
# Run on board:
#   cd /home/xilinx/jupyter_notebooks
#   python3 test_package_v2.py --mode auto


# --- Network configuration ---
SERVER_IP = "3.9.71.204"
SERVER_PORT = 9000
USERNAME = ""
MODE = "manual"
PREFERRED_ROLE = protocol.ROLE_ANY
TICK_RATE = 20
TICK_INTERVAL = 1.0 / TICK_RATE
REGISTER_RETRY_S = 2.0
SERVER_SILENCE_S = 5.0
STATE_LOG_PERIOD_S = 1.0
SOCKET_RECV_SIZE = 4096


# --- Hardware configuration ---
OVERLAY_PATH = "/home/xilinx/jupyter_notebooks/Final_project_test/design_1_wrapper.bit"
CLOCK_MHZ = 50.0
HW_MAP_ROWS = 32
HW_MAP_COLS = 32
HW_PLAYER_POS_OFFSET = 32 * 4
HW_PLAYER_ANGLE_OFFSET = 33 * 4
HW_COORD_FRAC_BITS = 10
HW_ANGLE_STEPS = 1 << 12
HW_ANGLE_MASK = HW_ANGLE_STEPS - 1


# --- Local control configuration ---
INITIAL_X = 0.0
INITIAL_Y = 0.0
INITIAL_ANGLE_RAW = 0
MOVE_SPEED = 0.2
TURN_STEP = 64
PLAYER_COLLISION_RADIUS = 2.5
AUTO_RUNNER_SPEED = 0.2
AUTO_TAGGER_SPEED = 0.26
AUTO_FALLBACK_SPEED = 0.18
AUTO_RUNNER_EVADE_DISTANCE = 42.0
AUTO_TAGGER_SHOOT_RANGE = 26.0
AUTO_TAGGER_SHOOT_ARC = 0.4
AUTO_TAGGER_SHOOT_PERIOD_TICKS = 4
SERVER_POSE_SNAP_DISTANCE = 1.5
SERVER_POSE_SNAP_ANGLE = 0.6
SERVER_POSE_RECONCILE_BLEND = 0.35
SERVER_POSE_RECONCILE_DEADBAND = 0.05
SERVER_POSE_RECONCILE_ANGLE_DEADBAND = math.radians(2.0)
BUTTON_TURN_RIGHT_MASK = 1 << 0
BUTTON_BACKWARD_MASK = 1 << 1
BUTTON_FORWARD_MASK = 1 << 2
BUTTON_TURN_LEFT_MASK = 1 << 3


# Convert a runtime mode name into the board client's internal mode label.
def _normalise_mode_name(value: str) -> str:
    return "auto" if str(value).lower() == "auto" else "manual"


# Convert a wire-level node control mode byte into a runtime mode label.
def _mode_name_from_packet(mode_value: int) -> str:
    return (
        "auto"
        if mode_value == protocol.NODE_CONTROL_MODE_AUTO
        else "manual"
    )


# Convert a server player id into a readable role label for logs.
def _role_name(player_id: int) -> str:
    if player_id == 0:
        return "LOBBY"
    if player_id == 1:
        return "RUNNER"
    if player_id == 2:
        return "TAGGER"
    return f"PLAYER_{player_id}"


# Summarise a raw UDP packet header for debug output.
def _describe_packet(data: bytes) -> str:
    if len(data) < protocol.HEADER_SIZE:
        return f"short packet ({len(data)} bytes)"
    pkt_type, seq, timestamp = protocol.unpack_header(data)
    return f"type=0x{pkt_type:04x} seq={seq} ts={timestamp} len={len(data)}"


# Pack a flat 32x32 tile map into BRAM row words for the FPGA.
def _encode_map_rows_for_bram(width: int, height: int, tiles: bytes):
    if width != HW_MAP_COLS or height != HW_MAP_ROWS:
        raise ValueError(
            f"hardware expects a {HW_MAP_COLS}x{HW_MAP_ROWS} map, got {width}x{height}"
        )
    if len(tiles) != width * height:
        raise ValueError(f"expected {width * height} tile bytes, got {len(tiles)}")

    rows = []
    for row in range(height):
        word = 0
        base = row * width
        for col in range(width):
            if tiles[base + col]:
                # Hardware map rows treat the leftmost tile as the MSB.
                word |= 1 << (width - 1 - col)
        rows.append(word)
    return rows


# Convert world-space into the unsigned Q6.10 coordinate format used by hardware.
def _world_to_hw_q6_10(value: float, tile_scale: int, map_dim: int) -> int:
    tile_units = (value / tile_scale) + (map_dim / 2.0)
    raw = int(round(tile_units * (1 << HW_COORD_FRAC_BITS)))
    max_raw = (map_dim << HW_COORD_FRAC_BITS) - 1
    return max(0, min(max_raw, raw))


# Convert a radians angle into the 12-bit turn value used by hardware.
def _radians_to_hw_angle(angle_radians: float) -> int:
    turn = angle_radians % (2.0 * math.pi)
    return int(round(turn * HW_ANGLE_STEPS / (2.0 * math.pi))) & HW_ANGLE_MASK


# Load the bitstream and expose BRAM and button GPIO handles.
def _load_overlay(overlay_path: str):
    if Overlay is None or Clocks is None:
        raise SystemExit(
            "test_package_v2.py must run on the PYNQ board with the pynq package installed"
        )
    print(f"[HW] loading overlay {overlay_path}")
    overlay = Overlay(overlay_path)
    bram = overlay.axi_bram_ctrl_0
    buttons = overlay.axi_gpio_0.channel1
    Clocks.fclk0_mhz = CLOCK_MHZ
    time.sleep(0.1)
    print(f"[HW] overlay ready, fclk0={CLOCK_MHZ:.1f}MHz")
    return overlay, bram, buttons


# Write the current map tiles into the FPGA-visible BRAM rows.
def _write_map_to_bram(bram, width: int, height: int, tiles: bytes):
    rows = _encode_map_rows_for_bram(width, height, tiles)
    for row_index, word in enumerate(rows):
        bram.write(row_index * 4, int(word) & 0xFFFFFFFF)
    print(f"[HW] wrote {len(rows)} map rows to BRAM ({width}x{height})")


# Mirror the local player pose into the BRAM control registers.
def _write_pose_to_bram(bram, state: dict):
    x_q = _world_to_hw_q6_10(state["x"], state["tile_scale"], state["map_w"])
    y_q = _world_to_hw_q6_10(state["y"], state["tile_scale"], state["map_h"])
    bram.write(HW_PLAYER_POS_OFFSET, ((x_q & 0xFFFF) << 16) | (y_q & 0xFFFF))
    bram.write(HW_PLAYER_ANGLE_OFFSET, state["angle_raw"] & HW_ANGLE_MASK)


# Check whether a world-space point and collision radius are clear of walls.
def _is_walkable(state: dict, x: float, y: float, radius: float = PLAYER_COLLISION_RADIUS) -> bool:
    tiles = state["tiles"]
    map_w = state["map_w"]
    map_h = state["map_h"]
    tile_scale = state["tile_scale"]
    if not tiles or map_w <= 0 or map_h <= 0 or tile_scale <= 0:
        return True

    offsets = [(0.0, 0.0)]
    if radius > 0.0:
        offsets.extend([
            (radius, 0.0), (-radius, 0.0),
            (0.0, radius), (0.0, -radius),
            (radius, radius), (radius, -radius),
            (-radius, radius), (-radius, -radius),
        ])

    for dx, dy in offsets:
        col = int(math.floor(((x + dx) / tile_scale) + (map_w / 2.0)))
        row = int(math.floor(((y + dy) / tile_scale) + (map_h / 2.0)))
        if col < 0 or row < 0 or col >= map_w or row >= map_h:
            return False
        if tiles[row * map_w + col]:
            return False
    return True


# Clamp a desired move back to the nearest safe walkable position.
def _resolve_move(state: dict, desired_x: float, desired_y: float):
    if _is_walkable(state, desired_x, desired_y):
        return desired_x, desired_y
    if _is_walkable(state, desired_x, state["y"]):
        return desired_x, state["y"]
    if _is_walkable(state, state["x"], desired_y):
        return state["x"], desired_y
    if not _is_walkable(state, state["x"], state["y"]):
        return state["x"], state["y"]

    low_x, low_y = state["x"], state["y"]
    high_x, high_y = desired_x, desired_y
    for _ in range(10):
        mid_x = (low_x + high_x) / 2.0
        mid_y = (low_y + high_y) / 2.0
        if _is_walkable(state, mid_x, mid_y):
            low_x, low_y = mid_x, mid_y
        else:
            high_x, high_y = mid_x, mid_y
    return low_x, low_y


# Convert a tile cell back into the world-space centre of that cell.
def _cell_to_world(col: int, row: int, width: int, height: int, tile_scale: int):
    return (
        (col - width / 2.0 + 0.5) * tile_scale,
        (row - height / 2.0 + 0.5) * tile_scale,
    )


# Convert a world-space position into a tile-grid cell.
def _world_to_cell(state: dict, x: float, y: float):
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


# Test whether a given grid cell is inside the map and not a wall.
def _cell_is_open(state: dict, col: int, row: int) -> bool:
    map_w = state["map_w"]
    map_h = state["map_h"]
    tiles = state["tiles"]
    if col < 0 or row < 0 or col >= map_w or row >= map_h:
        return False
    if not tiles:
        return True
    return tiles[row * map_w + col] == 0


# Find the nearest open cell to a world-space position for pathfinding start/goal snapping.
def _nearest_open_cell(state: dict, x: float, y: float):
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


# Run a small 4-way A* search across the 32x32 grid.
def _build_cell_path(state: dict, start_cell, goal_cell):
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


# Reduce a distant target to the next cell-centre waypoint along the path.
def _path_step_target(state: dict, current_x: float, current_y: float,
                      target_x: float, target_y: float):
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


# Normalise an angle onto the [-pi, pi] range.
def _wrap_angle(angle: float) -> float:
    while angle <= -math.pi:
        angle += math.pi * 2.0
    while angle > math.pi:
        angle -= math.pi * 2.0
    return angle


# Sample a few steering offsets and keep the least-blocked step toward a target.
def _choose_best_step_towards(state: dict, x: float, y: float, angle: float, target, move_speed: float):
    desired_angle = math.atan2(target[1] - y, target[0] - x)
    best = None
    offsets = (0.0, 0.35, -0.35, 0.7, -0.7, 1.05, -1.05, math.pi)
    for offset in offsets:
        candidate_angle = _wrap_angle(desired_angle + offset)
        desired_x = x + move_speed * math.cos(candidate_angle)
        desired_y = y + move_speed * math.sin(candidate_angle)
        next_x, next_y = _resolve_move(state, desired_x, desired_y)
        blocked = next_x == x and next_y == y
        score = ((next_x - target[0]) ** 2 + (next_y - target[1]) ** 2) + (abs(offset) * 3.0)
        if blocked:
            score += 1_000_000
        candidate = (score, next_x, next_y, candidate_angle)
        if best is None or candidate < best:
            best = candidate
    if best is None:
        return x, y, angle, desired_angle
    return best[1], best[2], best[3], desired_angle


# Return only the bit positions that are still active in the current mask.
def _active_bit_positions(state: dict):
    positions = []
    for index, bit in enumerate(state["bits"]):
        if bit is None:
            continue
        if (state["bits_mask"] & (1 << index)) == 0:
            continue
        positions.append(bit)
    return positions


# Build a retreat target that moves away from a threat with a slight lateral bias.
def _compute_evade_target(x: float, y: float, threat_x: float, threat_y: float, distance: float):
    away_x = x - threat_x
    away_y = y - threat_y
    length = math.hypot(away_x, away_y)
    if length < 0.001:
        away_x, away_y, length = 1.0, 0.0, 1.0
    away_x /= length
    away_y /= length
    lateral_x = -away_y
    lateral_y = away_x
    return (
        x + away_x * distance + lateral_x * (distance * 0.35),
        y + away_y * distance + lateral_y * (distance * 0.2),
    )


# Choose the current auto objective based on role, mode, bits, and opponent state.
def _choose_auto_objective(state: dict):
    player_id = state["player_id"]
    players = [
        player for player in state["players"]
        if not (player.get("flags", 0) & protocol.FLAG_MATCH_END)
    ]
    runner = next((player for player in players if player.get("player_id") == 1), None)
    tagger = next((player for player in players if player.get("player_id") == 2), None)
    x = state["x"]
    y = state["y"]

    if player_id == 2 and runner:
        return {"mode": "chase", "target": (float(runner["x"]), float(runner["y"]))}

    if player_id == 1:
        if tagger:
            tagger_dx = float(tagger["x"]) - x
            tagger_dy = float(tagger["y"]) - y
            tagger_dist = math.hypot(tagger_dx, tagger_dy)
            if tagger_dist <= AUTO_RUNNER_EVADE_DISTANCE:
                return {
                    "mode": "evade",
                    "target": _compute_evade_target(
                        x, y, float(tagger["x"]), float(tagger["y"]), AUTO_RUNNER_EVADE_DISTANCE,
                    ),
                }
        if state["game_mode"] == protocol.GAME_MODE_CHASE_BITS:
            candidates = _active_bit_positions(state)
            if candidates:
                target = min(
                    candidates,
                    key=lambda bit: (float(bit[0]) - x) ** 2 + (float(bit[1]) - y) ** 2,
                )
                return {"mode": "collect", "target": (float(target[0]), float(target[1]))}
        if tagger:
            return {
                "mode": "kite",
                "target": _compute_evade_target(
                    x, y, float(tagger["x"]), float(tagger["y"]), AUTO_RUNNER_EVADE_DISTANCE * 0.7,
                ),
            }

    return {"mode": "roam", "target": None}


# Advance one auto-controlled tick for the board client.
def _apply_auto_input(state: dict) -> None:
    state["input_flags"] = 0
    if not state["registered"] or state["match_ended"] or state["player_id"] in (None, 0):
        return

    objective = _choose_auto_objective(state)
    mode = objective["mode"]
    target = objective["target"]

    if mode == "chase":
        move_speed = float(state.get("auto_tagger_speed", AUTO_TAGGER_SPEED))
    elif mode in {"evade", "collect", "kite"}:
        move_speed = float(state.get("auto_runner_speed", AUTO_RUNNER_SPEED))
    else:
        move_speed = float(state.get("auto_fallback_speed", AUTO_FALLBACK_SPEED))

    if target is None:
        roam_target = (
            state["x"] + move_speed * 3.0 * math.cos(state["angle"]),
            state["y"] + move_speed * 3.0 * math.sin(state["angle"]),
        )
        next_x, next_y, next_angle, _ = _choose_best_step_towards(
            state, state["x"], state["y"], state["angle"], roam_target, move_speed,
        )
        state["x"], state["y"], state["angle"] = next_x, next_y, next_angle
        state["angle_raw"] = _radians_to_hw_angle(next_angle)
        return

    nav_target = _path_step_target(state, state["x"], state["y"], target[0], target[1])
    next_x, next_y, next_angle, desired_angle = _choose_best_step_towards(
        state, state["x"], state["y"], state["angle"], nav_target, move_speed,
    )
    state["x"], state["y"], state["angle"] = next_x, next_y, next_angle
    state["angle_raw"] = _radians_to_hw_angle(next_angle)

    if mode == "chase":
        distance = math.hypot(target[0] - state["x"], target[1] - state["y"])
        aligned = abs(_wrap_angle(desired_angle - next_angle)) <= float(state.get("auto_tagger_shoot_arc", AUTO_TAGGER_SHOOT_ARC))
        shoot_range = float(state.get("auto_tagger_shoot_range", AUTO_TAGGER_SHOOT_RANGE))
        shoot_period = int(state.get("auto_tagger_shoot_period_ticks", AUTO_TAGGER_SHOOT_PERIOD_TICKS))
        if aligned and distance <= shoot_range and (state["tick"] % max(1, shoot_period) == 0):
            state["input_flags"] = protocol.FLAG_SHOOTING


# Poll the FPGA buttons and apply one manual movement tick.
def _apply_manual_input(state: dict, buttons) -> None:
    raw = buttons.read() & 0xF
    state["input_flags"] = 0
    turn_step = int(state.get("turn_step", TURN_STEP))
    if raw & BUTTON_TURN_LEFT_MASK:
        state["angle_raw"] = (state["angle_raw"] + turn_step) % HW_ANGLE_STEPS
    if raw & BUTTON_TURN_RIGHT_MASK:
        state["angle_raw"] = (state["angle_raw"] - turn_step) % HW_ANGLE_STEPS

    state["angle"] = (state["angle_raw"] * (2.0 * math.pi / HW_ANGLE_STEPS)) % (2.0 * math.pi)

    move_step = 0.0
    move_speed = float(state.get("move_speed", MOVE_SPEED))
    if raw & BUTTON_FORWARD_MASK:
        move_step += move_speed
    if raw & BUTTON_BACKWARD_MASK:
        move_step -= move_speed
    if move_step == 0.0:
        return

    desired_x = state["x"] + move_step * math.cos(state["angle"])
    desired_y = state["y"] + move_step * math.sin(state["angle"])
    state["x"], state["y"] = _resolve_move(state, desired_x, desired_y)


# Send a registration packet so the server can place this board in the lobby.
def _send_register(sock: socket.socket, server_address, state: dict):
    pkt = protocol.pack_register_packet(
        seq=state["seq"],
        x=state["x"],
        y=state["y"],
        angle=state["angle"],
        preferred_role=PREFERRED_ROLE,
        username=USERNAME,
        movement_mode=protocol.MOVEMENT_MODE_POSE,
    )
    sock.sendto(pkt, server_address)
    print(
        f"[TX] REGISTER seq={state['seq']} pose=({state['x']:.2f}, {state['y']:.2f}, {state['angle']:.2f}) "
        f"username={USERNAME or '<none>'}"
    )
    state["seq"] = (state["seq"] + 1) & 0xFFFF
    state["last_register_tx_at"] = time.monotonic()


# Send either a live state update or a heartbeat when the match has ended.
def _send_state(sock: socket.socket, server_address, state: dict):
    pkt_type = protocol.PKT_HEARTBEAT if state["match_ended"] else protocol.PKT_STATE_UPDATE
    pkt = protocol.pack_node_packet(
        pkt_type=pkt_type,
        seq=state["seq"],
        x=state["x"],
        y=state["y"],
        angle=state["angle"],
        flags=state.get("input_flags", 0),
        movement_mode=protocol.MOVEMENT_MODE_POSE,
    )
    sock.sendto(pkt, server_address)
    state["seq"] = (state["seq"] + 1) & 0xFFFF


# Snap the local authoritative pose from the latest server player snapshot.
def _update_local_pose_from_server(state: dict, players) -> None:
    player_id = state["player_id"]
    if player_id is None:
        return
    for player in players:
        if player["player_id"] != player_id:
            continue
        server_x = float(player["x"])
        server_y = float(player["y"])
        server_angle = float(player["angle"])
        server_match_end = bool(player["flags"] & protocol.FLAG_MATCH_END)
        distance_error = math.hypot(server_x - state["x"], server_y - state["y"])
        angle_error = abs(_wrap_angle(server_angle - state["angle"]))
        should_snap = (
            state.get("force_server_pose_sync", False)
            or player_id == 0
            or server_match_end
            or distance_error >= float(state.get("server_pose_snap_distance", SERVER_POSE_SNAP_DISTANCE))
            or angle_error >= float(state.get("server_pose_snap_angle", SERVER_POSE_SNAP_ANGLE))
        )
        if should_snap:
            state["x"] = server_x
            state["y"] = server_y
            state["angle"] = server_angle
            state["angle_raw"] = _radians_to_hw_angle(server_angle)
            state["force_server_pose_sync"] = False
        else:
            blend = min(
                1.0,
                max(0.0, float(state.get("server_pose_reconcile_blend", SERVER_POSE_RECONCILE_BLEND))),
            )
            deadband = max(
                0.0,
                float(state.get("server_pose_reconcile_deadband", SERVER_POSE_RECONCILE_DEADBAND)),
            )
            angle_deadband = float(
                state.get("server_pose_reconcile_angle_deadband", SERVER_POSE_RECONCILE_ANGLE_DEADBAND)
            )
            if distance_error > deadband:
                state["x"] += (server_x - state["x"]) * blend
                state["y"] += (server_y - state["y"]) * blend
            else:
                state["x"] = server_x
                state["y"] = server_y
            if angle_error > angle_deadband:
                state["angle"] = _wrap_angle(state["angle"] + _wrap_angle(server_angle - state["angle"]) * blend)
            else:
                state["angle"] = server_angle
            state["angle_raw"] = _radians_to_hw_angle(state["angle"])
        state["match_ended"] = server_match_end
        return


# Decode one server packet and update local board/runtime state from it.
def _handle_packet(data: bytes, state: dict, bram) -> None:
    pkt_type, seq, timestamp = protocol.unpack_header(data)
    state["last_rx_at"] = time.monotonic()

    if pkt_type == protocol.PKT_ACK:
        if len(data) < protocol.HEADER_SIZE + 1:
            print(f"[ACK] malformed: {_describe_packet(data)}")
            return
        player_id = struct.unpack_from("<B", data, protocol.HEADER_SIZE)[0]
        previous_player_id = state["player_id"]
        state["registered"] = True
        state["player_id"] = player_id
        state["match_ended"] = False
        state["force_server_pose_sync"] = True
        if previous_player_id != player_id:
            print(f"[ACK] player_id={player_id} role={_role_name(player_id)} header_seq={seq} ts={timestamp}")
        return

    if pkt_type == protocol.PKT_MAP:
        width, height, tile_scale, tiles = protocol.unpack_map_packet(data)
        state["map_w"] = width
        state["map_h"] = height
        state["tile_scale"] = tile_scale
        state["tiles"] = tiles
        state["bits"] = []
        state["bits_mask"] = 0
        state["game_mode"] = protocol.GAME_MODE_CHASE
        state["match_ended"] = False
        state["force_server_pose_sync"] = True
        _write_map_to_bram(bram, width, height, tiles)
        return

    if pkt_type == protocol.PKT_BITS_INIT:
        raw_bits = protocol.unpack_bits_init_packet(data)
        if raw_bits:
            max_id = max(bit_id for bit_id, _, _ in raw_bits)
            bits = [None] * (max_id + 1)
            for bit_id, bit_x, bit_y in raw_bits:
                bits[bit_id] = (bit_x, bit_y)
            state["bits"] = bits
        else:
            state["bits"] = []
        print(f"[BITS_INIT] count={len(raw_bits)}")
        return

    if pkt_type == protocol.PKT_NODE_MODE:
        next_mode = _mode_name_from_packet(protocol.unpack_node_mode_packet(data))
        previous_mode = state.get("mode", "manual")
        state["mode"] = next_mode
        if previous_mode != next_mode:
            print(f"[CTRL] mode {previous_mode} -> {next_mode}")
        return

    if pkt_type == protocol.PKT_GAME_STATE:
        _, rx_seq, rx_ts, game_mode, players, bits_mask = protocol.unpack_server_packet(data)
        state["game_mode"] = game_mode
        state["bits_mask"] = bits_mask
        state["players"] = players
        _update_local_pose_from_server(state, players)

        now = time.monotonic()
        if now - state["last_state_log_at"] >= STATE_LOG_PERIOD_S:
            print(
                f"[STATE] tick={rx_seq} ts={rx_ts} mode={game_mode} players={len(players)} "
                f"self_id={state['player_id']} pose=({state['x']:.2f}, {state['y']:.2f}, {state['angle']:.2f})"
            )
            state["last_state_log_at"] = now
        return

    print(f"[RX] {_describe_packet(data)}")


# Drain all queued UDP packets for this tick without blocking the main loop.
def _drain_packets(sock: socket.socket, state: dict, bram) -> None:
    while True:
        try:
            data, addr = sock.recvfrom(SOCKET_RECV_SIZE)
        except BlockingIOError:
            return
        except OSError as exc:
            print(f"[SOCK] recv error: {exc}")
            return

        try:
            _handle_packet(data, state, bram)
        except Exception as exc:
            print(f"[RX_ERR] {exc} from {addr} {_describe_packet(data)}")


# Run the standalone PYNQ board loop in either manual or auto mode.
def main():
    global USERNAME
    parser = argparse.ArgumentParser(description="Standalone PYNQ board client")
    parser.add_argument("--server", default=SERVER_IP)
    parser.add_argument("--port", type=int, default=SERVER_PORT)
    parser.add_argument("--overlay", default=OVERLAY_PATH)
    parser.add_argument("--username", default=os.environ.get("PYNQ_USERNAME", USERNAME))
    parser.add_argument("--mode", choices=["manual", "auto"],
                        default=os.environ.get("PYNQ_MODE", MODE))
    parser.add_argument("--move-speed", type=float,
                        default=float(os.environ.get("PYNQ_MOVE_SPEED", MOVE_SPEED)))
    parser.add_argument("--turn-step", type=int,
                        default=int(os.environ.get("PYNQ_TURN_STEP", TURN_STEP)))
    parser.add_argument("--auto-runner-speed", type=float,
                        default=float(os.environ.get("PYNQ_AUTO_RUNNER_SPEED", AUTO_RUNNER_SPEED)))
    parser.add_argument("--auto-tagger-speed", type=float,
                        default=float(os.environ.get("PYNQ_AUTO_TAGGER_SPEED", AUTO_TAGGER_SPEED)))
    parser.add_argument("--auto-fallback-speed", type=float,
                        default=float(os.environ.get("PYNQ_AUTO_FALLBACK_SPEED", AUTO_FALLBACK_SPEED)))
    args = parser.parse_args()

    print(f"[NET] EC2 target {args.server}:{args.port}")
    print(
        f"[CFG] username={args.username or '<none>'} role={PREFERRED_ROLE} "
        f"tick_rate={TICK_RATE} overlay={args.overlay} mode={args.mode}"
    )
    print(
        f"[CFG] move_speed={args.move_speed:.3f} turn_step={args.turn_step} "
        f"auto_runner_speed={args.auto_runner_speed:.3f} "
        f"auto_tagger_speed={args.auto_tagger_speed:.3f} "
        f"auto_fallback_speed={args.auto_fallback_speed:.3f}"
    )

    USERNAME = args.username

    overlay, bram, buttons = _load_overlay(args.overlay)
    _ = overlay

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    server_address = (args.server, args.port)

    state = {
        "mode": _normalise_mode_name(args.mode),
        "registered": False,
        "player_id": None,
        "seq": 0,
        "tick": 0,
        "x": INITIAL_X,
        "y": INITIAL_Y,
        "angle_raw": INITIAL_ANGLE_RAW & HW_ANGLE_MASK,
        "angle": (INITIAL_ANGLE_RAW & HW_ANGLE_MASK) * (2.0 * math.pi / HW_ANGLE_STEPS),
        "input_flags": 0,
        "match_ended": False,
        "last_rx_at": None,
        "last_register_tx_at": 0.0,
        "last_state_log_at": 0.0,
        "map_w": 32,
        "map_h": 32,
        "tile_scale": 8,
        "tiles": bytearray(),
        "game_mode": protocol.GAME_MODE_CHASE,
        "bits_mask": 0xFFFF,
        "bits": [],
        "players": [],
        "move_speed": float(args.move_speed),
        "turn_step": int(args.turn_step),
        "auto_runner_speed": float(args.auto_runner_speed),
        "auto_tagger_speed": float(args.auto_tagger_speed),
        "auto_fallback_speed": float(args.auto_fallback_speed),
        "auto_tagger_shoot_range": AUTO_TAGGER_SHOOT_RANGE,
        "auto_tagger_shoot_arc": AUTO_TAGGER_SHOOT_ARC,
        "auto_tagger_shoot_period_ticks": AUTO_TAGGER_SHOOT_PERIOD_TICKS,
        "server_pose_snap_distance": SERVER_POSE_SNAP_DISTANCE,
        "server_pose_snap_angle": SERVER_POSE_SNAP_ANGLE,
        "force_server_pose_sync": True,
    }

    print("[NET] starting lobby/register loop")

    try:
        while True:
            tick_start = time.monotonic()

            _drain_packets(sock, state, bram)

            now = time.monotonic()
            if state["registered"] and state["last_rx_at"] is not None:
                if now - state["last_rx_at"] > SERVER_SILENCE_S:
                    print(
                        f"[NET] server silent for {SERVER_SILENCE_S:.1f}s; "
                        "dropping back to registration"
                    )
                    state["registered"] = False
                    state["player_id"] = None
                    state["match_ended"] = False

            if state["mode"] == "auto":
                _apply_auto_input(state)
            else:
                _apply_manual_input(state, buttons)
            _write_pose_to_bram(bram, state)

            if not state["registered"]:
                if now - state["last_register_tx_at"] >= REGISTER_RETRY_S:
                    _send_register(sock, server_address, state)
            else:
                _send_state(sock, server_address, state)
                state["tick"] += 1

            elapsed = time.monotonic() - tick_start
            sleep_time = TICK_INTERVAL - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        print("\n[NET] stopped")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
