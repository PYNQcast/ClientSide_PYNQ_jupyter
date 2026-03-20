#!/usr/bin/env python3
"""Minimal local runtime helpers for PYNQ RTT/button-to-visible tests.

This is a self-contained subset of jupyter_side/run_pynq.py so the
benchmark folder can be copied to a board on its own.
"""

from __future__ import annotations

import errno
import math
import socket
import struct
import time

try:
    from pynq import Overlay
    from pynq.ps import Clocks
except ImportError:
    Overlay = None
    Clocks = None

try:
    from . import protocol as protocol
except ImportError:
    import os as _os, sys as _sys
    _d = _os.path.dirname(_os.path.abspath(__file__))
    if _d not in _sys.path:
        _sys.path.insert(0, _d)
    import protocol

SERVER_IP = "3.9.71.204"
SERVER_PORT = 9000
OVERLAY_PATH = "/home/xilinx/jupyter_notebooks/Final_project_test/design_1_wrapper.bit"
CLOCK_MHZ = 50.0
TICK_RATE = 60
SEND_RATE = 60
REGISTER_RETRY_S = 2.0
SERVER_SILENCE_S = 5.0
LOG_PERIOD_S = 1.0

MAP_ROWS = MAP_COLS = 32
PLAYER_POS_OFFSET = 32 * 4
PLAYER_ANGLE_OFFSET = 33 * 4
COORD_FRAC_BITS = 10
ANGLE_STEPS = 1 << 12
ANGLE_MASK = ANGLE_STEPS - 1

MOVE_SPEED = 0.25
TURN_STEP = 26
COLLISION_R = 2.0

BTN_LEFT = 1 << 0
BTN_BACK = 1 << 1
BTN_FWD = 1 << 2
BTN_RIGHT = 1 << 3


class _NullBram:
    def write(self, offset, value):
        return None


class _NullButtons:
    def read(self):
        return 0


def _load_overlay(path):
    if Overlay is None:
        raise SystemExit("pynq package not found — use --no-hw for PC testing")
    overlay = Overlay(path)
    bram = overlay.axi_bram_ctrl_0
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


def _write_map(bram, tiles, w, h):
    if w <= 0 or h <= 0 or len(tiles) < (w * h):
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
    return True


def _write_pose(bram, state):
    ts = state["tile_scale"]
    w, h = state["map_w"], state["map_h"]
    bram.write(PLAYER_POS_OFFSET, _xy_word(state["x"], state["y"], ts, w, h))
    bram.write(PLAYER_ANGLE_OFFSET, state["angle_raw"] & ANGLE_MASK)


def _walkable(state, x, y):
    tiles = state["tiles"]
    w, h, s = state["map_w"], state["map_h"], state["tile_scale"]
    if not tiles or w <= 0 or h <= 0:
        return True
    for dx, dy in [(0, 0), (COLLISION_R, 0), (-COLLISION_R, 0), (0, COLLISION_R), (0, -COLLISION_R)]:
        col = int(math.floor((x + dx) / s + w / 2.0))
        row = int(math.floor((y + dy) / s + h / 2.0))
        if not (0 <= col < w and 0 <= row < h):
            return False
        if tiles[row * w + col]:
            return False
    return True


def _resolve_move(state, nx, ny):
    if _walkable(state, nx, ny):
        return nx, ny
    if _walkable(state, nx, state["y"]):
        return nx, state["y"]
    if _walkable(state, state["x"], ny):
        return state["x"], ny
    return state["x"], state["y"]


def _fallback_map():
    tiles = bytearray(MAP_ROWS * MAP_COLS)
    for c in range(MAP_COLS):
        tiles[c] = tiles[(MAP_ROWS - 1) * MAP_COLS + c] = 1
    for r in range(MAP_ROWS):
        tiles[r * MAP_COLS] = tiles[r * MAP_COLS + MAP_COLS - 1] = 1
    return tiles


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
        seq=state["seq"],
        x=state["x"],
        y=state["y"],
        angle=state["angle"],
        preferred_role=state["preferred_role"],
        username=state["username"],
        movement_mode=protocol.MOVEMENT_MODE_POSE,
    )
    if _send(sock, pkt, addr):
        state["seq"] = (state["seq"] + 1) & 0xFFFF
        state["last_reg_tx"] = time.monotonic()


def _send_state(sock, addr, state):
    pkt = protocol.pack_node_packet(
        pkt_type=protocol.PKT_STATE_UPDATE,
        seq=state["seq"],
        x=state["x"],
        y=state["y"],
        angle=state["angle"],
        flags=state["input_flags"],
        movement_mode=protocol.MOVEMENT_MODE_POSE,
    )
    if _send(sock, pkt, addr):
        state["seq"] = (state["seq"] + 1) & 0xFFFF
        state["last_state_tx"] = time.monotonic()
        state["input_flags"] = 0


def _is_newer_seq(prev_seq, seq):
    if prev_seq is None:
        return True
    delta = (int(seq) - int(prev_seq)) & 0xFFFF
    return delta != 0 and delta <= 0x7FFF


def _is_newer_timestamp(prev_ts, timestamp):
    if prev_ts is None:
        return True
    return int(timestamp) >= int(prev_ts)


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
        state["registered"] = True
        state["player_id"] = pid
        state["match_ended"] = False
        state["bits"] = []
        state["bits_mask"] = 0
        state["players"] = []
        state["input_flags"] = 0
        state["force_server_pose_sync"] = True
        state["last_game_state_seq"] = None
        state["x"] = state["y"] = 0.0
        state["angle"] = 0.0
        state["angle_raw"] = 0
        _write_pose(bram, state)
        return

    if pkt_type == protocol.PKT_MAP:
        if not _is_newer_timestamp(state.get("last_map_ts"), ts):
            return
        state["last_map_ts"] = ts
        w, h, tile_scale, tiles = protocol.unpack_map_packet(data)
        if w <= 0 or h <= 0 or tile_scale <= 0 or len(tiles) != (w * h):
            return
        state["map_w"] = w
        state["map_h"] = h
        state["tile_scale"] = tile_scale
        state["tiles"] = tiles
        _write_map(bram, tiles, w, h)
        _write_pose(bram, state)
        return

    if pkt_type == protocol.PKT_GAME_STATE:
        _, rx_seq, rx_ts, game_mode, players, bits_mask = protocol.unpack_server_packet(data)
        if not _is_newer_seq(state.get("last_game_state_seq"), rx_seq):
            return
        state["last_game_state_seq"] = rx_seq
        state["last_game_state_ts"] = int(rx_ts)  # server wall-clock ms (truncated 32-bit)
        state["game_mode"] = game_mode
        state["bits_mask"] = bits_mask
        state["players"] = players
        _update_local_pose_from_server(state, players)


def _input_is_temporarily_suspended(state):
    return time.monotonic() < float(state.get("input_suspended_until", 0.0) or 0.0)


def _apply_manual_input(state, buttons):
    state["input_flags"] = 0
    if _input_is_temporarily_suspended(state):
        return
    raw = buttons.read() & 0xF
    if raw & BTN_LEFT:
        state["angle_raw"] = (state["angle_raw"] - state["turn_step"]) % ANGLE_STEPS
    if raw & BTN_RIGHT:
        state["angle_raw"] = (state["angle_raw"] + state["turn_step"]) % ANGLE_STEPS
    state["angle"] = (state["angle_raw"] * 2.0 * math.pi / ANGLE_STEPS) % (2.0 * math.pi)

    move = 0.0
    if raw & BTN_FWD:
        move += state["move_speed"]
    if raw & BTN_BACK:
        move -= state["move_speed"]
    if move:
        nx = state["x"] + move * math.cos(state["angle"])
        ny = state["y"] + move * math.sin(state["angle"])
        state["x"], state["y"] = _resolve_move(state, nx, ny)


def _update_local_pose_from_server(state, players):
    player_id = state["player_id"]
    if player_id is None:
        return
    for player in players:
        if player["player_id"] != player_id:
            continue
        state["x"] = float(player["x"])
        state["y"] = float(player["y"])
        state["angle"] = float(player["angle"])
        state["angle_raw"] = _hw_angle(state["angle"])
        return
