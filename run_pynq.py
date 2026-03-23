#!/usr/bin/env python3
# PYNQ-Z1 raycaster client.
#
# Modes:
#   manual  — 100% local authority; buttons drive movement and collision.
#   auto    — local board steering, server resync on tag / map-reset / large divergence.
#   replay  — server-driven playback; buttons ignored, pose streamed from UDP.
#   All modes write collision + sprite BRAM every tick.
#
# Hardware (design_1_wrapper.bit):
#   ray_caster_0  Q6.16 fixed-point, ≥2 sprite slots (v_sprite_* + v_r_sprite_*),
#                 up to MAX_ENTITIES (4) written to BRAM each tick.
#   axi_gpio_0    4-bit (BTN_LEFT / RIGHT / FWD / BACK).
#
# Copy to board:
#   scp ./* xilinx@<PYNQ_IP>:/home/xilinx/jupyter_notebooks/Final_project_test/
#
# Run:
#   python3 run_pynq.py [--mode auto] [--username NAME] [--no-hw]

import argparse
import os
import socket
import time

import protocol
from hardware import (
    _NullBram, _NullButtons, _load_overlay,
    _write_map, _write_pose, _write_sprites,
    _fallback_map,
    MAP_ROWS, MAP_COLS,
)
from network import _send_register, _send_state, _send_perf, _drain
from input_handlers import _apply_manual_input, _apply_auto_input

# ── config ────────────────────────────────────────────────────────────────────
SERVER_IP    = "3.9.71.204"
SERVER_PORT  = 9000
OVERLAY_PATH = "/home/xilinx/jupyter_notebooks/Final_project_test/design_1_wrapper.bit"
CLOCK_MHZ    = 50.0
TICK_RATE    = 60        # Hz — main loop rate, aligned with server authoritative tick
SEND_RATE    = 60        # Hz — state update rate to server to reduce board-side jitter
REGISTER_RETRY_S = 2.0
SERVER_SILENCE_S = 5.0

# ── movement config ───────────────────────────────────────────────────────────
MOVE_SPEED   = 0.25    # world units per tick at 50 Hz baseline; auto-rescaled for runtime tick rate
TURN_STEP    = 26      # angle units per tick at 50 Hz baseline; auto-rescaled for runtime tick rate
AUTO_RUNNER_SPEED    = 0.10
AUTO_TAGGER_SPEED    = 0.11
AUTO_FALLBACK_SPEED  = 0.09
AUTO_TAGGER_SHOOT_RANGE        = 26.0
AUTO_TAGGER_SHOOT_ARC          = 0.4
AUTO_TAGGER_SHOOT_PERIOD_TICKS = 4
SERVER_POSE_SNAP_DISTANCE = 8.0
SERVER_POSE_SNAP_ANGLE    = 0.75

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
        "sprites_dirty":  True,
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
                _write_pose(bram, state)
            elif state["mode"] == "replay":
                _write_pose(bram, state)
            elif state["mode"] == "auto":
                _apply_auto_input(state)
                _write_pose(bram, state)
            else:
                _apply_manual_input(state, buttons)
                _write_pose(bram, state)

            if state["sprites_dirty"]:
                _write_sprites(bram, state)
                state["sprites_dirty"] = False
            state["perf_bram_write_us"] = int((time.monotonic() - bram_t0) * 1e6)

            state["perf_tick_count"] += 1

            if not state["registered"]:
                if now - state["last_reg_tx"] >= REGISTER_RETRY_S:
                    _send_register(sock, addr, state)
            else:
                if state["last_map_ts"] is not None and now - state["last_state_tx"] >= send_interval:
                    _send_state(sock, addr, state)
                if now - state["last_perf_tx"] >= 2.0:
                    _send_perf(sock, addr, state)

            sleep = next_tick - time.monotonic()
            overrun_us = int(-sleep * 1e6)
            if overrun_us > state["perf_worst_overrun_us"]:
                state["perf_worst_overrun_us"] = overrun_us
            if sleep > 0:
                time.sleep(sleep)
            elif sleep < -tick_interval:
                next_tick = time.monotonic()

    except KeyboardInterrupt:
        print("\n[NET] stopped")
    finally:
        sock.close()

if __name__ == "__main__":
    main()
