#!/usr/bin/env python3
# test_package_v4.py — PYNQ board client, derived from test_package_basic.py
#
# Adds over basic:
#   - Sprite BRAM: remote entities (other players/ghosts) + collectible bits
#   - Auto mode: local board steering from authoritative server world snapshots
#   - Anticheat-aware: FLAG_TAGGED triggers a spawn snap; FLAG_MATCH_END halts
#     movement and sends heartbeats until next match.
#   - PKT_BITS_INIT handled: bit positions cached, bitmask updated each tick.
#   - PKT_NODE_MODE honoured: server can switch manual ↔ auto ↔ replay at runtime.
#
# Design principle: manual mode = 100% local authority.
#                   auto mode   = local board steering with server resync on
#                                 tagged/map-reset/large divergence.
#                   replay      = authoritative server-driven playback; buttons
#                                 are ignored and pose is streamed from UDP.
#                   Both modes  = local collision + BRAM writes every tick.
#
# Hardware note (design_1_wrapper.bit / design_1.hwh):
#   ray_caster_0 uses COORD_FRAC_BITS=16 (Q6.16 fixed-point) and has both
#   v_sprite_* and v_r_sprite_* port groups, confirming at least two sprite
#   slots are active in the raycaster pipeline. All MAX_ENTITIES slots (up to 4)
#   are written to BRAM; the hardware renders as many as its pipeline supports.
#   axi_gpio_0 is 4-bit wide (C_GPIO_WIDTH=4), matching BTN_LEFT/RIGHT/FWD/BACK.
#
# Copy to board:
#   scp jupyter_side/test_package_v4.py pynq_full/interfacing/protocol.py \
#       xilinx@<PYNQ_IP>:/home/xilinx/jupyter_notebooks/Final_project_test/
#
# Run:
#   python3 test_package_v4.py [--mode auto] [--username NAME] [--no-hw]

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

# ── config ────────────────────────────────────────────────────────────────────
SERVER_IP    = "3.9.71.204"
SERVER_PORT  = 9000
OVERLAY_PATH = "/home/xilinx/jupyter_notebooks/Final_project_test/design_1_wrapper.bit"
CLOCK_MHZ    = 50.0
TICK_RATE    = 60        # Hz — main loop rate, aligned with server authoritative tick
SEND_RATE    = 60        # Hz — state update rate to server to reduce board-side jitter
REGISTER_RETRY_S = 2.0
SERVER_SILENCE_S = 5.0
LOG_PERIOD_S     = 1.0

# ── BRAM memory map ───────────────────────────────────────────────────────────
# Words 0–31   : map rows (one 32-bit word per row, bit N = tile N)
# Word  32     : player position  (x[31:16] | y[15:0])  Q6.10 fixed-point
# Word  33     : player angle     (12-bit raw angle)
# Word  34     : sprite 0 xy position   (x[31:16] | y[15:0])  — hardcoded by HDL
# Word  35     : sprite 0 metadata      valid[31] | id[30:24] | flags[23:16] | angle[11:0]
# Word  36     : sprite 1 xy position   (v_r_sprite_* pipeline, second slot)
# Word  37     : sprite 1 metadata
# ... (further slots may be read if HDL supports them)
MAP_ROWS = MAP_COLS = 32
PLAYER_POS_OFFSET     = 32 * 4
PLAYER_ANGLE_OFFSET   = 33 * 4
ENTITY_BASE_OFFSET    = 34 * 4   # HDL reads sprite xy directly from word 34 — no count word
ENTITY_STRIDE         = 2          # words per entity slot (xy + meta)
MAX_ENTITIES          = 4
BITS_COUNT_OFFSET     = (34 + ENTITY_STRIDE * MAX_ENTITIES) * 4
BITS_MASK_OFFSET      = BITS_COUNT_OFFSET + 4
BITS_BASE_OFFSET      = BITS_MASK_OFFSET  + 4
MAX_BITS              = 16
COORD_FRAC_BITS       = 10
ANGLE_STEPS           = 1 << 12
ANGLE_MASK            = ANGLE_STEPS - 1

# ── movement config ───────────────────────────────────────────────────────────
MOVE_SPEED   = 0.25    # world units per tick at 50 Hz baseline; auto-rescaled for runtime tick rate
TURN_STEP    = 26      # angle units per tick at 50 Hz baseline; auto-rescaled for runtime tick rate
COLLISION_R  = 2.0
AUTO_SPEED   = 0.10    # world units per tick for auto steering
AUTO_TURN    = 80      # angle units per tick for auto steering
AUTO_RUNNER_SPEED = AUTO_SPEED
AUTO_TAGGER_SPEED = 0.11
AUTO_FALLBACK_SPEED = 0.09
AUTO_RUNNER_EVADE_DISTANCE = 42.0
AUTO_TAGGER_SHOOT_RANGE = 26.0
AUTO_TAGGER_SHOOT_ARC = 0.4
AUTO_TAGGER_SHOOT_PERIOD_TICKS = 4
SERVER_POSE_SNAP_DISTANCE = 8.0
SERVER_POSE_SNAP_ANGLE = 0.75
MAP_SYNC_INPUT_GRACE_S = 0.35
BTN_LEFT     = 1 << 0  # axi_gpio_0 C_GPIO_WIDTH=4 → 4-bit button input
BTN_BACK     = 1 << 1
BTN_FWD      = 1 << 2
BTN_RIGHT    = 1 << 3

# ── sprite slot config ─────────────────────────────────────────────────────────
# design_1_wrapper.bit exposes v_sprite_* (slot 0) and v_r_sprite_* (slot 1)
# ports in ray_caster_0, confirming at least 2 sprite slots are rendered.
# All MAX_ENTITIES slots are staged in BRAM; hardware renders up to HW_SPRITE_SLOTS.
HW_SPRITE_SLOTS = 2   # confirmed active in current bitstream (v_sprite + v_r_sprite)

# ── hw stubs ──────────────────────────────────────────────────────────────────
class _NullBram:
    def write(self, offset, value): pass

class _NullButtons:
    def read(self): return 0

# ── hw helpers ────────────────────────────────────────────────────────────────
def _load_overlay(path):
    if Overlay is None:
        raise SystemExit("pynq package not found — use --no-hw for PC testing")
    overlay = Overlay(path)
    bram    = overlay.axi_bram_ctrl_0
    buttons = overlay.axi_gpio_0.channel1
    Clocks.fclk0_mhz = CLOCK_MHZ
    time.sleep(0.1)
    print(f"[HW] overlay ready, fclk0={CLOCK_MHZ:.0f}MHz")
    return overlay, bram, buttons

def _q6_10(v, tile_scale, dim):
    raw = int(round(((v / tile_scale) + dim / 2.0) * (1 << COORD_FRAC_BITS)))
    return max(0, min((dim << COORD_FRAC_BITS) - 1, raw))

def _hw_angle(a):
    return int(round((a % (2 * math.pi)) * ANGLE_STEPS / (2 * math.pi))) & ANGLE_MASK

def _xy_word(x, y, ts, w, h):
    return ((_q6_10(x, ts, w) & 0xFFFF) << 16) | (_q6_10(y, ts, h) & 0xFFFF)

# ── BRAM writes ───────────────────────────────────────────────────────────────
def _write_map(bram, tiles, w, h):
    if w <= 0 or h <= 0 or len(tiles) < (w * h):
        print(f"[HW] ignored malformed map write ({w}x{h}, tiles={len(tiles)})")
        return False

    for row in range(MAP_ROWS):
        bram.write(row * 4, 0)

    for row in range(min(h, MAP_ROWS)):
        word = 0
        base = row * w
        for col in range(min(w, MAP_COLS)):
            if tiles[base + col]:
                word |= 1 << col
        bram.write(row * 4, word & 0xFFFFFFFF)
    print(f"[HW] map written ({w}x{h})")
    return True

def _write_pose(bram, state):
    ts = state["tile_scale"]
    w, h = state["map_w"], state["map_h"]
    bram.write(PLAYER_POS_OFFSET,   _xy_word(state["x"], state["y"], ts, w, h))
    bram.write(PLAYER_ANGLE_OFFSET, state["angle_raw"] & ANGLE_MASK)

def _write_sprites(bram, state):
    """Write remote entities and bit markers to BRAM sprite region.

    Entity slot layout (2 words per slot, starting at ENTITY_BASE_OFFSET):
      Word+0: xy position  — x_q6_10[31:16] | y_q6_10[15:0]
      Word+1: metadata     — valid[31] | entity_id[30:24] | flags[23:16] | angle_raw[11:0]

    Ghosts (FLAG_GHOST in flags byte) are included as normal remote entities.
    Hardware renders up to HW_SPRITE_SLOTS (2 confirmed active in current bitstream);
    remaining slots are staged in BRAM for future hardware upgrades.
    """
    ts = state["tile_scale"]
    w, h = state["map_w"], state["map_h"]
    pid = state["player_id"]

    # Remote entities: all players except self and unregistered (id=0).
    # Ghosts (id >= 3, FLAG_GHOST set) are included — they appear as regular sprites.
    # Human opponent first (lowest id), then ghosts — slot 0 = most important target.
    humans  = sorted(
        [p for p in state["players"] if p["player_id"] not in (0, pid) and not (int(p["flags"]) & protocol.FLAG_GHOST)],
        key=lambda p: p["player_id"],
    )
    ghosts  = sorted(
        [p for p in state["players"] if p["player_id"] not in (0, pid) and (int(p["flags"]) & protocol.FLAG_GHOST)],
        key=lambda p: p["player_id"],
    )
    entities = (humans + ghosts)[:MAX_ENTITIES]

    # No count word — HDL reads sprite xy directly from ENTITY_BASE_OFFSET (word 34).
    for slot in range(MAX_ENTITIES):
        base = ENTITY_BASE_OFFSET + slot * ENTITY_STRIDE * 4
        if slot < len(entities):
            e = entities[slot]
            angle_raw = _hw_angle(float(e["angle"]))
            eid   = int(e["player_id"]) & 0x7F
            flags = int(e["flags"])     & 0xFF
            meta  = (1 << 31) | (eid << 24) | (flags << 16) | (angle_raw & 0x0FFF)
            bram.write(base,     _xy_word(float(e["x"]), float(e["y"]), ts, w, h))
            bram.write(base + 4, meta)
        else:
            bram.write(base,     0)
            bram.write(base + 4, 0)

    # collectible bits
    bits      = state["bits"]          # list of (x,y) or None, indexed by bit_id
    bits_mask = state["bits_mask"]
    count     = min(len(bits), MAX_BITS)
    bram.write(BITS_COUNT_OFFSET, count & 0xFFFFFFFF)
    bram.write(BITS_MASK_OFFSET,  bits_mask & 0xFFFF)
    for slot in range(MAX_BITS):
        offset = BITS_BASE_OFFSET + slot * 4
        if slot < count and bits[slot] is not None:
            bx, by = bits[slot]
            bram.write(offset, _xy_word(float(bx), float(by), ts, w, h))
        else:
            bram.write(offset, 0)

# ── collision ─────────────────────────────────────────────────────────────────
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

def _resolve_move(state, nx, ny):
    if _walkable(state, nx, ny):          return nx, ny
    if _walkable(state, nx, state["y"]):  return nx, state["y"]
    if _walkable(state, state["x"], ny):  return state["x"], ny
    return state["x"], state["y"]


def _cell_to_world(col, row, width, height, tile_scale):
    return (
        (col - width / 2.0 + 0.5) * tile_scale,
        (row - height / 2.0 + 0.5) * tile_scale,
    )


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


def _cell_is_open(state, col, row):
    map_w = state["map_w"]
    map_h = state["map_h"]
    tiles = state["tiles"]
    if col < 0 or row < 0 or col >= map_w or row >= map_h:
        return False
    if not tiles:
        return True
    return tiles[row * map_w + col] == 0


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


def _active_bit_positions(state):
    positions = []
    for index, bit in enumerate(state["bits"]):
        if bit is None:
            continue
        if (state["bits_mask"] & (1 << index)) == 0:
            continue
        positions.append(bit)
    return positions


def _compute_evade_target(x, y, threat_x, threat_y, distance):
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


def _choose_auto_objective(state):
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

# ── fallback map ──────────────────────────────────────────────────────────────
def _fallback_map():
    tiles = bytearray(MAP_ROWS * MAP_COLS)
    for c in range(MAP_COLS):
        tiles[c] = tiles[(MAP_ROWS-1)*MAP_COLS+c] = 1
    for r in range(MAP_ROWS):
        tiles[r*MAP_COLS] = tiles[r*MAP_COLS+MAP_COLS-1] = 1
    return tiles

# ── network helpers ───────────────────────────────────────────────────────────
def _send(sock, pkt, addr):
    try:
        sock.sendto(pkt, addr)
        return True
    except OSError as e:
        if e.errno not in {errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOBUFS}:
            raise
        return False

def _send_register(sock, addr, state):
    pkt = protocol.pack_register_packet(
        seq=state["seq"], x=state["x"], y=state["y"], angle=state["angle"],
        preferred_role=state["preferred_role"], username=state["username"],
        movement_mode=protocol.MOVEMENT_MODE_POSE,
    )
    if _send(sock, pkt, addr):
        print(f"[TX] REGISTER seq={state['seq']} username={state['username'] or '<none>'}")
        state["seq"]        = (state["seq"] + 1) & 0xFFFF
        state["last_reg_tx"] = time.monotonic()

def _read_cpu_temp():
    # Try standard Linux thermal zone first (works on many SBCs)
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            val = int(f.read().strip())
            return val // 1000 if val > 1000 else val  # millidegrees or degrees
    except Exception:
        pass
    # Zynq-7020 XADC IIO — millidegrees Celsius
    try:
        with open("/sys/bus/iio/devices/iio:device0/in_temp0_input") as f:
            return int(f.read().strip()) // 1000
    except Exception:
        pass
    # Zynq-7020 XADC IIO — raw ADC value, needs conversion
    try:
        with open("/sys/bus/iio/devices/iio:device0/in_temp0_raw") as f:
            raw = int(f.read().strip())
            return int(raw * 503.975 / 4096 - 273.15)
    except Exception:
        pass
    return 0


def _send_perf(sock, addr, state):
    now = time.monotonic()
    elapsed = now - state["last_perf_tx"]
    tick_hz = int(round(state["perf_tick_count"] / elapsed)) if elapsed > 0 else 0
    pkt = protocol.pack_perf_packet(
        seq=state["seq"],
        tick_rate_hz=tick_hz,
        cpu_temp_c=_read_cpu_temp(),
        bram_write_us=int(state["perf_bram_write_us"]),
        worst_overrun_us=int(state["perf_worst_overrun_us"]),
    )
    if _send(sock, pkt, addr):
        state["seq"] = (state["seq"] + 1) & 0xFFFF
    # reset window counters
    state["perf_tick_count"]       = 0
    state["perf_worst_overrun_us"] = 0
    state["perf_bram_write_us"]    = 0
    state["last_perf_tx"]          = now


def _send_state(sock, addr, state):
    in_replay = state.get("mode") == "replay"
    ptype = protocol.PKT_HEARTBEAT if (state["match_ended"] or in_replay) else protocol.PKT_STATE_UPDATE
    movement_mode = protocol.MOVEMENT_MODE_INTENT_ONLY if in_replay else protocol.MOVEMENT_MODE_POSE
    pkt = protocol.pack_node_packet(
        pkt_type=ptype, seq=state["seq"],
        x=state["x"], y=state["y"], angle=state["angle"],
        flags=state["input_flags"], movement_mode=movement_mode,
    )
    if _send(sock, pkt, addr):
        state["seq"]           = (state["seq"] + 1) & 0xFFFF
        state["last_state_tx"] = time.monotonic()
        state["input_flags"]   = 0   # consume shoot flag after sending


def _is_newer_seq(prev_seq, seq):
    if prev_seq is None:
        return True
    delta = (int(seq) - int(prev_seq)) & 0xFFFF
    return delta != 0 and delta <= 0x7FFF


def _is_newer_timestamp(prev_ts, timestamp):
    if prev_ts is None:
        return True
    return int(timestamp) >= int(prev_ts)

# ── packet handling ───────────────────────────────────────────────────────────
def _handle(data, state, bram):
    if len(data) < protocol.HEADER_SIZE:
        return
    pkt_type, seq, ts = protocol.unpack_header(data)
    state["last_rx"] = time.monotonic()

    if pkt_type == protocol.PKT_ACK:
        if not _is_newer_timestamp(state.get("last_ack_ts"), ts):
            return
        state["last_ack_ts"] = ts
        if len(data) < protocol.HEADER_SIZE + 1:
            return
        pid = struct.unpack_from("<B", data, protocol.HEADER_SIZE)[0]
        changed = pid != state["player_id"]
        state["registered"]  = True
        state["player_id"]   = pid
        state["match_ended"] = False
        state["bits"]        = []
        state["bits_mask"]   = 0
        state["players"]     = []
        state["input_flags"] = 0
        state["force_server_pose_sync"] = True
        state["input_suspended_until"] = time.monotonic() + MAP_SYNC_INPUT_GRACE_S
        state["last_game_state_seq"] = None
        # hard snap to origin on (re)registration
        state["x"] = state["y"] = 0.0
        state["angle"] = 0.0; state["angle_raw"] = 0
        _write_pose(bram, state)
        _write_sprites(bram, state)
        role = {0:"LOBBY",1:"RUNNER",2:"TAGGER"}.get(pid, f"P{pid}")
        if changed:
            print(f"[ACK] player_id={pid} role={role} mode={state['mode']} ts={ts}")
        else:
            print(f"[ACK] re-ack player_id={pid} role={role} mode={state['mode']}")

    elif pkt_type == protocol.PKT_MAP:
        if not _is_newer_timestamp(state.get("last_map_ts"), ts):
            return
        state["last_map_ts"] = ts
        w, h, tile_scale, tiles = protocol.unpack_map_packet(data)
        if w <= 0 or h <= 0 or tile_scale <= 0 or len(tiles) != (w * h):
            print(f"[HW] ignored malformed PKT_MAP ({w}x{h}, tiles={len(tiles)})")
            return
        state["map_w"] = w; state["map_h"] = h
        state["tile_scale"] = tile_scale
        state["tiles"]      = tiles
        state["match_ended"] = False
        state["bits"]        = []
        state["bits_mask"]   = 0
        state["players"]     = []
        state["input_flags"] = 0
        state["force_server_pose_sync"] = True
        state["input_suspended_until"] = time.monotonic() + MAP_SYNC_INPUT_GRACE_S
        state["last_game_state_seq"] = None
        if not _write_map(bram, tiles, w, h):
            return
        if not _walkable(state, state["x"], state["y"]):
            state["x"] = state["y"] = 0.0
            state["angle"] = 0.0; state["angle_raw"] = 0
            print("[HW] snapped to origin — inside wall after map change")
        _write_pose(bram, state)
        _write_sprites(bram, state)

    elif pkt_type == protocol.PKT_BITS_INIT:
        if not _is_newer_timestamp(state.get("last_bits_ts"), ts):
            return
        state["last_bits_ts"] = ts
        raw_bits = protocol.unpack_bits_init_packet(data)
        if raw_bits:
            max_id = max(b[0] for b in raw_bits)
            bits = [None] * (max_id + 1)
            for bit_id, bx, by in raw_bits:
                bits[bit_id] = (bx, by)
            state["bits"] = bits
        else:
            state["bits"] = []
        state["bits_mask"] = 0xFFFF
        state["sprites_dirty"] = True
        print(f"[BITS_INIT] count={len(raw_bits)}")

    elif pkt_type == protocol.PKT_NODE_MODE:
        if not _is_newer_timestamp(state.get("last_mode_ts"), ts):
            return
        state["last_mode_ts"] = ts
        mode_byte = protocol.unpack_node_mode_packet(data)
        new_mode  = protocol.decode_node_control_mode(mode_byte)
        if new_mode != state["mode"]:
            print(f"[CTRL] mode {state['mode']} -> {new_mode} (server request)")
            state["mode"] = new_mode
            if new_mode == "replay":
                state["last_game_state_seq"] = None  # reset so replay frames aren't dropped as stale
        else:
            print(f"[CTRL] mode confirmed: {state['mode']}")

    elif pkt_type == protocol.PKT_GAME_STATE:
        _, rx_seq, rx_ts, game_mode, players, bits_mask = protocol.unpack_server_packet(data)
        if not _is_newer_seq(state.get("last_game_state_seq"), rx_seq):
            return
        state["last_game_state_seq"] = rx_seq
        state["game_mode"]  = game_mode
        state["bits_mask"]  = bits_mask
        state["players"]    = players
        state["sprites_dirty"] = True  # entity positions + bits_mask changed

        _update_local_pose_from_server(state, players)

        now = time.monotonic()
        if now - state["last_log"] >= LOG_PERIOD_S:
            print(f"[STATE] tick={rx_seq} mode={state['mode']} players={len(players)} "
                  f"self_id={state['player_id']} "
                  f"pose=({state['x']:.2f},{state['y']:.2f},{math.degrees(state['angle']):.0f}°)")
            state["last_log"] = now

def _drain(sock, state, bram):
    while True:
        try:
            data, _ = sock.recvfrom(4096)
            _handle(data, state, bram)
        except BlockingIOError:
            return
        except Exception as e:
            print(f"[RX_ERR] {e}")

# ── manual input ──────────────────────────────────────────────────────────────
def _apply_manual_input(state, buttons):
    """Buttons own angle and position entirely — server never overrides this."""
    state["input_flags"] = 0
    if _input_is_temporarily_suspended(state):
        return
    
    #can change lefts and rights here by inverting +/- state 
    raw = buttons.read() & 0xF
    if raw & BTN_LEFT:      # physical BTN0 — right
        state["angle_raw"] = (state["angle_raw"] - state["turn_step"]) % ANGLE_STEPS
    if raw & BTN_RIGHT:     # physical BTN3 — left
        state["angle_raw"] = (state["angle_raw"] + state["turn_step"]) % ANGLE_STEPS
    state["angle"] = (state["angle_raw"] * 2.0 * math.pi / ANGLE_STEPS) % (2.0 * math.pi)

    move = 0.0
    if raw & BTN_FWD:  move += state["move_speed"]
    if raw & BTN_BACK: move -= state["move_speed"]
    if move:
        nx = state["x"] + move * math.cos(state["angle"])
        ny = state["y"] + move * math.sin(state["angle"])
        state["x"], state["y"] = _resolve_move(state, nx, ny)

# ── auto steering ─────────────────────────────────────────────────────────────
def _wrap(a):
    """Wrap angle to [-pi, pi]."""
    return (a + math.pi) % (2 * math.pi) - math.pi


def _input_is_temporarily_suspended(state):
    return time.monotonic() < float(state.get("input_suspended_until", 0.0) or 0.0)


def _choose_best_step_towards(state, x, y, angle, target, move_speed):
    desired_angle = math.atan2(target[1] - y, target[0] - x)
    best = None
    offsets = (0.0, 0.35, -0.35, 0.7, -0.7, 1.05, -1.05, math.pi)
    for offset in offsets:
        candidate_angle = _wrap(desired_angle + offset)
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


def _update_local_pose_from_server(state, players):
    player_id = state["player_id"]
    if player_id is None:
        return

    for player in players:
        if player["player_id"] != player_id:
            continue
        server_x = float(player["x"])
        server_y = float(player["y"])
        server_angle = float(player["angle"])
        was_ended = state["match_ended"]
        state["match_ended"] = bool(player["flags"] & protocol.FLAG_MATCH_END)
        if state["match_ended"] and not was_ended:
            print("[MATCH_END] halting movement")
        elif was_ended and not state["match_ended"]:
            print("[MATCH_RESET] movement re-enabled for new match")

        should_snap = (
            state.get("mode") == "replay"
            or bool(player["flags"] & protocol.FLAG_TAGGED)
            or bool(state.get("force_server_pose_sync"))
        )
        if not should_snap and state["mode"] == "auto":
            distance_error = math.hypot(server_x - state["x"], server_y - state["y"])
            angle_error = abs(_wrap(server_angle - state["angle"]))
            should_snap = (
                distance_error >= float(state.get("server_pose_snap_distance", SERVER_POSE_SNAP_DISTANCE))
                or angle_error >= float(state.get("server_pose_snap_angle", SERVER_POSE_SNAP_ANGLE))
            )

        if should_snap:
            state["x"] = server_x
            state["y"] = server_y
            state["angle"] = server_angle
            state["angle_raw"] = _hw_angle(server_angle)
            state["force_server_pose_sync"] = False
            if player["flags"] & protocol.FLAG_TAGGED:
                print(f"[TAGGED] snapped to spawn ({state['x']:.1f},{state['y']:.1f})")
        return


def _apply_auto_input(state):
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
        state["angle_raw"] = _hw_angle(next_angle)
        return

    nav_target = _path_step_target(state, state["x"], state["y"], target[0], target[1])
    next_x, next_y, next_angle, desired_angle = _choose_best_step_towards(
        state, state["x"], state["y"], state["angle"], nav_target, move_speed,
    )
    state["x"], state["y"], state["angle"] = next_x, next_y, next_angle
    state["angle_raw"] = _hw_angle(next_angle)

    if mode == "chase":
        distance = math.hypot(target[0] - state["x"], target[1] - state["y"])
        aligned = abs(_wrap(desired_angle - next_angle)) <= float(
            state.get("auto_tagger_shoot_arc", AUTO_TAGGER_SHOOT_ARC)
        )
        shoot_range = float(state.get("auto_tagger_shoot_range", AUTO_TAGGER_SHOOT_RANGE))
        shoot_period = int(state.get("auto_tagger_shoot_period_ticks", AUTO_TAGGER_SHOOT_PERIOD_TICKS))
        if aligned and distance <= shoot_range and (int(state.get("tick", 0)) % max(1, shoot_period) == 0):
            state["input_flags"] = protocol.FLAG_INPUT_SHOOT

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="PYNQ board client v4")
    parser.add_argument("--server",         default=SERVER_IP)
    parser.add_argument("--port",           type=int, default=SERVER_PORT)
    parser.add_argument("--overlay",        default=OVERLAY_PATH)
    parser.add_argument("--username",       default=os.environ.get("PYNQ_USERNAME", ""))
    parser.add_argument("--mode",           choices=["manual","auto","replay"],
                        default=os.environ.get("PYNQ_MODE", "manual"),
                        help="Initial mode; server can override via PKT_NODE_MODE")
    parser.add_argument("--role",           choices=["any","runner","tagger"], default="any")
    parser.add_argument("--tick-rate",      type=int, default=TICK_RATE)
    parser.add_argument("--send-rate",      type=int, default=SEND_RATE)
    parser.add_argument("--move-speed",     type=float, default=None)
    parser.add_argument("--turn-step",      type=int,   default=None)
    parser.add_argument("--no-hw",          action="store_true")
    args = parser.parse_args()

    role_map = {"any": protocol.ROLE_ANY, "runner": protocol.ROLE_RUNNER,
                "tagger": protocol.ROLE_TAGGER}

    tick_rate     = max(1, args.tick_rate)
    tick_interval = 1.0 / tick_rate
    send_interval = 1.0 / max(1, args.send_rate)
    # scale movement defaults if tick rate differs from 50 Hz baseline
    scale = 50.0 / tick_rate
    move_speed = args.move_speed if args.move_speed is not None else MOVE_SPEED * scale
    turn_step  = args.turn_step  if args.turn_step  is not None else max(1, int(round(TURN_STEP * scale)))

    print(f"[NET] target {args.server}:{args.port}")
    print(f"[CFG] username={args.username or '<none>'} mode={args.mode} role={args.role} "
          f"tick={tick_rate}Hz send={args.send_rate}Hz "
          f"move={move_speed:.3f} turn={turn_step}")

    if args.no_hw:
        print("[HW] --no-hw: null stubs")
        bram, buttons = _NullBram(), _NullButtons()
    else:
        _, bram, buttons = _load_overlay(args.overlay)

    tiles = _fallback_map()
    _write_map(bram, tiles, MAP_COLS, MAP_ROWS)

    state = {
        "username":       args.username,
        "mode":           args.mode,
        "preferred_role": role_map[args.role],
        "registered":     False,
        "player_id":      None,
        "seq":            0,
        "x": 0.0, "y": 0.0,
        "angle": 0.0, "angle_raw": 0,
        "input_flags":    0,
        "match_ended":    False,
        "game_mode":      protocol.GAME_MODE_CHASE,
        "map_w": MAP_COLS, "map_h": MAP_ROWS,
        "tile_scale":     8,
        "tiles":          tiles,
        "players":        [],
        "bits":           [],
        "bits_mask":      0,
        "move_speed":     move_speed,
        "turn_step":      turn_step,
        "auto_runner_speed": AUTO_RUNNER_SPEED,
        "auto_tagger_speed": AUTO_TAGGER_SPEED,
        "auto_fallback_speed": AUTO_FALLBACK_SPEED,
        "auto_tagger_shoot_range": AUTO_TAGGER_SHOOT_RANGE,
        "auto_tagger_shoot_arc": AUTO_TAGGER_SHOOT_ARC,
        "auto_tagger_shoot_period_ticks": AUTO_TAGGER_SHOOT_PERIOD_TICKS,
        "server_pose_snap_distance": SERVER_POSE_SNAP_DISTANCE,
        "server_pose_snap_angle": SERVER_POSE_SNAP_ANGLE,
        "force_server_pose_sync": True,
        "input_suspended_until": 0.0,
        "last_rx":        None,
        "last_reg_tx":    0.0,
        "last_state_tx":  0.0,
        "last_log":       0.0,
        "last_ack_ts":    None,
        "last_map_ts":    None,
        "last_bits_ts":   None,
        "last_mode_ts":   None,
        "last_game_state_seq": None,
        "tick":           0,
        "sprites_dirty":  True,  # flush once at startup, then on PKT_GAME_STATE / PKT_BITS_INIT
        # perf telemetry window (reset after each PKT_PERF send)
        "last_perf_tx":          0.0,
        "perf_tick_count":       0,
        "perf_worst_overrun_us": 0,
        "perf_bram_write_us":    0,
    }

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)
    sock.setblocking(False)
    addr = (args.server, args.port)

    next_tick = time.monotonic()

    try:
        while True:
            next_tick += tick_interval
            now = time.monotonic()
            state["tick"] += 1

            _drain(sock, state, bram)

            # silence timeout
            if state["registered"] and state["last_rx"] and \
                    now - state["last_rx"] > SERVER_SILENCE_S:
                print("[NET] server silent — re-registering")
                state["registered"]    = False
                state["player_id"]     = None
                state["players"]       = []
                state["sprites_dirty"] = True

            bram_t0 = time.monotonic()
            if state["match_ended"]:
                # halted — just keep the BRAM current and send heartbeats
                _write_pose(bram, state)
            elif state["mode"] == "replay":
                _write_pose(bram, state)
            elif state["mode"] == "auto":
                _apply_auto_input(state)
                # auto mode steers locally and only snaps from server on big mismatches
                _write_pose(bram, state)
            else:
                _apply_manual_input(state, buttons)
                _write_pose(bram, state)

            # sprite BRAM — write only when something changed (dirty flag set in _handle).
            # PKT_GAME_STATE and PKT_BITS_INIT both set sprites_dirty; ACK/MAP flush inline.
            if state["sprites_dirty"]:
                _write_sprites(bram, state)
                state["sprites_dirty"] = False
            state["perf_bram_write_us"] = int((time.monotonic() - bram_t0) * 1e6)

            state["perf_tick_count"] += 1

            if not state["registered"]:
                if now - state["last_reg_tx"] >= REGISTER_RETRY_S:
                    _send_register(sock, addr, state)
            else:
                # Wait for PKT_MAP before sending poses — avoids phantom collisions
                # from the server using lobby-map collision against pre-map coordinates.
                if state["last_map_ts"] is not None and now - state["last_state_tx"] >= send_interval:
                    _send_state(sock, addr, state)
                if now - state["last_perf_tx"] >= 2.0:
                    _send_perf(sock, addr, state)

            sleep = next_tick - time.monotonic()
            overrun_us = int(-sleep * 1e6)  # positive = overran, negative = slack
            if overrun_us > state["perf_worst_overrun_us"]:
                state["perf_worst_overrun_us"] = overrun_us
            if sleep > 0:
                time.sleep(sleep)
            elif sleep < -tick_interval:
                next_tick = time.monotonic()  # fell behind — reset

    except KeyboardInterrupt:
        print("\n[NET] stopped")
    finally:
        sock.close()

if __name__ == "__main__":
    main()
