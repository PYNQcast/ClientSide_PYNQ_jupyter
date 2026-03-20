#!/usr/bin/env python3

import argparse
import errno
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
#   scp jupyter_side/test_package_v3.py pynq_full/interfacing/protocol.py \
#       xilinx@<PYNQ_IP>:/home/xilinx/jupyter_notebooks/
#
# Run on board:
#   cd /home/xilinx/jupyter_notebooks
#   python3 test_package_v3.py --mode auto


# --- Network configuration ---
SERVER_IP = "3.9.71.204"
SERVER_PORT = 9000
USERNAME = ""
MODE = "manual"
PREFERRED_ROLE = protocol.ROLE_ANY
BASE_TICK_RATE = 20
TICK_RATE = 50
TICK_INTERVAL = 1.0 / TICK_RATE
STATE_SEND_RATE = 20
STATE_SEND_INTERVAL = 1.0 / STATE_SEND_RATE
REGISTER_RETRY_S = 2.0
SERVER_SILENCE_S = 5.0
STATE_LOG_PERIOD_S = 1.0
SOCKET_RECV_SIZE = 4096
SOCKET_SEND_BUFFER = 262144


# --- Hardware configuration ---
OVERLAY_PATH = "/home/xilinx/jupyter_notebooks/Final_project_test/design_1_wrapper.bit"
CLOCK_MHZ = 50.0
HW_MAP_ROWS = 32
HW_MAP_COLS = 32
HW_PLAYER_POS_OFFSET = 32 * 4
HW_PLAYER_ANGLE_OFFSET = 33 * 4
# Sprite-capable hardware still exposes one BRAM window, so remote entities and
# bit markers are staged into the unused words immediately after the local pose.
HW_REMOTE_ENTITY_COUNT_OFFSET = 34 * 4
HW_REMOTE_ENTITY_BASE_OFFSET = 35 * 4
HW_REMOTE_ENTITY_STRIDE_WORDS = 2
HW_MAX_REMOTE_ENTITIES = 4
HW_BITS_COUNT_OFFSET = (35 + (HW_REMOTE_ENTITY_STRIDE_WORDS * HW_MAX_REMOTE_ENTITIES)) * 4
HW_BITS_MASK_OFFSET = HW_BITS_COUNT_OFFSET + 4
HW_BITS_BASE_OFFSET = HW_BITS_MASK_OFFSET + 4
HW_MAX_BITS = 16
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
SERVER_POSE_SNAP_DISTANCE = 8.0
SERVER_POSE_SNAP_ANGLE = math.pi
SERVER_POSE_HARD_SNAP_DISTANCE = 24.0
SERVER_POSE_HARD_SNAP_ANGLE = math.pi
SERVER_POSE_RECONCILE_BLEND = 0.25
SERVER_POSE_RECONCILE_DEADBAND = 0.05
SERVER_POSE_RECONCILE_ANGLE_DEADBAND = math.radians(2.0)
MANUAL_CORRECTION_GRACE_S = 0.25
MAP_SYNC_INPUT_GRACE_S = 0.35
BUTTON_TURN_RIGHT_MASK = 1 << 0
BUTTON_BACKWARD_MASK = 1 << 1
BUTTON_FORWARD_MASK = 1 << 2
BUTTON_TURN_LEFT_MASK = 1 << 3


# Scale a per-tick linear movement value from the 20 Hz tuning baseline to a target tick rate.
def _scaled_linear_for_tick_rate(value: float, target_tick_rate: int, baseline_tick_rate: int = BASE_TICK_RATE) -> float:
    target = max(1, int(target_tick_rate))
    baseline = max(1, int(baseline_tick_rate))
    return float(value) * (baseline / target)


# Scale a per-tick angle step from the 20 Hz tuning baseline to a target tick rate.
def _scaled_turn_step_for_tick_rate(value: int, target_tick_rate: int, baseline_tick_rate: int = BASE_TICK_RATE) -> int:
    scaled = _scaled_linear_for_tick_rate(float(value), target_tick_rate, baseline_tick_rate)
    return max(1, int(round(scaled)))


# Scale a tick-count cadence from the 20 Hz baseline so time-based auto actions stay similar.
def _scaled_period_ticks_for_tick_rate(value: int, target_tick_rate: int, baseline_tick_rate: int = BASE_TICK_RATE) -> int:
    target = max(1, int(target_tick_rate))
    baseline = max(1, int(baseline_tick_rate))
    return max(1, int(round(int(value) * (target / baseline))))


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


# Reject an older 16-bit sequence number so stale server state cannot rewind the board.
def _is_newer_seq(prev_seq: int | None, seq: int) -> bool:
    if prev_seq is None:
        return True
    delta = (int(seq) - int(prev_seq)) & 0xFFFF
    return delta != 0 and delta <= 0x7FFF


# Reject an older timestamp for packets that do not carry a meaningful rolling seq.
def _is_newer_timestamp(prev_ts: int | None, timestamp: int) -> bool:
    if prev_ts is None:
        return True
    return int(timestamp) >= int(prev_ts)


# Build a local 32x32 border-wall lobby map so HDMI and local collision start in a known-safe state.
def _build_fallback_lobby_map():
    tiles = bytearray(HW_MAP_ROWS * HW_MAP_COLS)
    for col in range(HW_MAP_COLS):
        tiles[col] = 1
        tiles[(HW_MAP_ROWS - 1) * HW_MAP_COLS + col] = 1
    for row in range(HW_MAP_ROWS):
        tiles[row * HW_MAP_COLS] = 1
        tiles[row * HW_MAP_COLS + (HW_MAP_COLS - 1)] = 1
    return {
        "map_w": HW_MAP_COLS,
        "map_h": HW_MAP_ROWS,
        "tile_scale": 8,
        "tiles": tiles,
        "bits": [],
        "bits_mask": 0,
        "game_mode": protocol.GAME_MODE_CHASE,
    }


# Load the local fallback lobby map until the server sends the authoritative PKT_MAP.
def _apply_fallback_lobby_map(state: dict, bram) -> None:
    fallback = _build_fallback_lobby_map()
    state["map_w"] = fallback["map_w"]
    state["map_h"] = fallback["map_h"]
    state["tile_scale"] = fallback["tile_scale"]
    state["tiles"] = fallback["tiles"]
    state["bits"] = fallback["bits"]
    state["bits_mask"] = fallback["bits_mask"]
    state["game_mode"] = fallback["game_mode"]
    state["players"] = []
    state["remote_entities"] = []
    state["input_suspended_until"] = time.monotonic() + MAP_SYNC_INPUT_GRACE_S
    state["force_server_pose_sync"] = True
    _write_map_to_bram(bram, fallback["map_w"], fallback["map_h"], fallback["tiles"])
    _write_sprite_state_to_bram(bram, state)
    state["sprite_bram_dirty"] = False
    if state["tiles"] and not _is_walkable(state, state["x"], state["y"]):
        state["x"] = 0.0
        state["y"] = 0.0
        state["angle"] = 0.0
        state["angle_raw"] = 0
    print("[HW] loaded fallback lobby map until PKT_MAP arrives")


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


# Stub hardware objects used when --no-hw is passed (PC testing without a board).
class _NullBram:
    def write(self, offset, value):
        pass

class _NullButtons:
    def read(self):
        return 0


# Load the bitstream and expose BRAM and button GPIO handles.
def _load_overlay(overlay_path: str):
    if Overlay is None or Clocks is None:
        raise SystemExit(
            "test_package_v3.py must run on the PYNQ board with the pynq package installed"
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


# Pack world-space x/y into the same 16-bit-per-axis BRAM word used for the local player.
def _pack_hw_xy_word(x: float, y: float, tile_scale: int, map_w: int, map_h: int) -> int:
    x_q = _world_to_hw_q6_10(x, tile_scale, map_w)
    y_q = _world_to_hw_q6_10(y, tile_scale, map_h)
    return ((x_q & 0xFFFF) << 16) | (y_q & 0xFFFF)


# Keep only non-local, sprite-worthy entities in a small stable list for hardware rendering.
def build_remote_entities(local_player_id: int | None, players):
    entities = []
    for player in players or []:
        player_id = int(player.get("player_id", 0) or 0)
        if player_id == 0 or player_id == local_player_id:
            continue
        entities.append({
            "entity_id": player_id & 0xFF,
            "x": float(player.get("x", 0.0) or 0.0),
            "y": float(player.get("y", 0.0) or 0.0),
            "angle": float(player.get("angle", 0.0) or 0.0),
            "flags": int(player.get("flags", 0) or 0) & 0xFF,
        })
    entities.sort(key=lambda entity: entity["entity_id"])
    return entities[:HW_MAX_REMOTE_ENTITIES]


# Pack one remote entity's metadata into a compact control word for the sprite pass.
def _pack_remote_entity_meta(entity: dict) -> int:
    angle_raw = _radians_to_hw_angle(float(entity.get("angle", 0.0) or 0.0))
    entity_id = int(entity.get("entity_id", 0) or 0) & 0x7F
    flags = int(entity.get("flags", 0) or 0) & 0xFF
    return (1 << 31) | (entity_id << 24) | (flags << 16) | (angle_raw & 0x0FFF)


# Mirror remote entities into BRAM so the FPGA sprite pass can render other humans and ghosts.
def _write_remote_entities_to_bram(bram, state: dict) -> None:
    if bram is None:
        return

    entities = build_remote_entities(state.get("player_id"), state.get("players", []))
    state["remote_entities"] = entities
    bram.write(HW_REMOTE_ENTITY_COUNT_OFFSET, len(entities) & 0xFFFFFFFF)

    for slot in range(HW_MAX_REMOTE_ENTITIES):
        base = HW_REMOTE_ENTITY_BASE_OFFSET + (slot * HW_REMOTE_ENTITY_STRIDE_WORDS * 4)
        if slot < len(entities):
            entity = entities[slot]
            bram.write(
                base,
                _pack_hw_xy_word(
                    entity["x"],
                    entity["y"],
                    state["tile_scale"],
                    state["map_w"],
                    state["map_h"],
                ),
            )
            bram.write(base + 4, _pack_remote_entity_meta(entity))
        else:
            bram.write(base, 0)
            bram.write(base + 4, 0)


# Mirror collectible bit positions plus the live bits_mask into BRAM for sprite rendering.
def _write_bits_to_bram(bram, state: dict) -> None:
    if bram is None:
        return

    bits = list(state.get("bits") or [])
    count = min(len(bits), HW_MAX_BITS)
    bram.write(HW_BITS_COUNT_OFFSET, count & 0xFFFFFFFF)
    bram.write(HW_BITS_MASK_OFFSET, int(state.get("bits_mask", 0) or 0) & 0xFFFF)

    for slot in range(HW_MAX_BITS):
        offset = HW_BITS_BASE_OFFSET + (slot * 4)
        if slot < count and bits[slot] is not None:
            bit_x, bit_y = bits[slot]
            bram.write(
                offset,
                _pack_hw_xy_word(
                    float(bit_x),
                    float(bit_y),
                    state["tile_scale"],
                    state["map_w"],
                    state["map_h"],
                ),
            )
        else:
            bram.write(offset, 0)


# Keep the whole sprite side of BRAM coherent whenever remote state changes.
def _write_sprite_state_to_bram(bram, state: dict) -> None:
    _write_remote_entities_to_bram(bram, state)
    _write_bits_to_bram(bram, state)


# Hold local movement briefly while registration or map sync is settling.
def _input_is_temporarily_suspended(state: dict) -> bool:
    return time.monotonic() < float(state.get("input_suspended_until", 0.0) or 0.0)


# Send one UDP packet without letting temporary network backpressure kill the board loop.
def _try_send_packet(sock: socket.socket, packet: bytes, server_address, state: dict, label: str) -> bool:
    try:
        sock.sendto(packet, server_address)
        return True
    except BlockingIOError:
        err_no = errno.EAGAIN
        err_text = "send buffer busy"
    except OSError as exc:
        if exc.errno not in {errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOBUFS}:
            raise
        err_no = int(exc.errno or 0)
        err_text = str(exc)

    now = time.monotonic()
    state["tx_drop_count"] = int(state.get("tx_drop_count", 0) or 0) + 1
    last_log_at = float(state.get("last_tx_error_log_at", 0.0) or 0.0)
    if now - last_log_at >= 1.0:
        print(
            f"[TX_DROP] {label} err={err_no} {err_text} "
            f"drops={state['tx_drop_count']}"
        )
        state["last_tx_error_log_at"] = now
    return False


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
    if _input_is_temporarily_suspended(state):
        return
    if raw:
        state["last_manual_input_at"] = time.monotonic()
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
    if not _try_send_packet(sock, pkt, server_address, state, "register"):
        return False
    print(
        f"[TX] REGISTER seq={state['seq']} pose=({state['x']:.2f}, {state['y']:.2f}, {state['angle']:.2f}) "
        f"username={USERNAME or '<none>'}"
    )
    state["seq"] = (state["seq"] + 1) & 0xFFFF
    state["last_register_tx_at"] = time.monotonic()
    return True


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
    if not _try_send_packet(sock, pkt, server_address, state, "state"):
        return False
    state["seq"] = (state["seq"] + 1) & 0xFFFF
    state["last_state_tx_at"] = time.monotonic()
    return True


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
        now = time.monotonic()
        active_manual_control = (
            state.get("mode", "manual") == "manual"
            and (now - float(state.get("last_manual_input_at", 0.0) or 0.0)) <= MANUAL_CORRECTION_GRACE_S
        )
        soft_snap_distance = float(state.get("server_pose_snap_distance", SERVER_POSE_SNAP_DISTANCE))
        soft_snap_angle = float(state.get("server_pose_snap_angle", SERVER_POSE_SNAP_ANGLE))
        hard_snap_distance = float(state.get("server_pose_hard_snap_distance", SERVER_POSE_HARD_SNAP_DISTANCE))
        hard_snap_angle = float(state.get("server_pose_hard_snap_angle", SERVER_POSE_HARD_SNAP_ANGLE))
        should_snap_position = (
            state.get("force_server_pose_sync", False)
            or player_id == 0
            or server_match_end
            or distance_error >= hard_snap_distance
            or (
                not active_manual_control
                and distance_error >= soft_snap_distance
            )
        )
        should_snap_angle = (
            state.get("force_server_pose_sync", False)
            or player_id == 0
            or server_match_end
            or (
                state.get("mode", "manual") == "auto"
                and (
                    angle_error >= hard_snap_angle
                    or (
                        not active_manual_control
                        and angle_error >= soft_snap_angle
                    )
                )
            )
        )
        reconcile_blend = min(
            1.0,
            max(0.0, float(state.get("server_pose_reconcile_blend", SERVER_POSE_RECONCILE_BLEND))),
        )
        if active_manual_control:
            reconcile_blend *= 0.5
        reconcile_deadband = max(
            0.0,
            float(state.get("server_pose_reconcile_deadband", SERVER_POSE_RECONCILE_DEADBAND)),
        )
        reconcile_angle_deadband = float(
            state.get("server_pose_reconcile_angle_deadband", SERVER_POSE_RECONCILE_ANGLE_DEADBAND)
        )
        if should_snap_position:
            state["x"] = server_x
            state["y"] = server_y
        elif state.get("mode", "manual") != "manual":
            # auto mode only: blend toward server position
            if distance_error > reconcile_deadband:
                state["x"] += (server_x - state["x"]) * reconcile_blend
                state["y"] += (server_y - state["y"]) * reconcile_blend
            else:
                state["x"] = server_x
                state["y"] = server_y
        # manual mode: keep local position — server pose is authoritative only on snaps above

        if should_snap_angle:
            state["angle"] = server_angle
            state["angle_raw"] = _radians_to_hw_angle(server_angle)
        elif state.get("mode", "manual") == "auto":
            if angle_error > reconcile_angle_deadband:
                state["angle"] = _wrap_angle(state["angle"] + _wrap_angle(server_angle - state["angle"]) * reconcile_blend)
            else:
                state["angle"] = server_angle
            state["angle_raw"] = _radians_to_hw_angle(state["angle"])
        # manual mode: keep local angle entirely
        if should_snap_position or should_snap_angle:
            state["force_server_pose_sync"] = False
        state["match_ended"] = server_match_end
        return


# Decode one server packet and update local board/runtime state from it.
def _handle_packet(data: bytes, state: dict, bram) -> None:
    pkt_type, seq, timestamp = protocol.unpack_header(data)
    state["last_rx_at"] = time.monotonic()

    if pkt_type == protocol.PKT_ACK:
        last_ack_ts = state.get("last_ack_timestamp")
        if not _is_newer_timestamp(last_ack_ts, timestamp):
            return
        state["last_ack_timestamp"] = timestamp
        if len(data) < protocol.HEADER_SIZE + 1:
            print(f"[ACK] malformed: {_describe_packet(data)}")
            return
        player_id = struct.unpack_from("<B", data, protocol.HEADER_SIZE)[0]
        previous_player_id = state["player_id"]
        state["registered"] = True
        state["player_id"] = player_id
        state["match_ended"] = False
        state["players"] = []
        state["remote_entities"] = []
        state["bits_mask"] = 0
        state["input_suspended_until"] = time.monotonic() + MAP_SYNC_INPUT_GRACE_S
        state["force_server_pose_sync"] = True
        state["sprite_bram_dirty"] = True
        if previous_player_id != player_id:
            print(f"[ACK] player_id={player_id} role={_role_name(player_id)} header_seq={seq} ts={timestamp}")
        return

    if pkt_type == protocol.PKT_MAP:
        last_map_ts = state.get("last_map_timestamp")
        if not _is_newer_timestamp(last_map_ts, timestamp):
            return
        state["last_map_timestamp"] = timestamp
        width, height, tile_scale, tiles = protocol.unpack_map_packet(data)
        state["map_w"] = width
        state["map_h"] = height
        state["tile_scale"] = tile_scale
        state["tiles"] = tiles
        state["bits"] = []
        state["bits_mask"] = 0
        state["players"] = []
        state["remote_entities"] = []
        state["game_mode"] = protocol.GAME_MODE_CHASE
        state["match_ended"] = False
        state["input_suspended_until"] = time.monotonic() + MAP_SYNC_INPUT_GRACE_S
        state["force_server_pose_sync"] = True
        _write_map_to_bram(bram, width, height, tiles)
        state["sprite_bram_dirty"] = True
        if not _is_walkable(state, state["x"], state["y"]):
            state["x"] = 0.0
            state["y"] = 0.0
            state["angle"] = 0.0
            state["angle_raw"] = 0
            print("[HW] player inside wall after map change — snapped to origin")
        return

    if pkt_type == protocol.PKT_BITS_INIT:
        last_bits_ts = state.get("last_bits_timestamp")
        if not _is_newer_timestamp(last_bits_ts, timestamp):
            return
        state["last_bits_timestamp"] = timestamp
        raw_bits = protocol.unpack_bits_init_packet(data)
        if raw_bits:
            max_id = max(bit_id for bit_id, _, _ in raw_bits)
            bits = [None] * (max_id + 1)
            for bit_id, bit_x, bit_y in raw_bits:
                bits[bit_id] = (bit_x, bit_y)
            state["bits"] = bits
        else:
            state["bits"] = []
        state["sprite_bram_dirty"] = True
        print(f"[BITS_INIT] count={len(raw_bits)}")
        return

    if pkt_type == protocol.PKT_NODE_MODE:
        last_mode_ts = state.get("last_node_mode_timestamp")
        if not _is_newer_timestamp(last_mode_ts, timestamp):
            return
        state["last_node_mode_timestamp"] = timestamp
        next_mode = _mode_name_from_packet(protocol.unpack_node_mode_packet(data))
        previous_mode = state.get("mode", "manual")
        state["mode"] = next_mode
        if previous_mode != next_mode:
            print(f"[CTRL] mode {previous_mode} -> {next_mode}")
        return

    if pkt_type == protocol.PKT_GAME_STATE:
        last_seq = state.get("last_game_state_seq")
        if not _is_newer_seq(last_seq, seq):
            return
        state["last_game_state_seq"] = seq
        _, rx_seq, rx_ts, game_mode, players, bits_mask = protocol.unpack_server_packet(data)
        state["game_mode"] = game_mode
        state["bits_mask"] = bits_mask
        state["players"] = players
        _update_local_pose_from_server(state, players)
        state["sprite_bram_dirty"] = True

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
    parser.add_argument("--tick-rate", type=int,
                        default=int(os.environ.get("PYNQ_TICK_RATE", TICK_RATE)))
    parser.add_argument("--send-rate", type=int,
                        default=int(os.environ.get("PYNQ_SEND_RATE", STATE_SEND_RATE)))
    parser.add_argument("--move-speed", type=float,
                        default=(float(os.environ["PYNQ_MOVE_SPEED"]) if "PYNQ_MOVE_SPEED" in os.environ else None))
    parser.add_argument("--turn-step", type=int,
                        default=(int(os.environ["PYNQ_TURN_STEP"]) if "PYNQ_TURN_STEP" in os.environ else None))
    parser.add_argument("--auto-runner-speed", type=float,
                        default=(float(os.environ["PYNQ_AUTO_RUNNER_SPEED"]) if "PYNQ_AUTO_RUNNER_SPEED" in os.environ else None))
    parser.add_argument("--auto-tagger-speed", type=float,
                        default=(float(os.environ["PYNQ_AUTO_TAGGER_SPEED"]) if "PYNQ_AUTO_TAGGER_SPEED" in os.environ else None))
    parser.add_argument("--auto-fallback-speed", type=float,
                        default=(float(os.environ["PYNQ_AUTO_FALLBACK_SPEED"]) if "PYNQ_AUTO_FALLBACK_SPEED" in os.environ else None))
    parser.add_argument("--auto-shoot-period", type=int,
                        default=(int(os.environ["PYNQ_AUTO_SHOOT_PERIOD"]) if "PYNQ_AUTO_SHOOT_PERIOD" in os.environ else None))
    parser.add_argument("--no-hw", action="store_true",
                        help="Skip overlay load; use null BRAM/buttons stubs (PC testing)")
    args = parser.parse_args()
    tick_rate = max(1, int(args.tick_rate))
    tick_interval = 1.0 / tick_rate
    state_send_rate = max(1, int(args.send_rate))
    state_send_interval = 1.0 / state_send_rate
    effective_move_speed = (
        float(args.move_speed)
        if args.move_speed is not None
        else _scaled_linear_for_tick_rate(MOVE_SPEED, tick_rate)
    )
    effective_turn_step = (
        int(args.turn_step)
        if args.turn_step is not None
        else _scaled_turn_step_for_tick_rate(TURN_STEP, tick_rate)
    )
    effective_auto_runner_speed = (
        float(args.auto_runner_speed)
        if args.auto_runner_speed is not None
        else _scaled_linear_for_tick_rate(AUTO_RUNNER_SPEED, tick_rate)
    )
    effective_auto_tagger_speed = (
        float(args.auto_tagger_speed)
        if args.auto_tagger_speed is not None
        else _scaled_linear_for_tick_rate(AUTO_TAGGER_SPEED, tick_rate)
    )
    effective_auto_fallback_speed = (
        float(args.auto_fallback_speed)
        if args.auto_fallback_speed is not None
        else _scaled_linear_for_tick_rate(AUTO_FALLBACK_SPEED, tick_rate)
    )
    effective_auto_shoot_period = (
        int(args.auto_shoot_period)
        if args.auto_shoot_period is not None
        else _scaled_period_ticks_for_tick_rate(AUTO_TAGGER_SHOOT_PERIOD_TICKS, tick_rate)
    )

    print(f"[NET] EC2 target {args.server}:{args.port}")
    print(
        f"[CFG] username={args.username or '<none>'} role={PREFERRED_ROLE} "
        f"tick_rate={tick_rate} send_rate={state_send_rate} overlay={args.overlay} mode={args.mode}"
    )
    print(
        f"[CFG] move_speed={effective_move_speed:.3f} turn_step={effective_turn_step} "
        f"auto_runner_speed={effective_auto_runner_speed:.3f} "
        f"auto_tagger_speed={effective_auto_tagger_speed:.3f} "
        f"auto_fallback_speed={effective_auto_fallback_speed:.3f} "
        f"auto_shoot_period={effective_auto_shoot_period}"
    )

    USERNAME = args.username

    if args.no_hw:
        print("[HW] --no-hw: using null BRAM/button stubs (PC test mode)")
        bram = _NullBram()
        buttons = _NullButtons()
    else:
        overlay, bram, buttons = _load_overlay(args.overlay)
        _ = overlay

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_SEND_BUFFER)
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
        "last_state_tx_at": 0.0,
        "last_state_log_at": 0.0,
        "map_w": 32,
        "map_h": 32,
        "tile_scale": 8,
        "tiles": bytearray(),
        "game_mode": protocol.GAME_MODE_CHASE,
        "bits_mask": 0xFFFF,
        "bits": [],
        "players": [],
        "remote_entities": [],
        "move_speed": effective_move_speed,
        "turn_step": effective_turn_step,
        "auto_runner_speed": effective_auto_runner_speed,
        "auto_tagger_speed": effective_auto_tagger_speed,
        "auto_fallback_speed": effective_auto_fallback_speed,
        "auto_tagger_shoot_range": AUTO_TAGGER_SHOOT_RANGE,
        "auto_tagger_shoot_arc": AUTO_TAGGER_SHOOT_ARC,
        "auto_tagger_shoot_period_ticks": effective_auto_shoot_period,
        "server_pose_snap_distance": SERVER_POSE_SNAP_DISTANCE,
        "server_pose_snap_angle": SERVER_POSE_SNAP_ANGLE,
        "server_pose_hard_snap_distance": SERVER_POSE_HARD_SNAP_DISTANCE,
        "server_pose_hard_snap_angle": SERVER_POSE_HARD_SNAP_ANGLE,
        "force_server_pose_sync": True,
        "last_manual_input_at": 0.0,
        "last_ack_timestamp": None,
        "last_map_timestamp": None,
        "last_bits_timestamp": None,
        "last_node_mode_timestamp": None,
        "last_game_state_seq": None,
        "tx_drop_count": 0,
        "last_tx_error_log_at": 0.0,
        "sprite_bram_dirty": False,
    }
    _apply_fallback_lobby_map(state, bram)

    print("[NET] starting lobby/register loop")

    next_tick = time.monotonic()
    try:
        while True:
            next_tick += tick_interval

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
                    state["last_game_state_seq"] = None
                    _apply_fallback_lobby_map(state, bram)

            if state["mode"] == "auto":
                _apply_auto_input(state)
            else:
                _apply_manual_input(state, buttons)
            _write_pose_to_bram(bram, state)

            if state.get("sprite_bram_dirty"):
                _write_sprite_state_to_bram(bram, state)
                state["sprite_bram_dirty"] = False

            if not state["registered"]:
                if now - state["last_register_tx_at"] >= REGISTER_RETRY_S:
                    _send_register(sock, server_address, state)
            else:
                state["tick"] += 1
                if now - float(state.get("last_state_tx_at", 0.0) or 0.0) >= state_send_interval:
                    _send_state(sock, server_address, state)

            now = time.monotonic()
            sleep_time = next_tick - now
            if sleep_time > 0:
                time.sleep(sleep_time)
            elif sleep_time < -tick_interval:
                # fell behind by more than a full tick — reset to avoid burst catch-up
                next_tick = now

    except KeyboardInterrupt:
        print("\n[NET] stopped")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
