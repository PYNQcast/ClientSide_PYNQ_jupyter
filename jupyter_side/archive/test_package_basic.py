#!/usr/bin/env python3
# test_package_basic.py — minimal PYNQ board client, manual mode only.
# No reconciliation, no auto mode, no pathfinding.
# Server pose is NEVER applied to local state — buttons own position/angle entirely.
# Hard events (ACK, PKT_MAP) still snap position to origin as before.
#
# Copy to board:
#   scp jupyter_side/test_package_basic.py pynq_full/interfacing/protocol.py \
#       xilinx@<PYNQ_IP>:/home/xilinx/jupyter_notebooks/Final_project_test/
#
# Run on board:
#   cd /home/xilinx/jupyter_notebooks/Final_project_test
#   python3 test_package_basic.py [--username NAME]

import argparse
import errno
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
SERVER_IP   = "3.9.71.204"
SERVER_PORT = 9000
OVERLAY_PATH = "/home/xilinx/jupyter_notebooks/Final_project_test/design_1_wrapper.bit"
CLOCK_MHZ   = 50.0
TICK_RATE   = 50          # Hz
SEND_RATE   = 20          # Hz
REGISTER_RETRY_S  = 2.0
SERVER_SILENCE_S  = 5.0
LOG_PERIOD_S      = 1.0

# BRAM offsets
MAP_ROWS = MAP_COLS = 32
PLAYER_POS_OFFSET   = 32 * 4
PLAYER_ANGLE_OFFSET = 33 * 4
COORD_FRAC_BITS = 10
ANGLE_STEPS     = 1 << 12
ANGLE_MASK      = ANGLE_STEPS - 1

# Movement
MOVE_SPEED  = 0.08   # world units per tick at 50 Hz
TURN_STEP   = 26     # angle units per tick at 50 Hz
COLLISION_R = 2.5
BTN_RIGHT   = 1 << 0
BTN_BACK    = 1 << 1
BTN_FWD     = 1 << 2
BTN_LEFT    = 1 << 3

# ── hw stubs ──────────────────────────────────────────────────────────────────
class _NullBram:
    def write(self, offset, value): pass

class _NullButtons:
    def read(self): return 0

# ── hw helpers ────────────────────────────────────────────────────────────────
def _load_overlay(path):
    if Overlay is None:
        raise SystemExit("pynq package not found — use --no-hw for PC testing")
    overlay  = Overlay(path)
    bram     = overlay.axi_bram_ctrl_0
    buttons  = overlay.axi_gpio_0.channel1
    Clocks.fclk0_mhz = CLOCK_MHZ
    time.sleep(0.1)
    return overlay, bram, buttons

def _world_to_q6_10(v, tile_scale, dim):
    raw = int(round(((v / tile_scale) + dim / 2.0) * (1 << COORD_FRAC_BITS)))
    return max(0, min((dim << COORD_FRAC_BITS) - 1, raw))

def _angle_to_hw(a):
    return int(round((a % (2 * math.pi)) * ANGLE_STEPS / (2 * math.pi))) & ANGLE_MASK

def _write_map(bram, tiles, w, h):
    for row in range(h):
        word = 0
        for col in range(w):
            if tiles[row * w + col]:
                word |= 1 << (w - 1 - col)
        bram.write(row * 4, word & 0xFFFFFFFF)
    print(f"[HW] map written ({w}x{h})")

def _write_pose(bram, state):
    xq = _world_to_q6_10(state["x"], state["tile_scale"], state["map_w"])
    yq = _world_to_q6_10(state["y"], state["tile_scale"], state["map_h"])
    bram.write(PLAYER_POS_OFFSET,   ((xq & 0xFFFF) << 16) | (yq & 0xFFFF))
    bram.write(PLAYER_ANGLE_OFFSET, state["angle_raw"] & ANGLE_MASK)

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
    if _walkable(state, nx, ny):           return nx, ny
    if _walkable(state, nx, state["y"]):   return nx, state["y"]
    if _walkable(state, state["x"], ny):   return state["x"], ny
    return state["x"], state["y"]

# ── lobby map ─────────────────────────────────────────────────────────────────
def _fallback_map():
    tiles = bytearray(MAP_ROWS * MAP_COLS)
    for c in range(MAP_COLS):
        tiles[c] = tiles[(MAP_ROWS-1)*MAP_COLS+c] = 1
    for r in range(MAP_ROWS):
        tiles[r*MAP_COLS] = tiles[r*MAP_COLS+MAP_COLS-1] = 1
    return tiles

# ── network helpers ───────────────────────────────────────────────────────────
def _send(sock, pkt, addr, state):
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
        preferred_role=protocol.ROLE_ANY, username=state["username"],
        movement_mode=protocol.MOVEMENT_MODE_POSE,
    )
    if _send(sock, pkt, addr, state):
        print(f"[TX] REGISTER seq={state['seq']} username={state['username'] or '<none>'}")
        state["seq"] = (state["seq"] + 1) & 0xFFFF
        state["last_reg_tx"] = time.monotonic()

def _send_state(sock, addr, state):
    ptype = protocol.PKT_HEARTBEAT if state["match_ended"] else protocol.PKT_STATE_UPDATE
    pkt = protocol.pack_node_packet(
        pkt_type=ptype, seq=state["seq"],
        x=state["x"], y=state["y"], angle=state["angle"],
        flags=0, movement_mode=protocol.MOVEMENT_MODE_POSE,
    )
    if _send(sock, pkt, addr, state):
        state["seq"] = (state["seq"] + 1) & 0xFFFF
        state["last_state_tx"] = time.monotonic()

# ── packet handling ───────────────────────────────────────────────────────────
def _handle(data, state, bram):
    if len(data) < protocol.HEADER_SIZE:
        return
    pkt_type, seq, ts = protocol.unpack_header(data)
    state["last_rx"] = time.monotonic()

    if pkt_type == protocol.PKT_ACK:
        if len(data) < protocol.HEADER_SIZE + 1:
            return
        pid = struct.unpack_from("<B", data, protocol.HEADER_SIZE)[0]
        if pid != state["player_id"]:
            print(f"[ACK] player_id={pid} ts={ts}")
        state["registered"]  = True
        state["player_id"]   = pid
        state["match_ended"] = False
        # snap to origin on fresh registration
        state["x"] = state["y"] = 0.0
        state["angle"] = 0.0
        state["angle_raw"] = 0
        _write_pose(bram, state)

    elif pkt_type == protocol.PKT_MAP:
        w, h, tile_scale, tiles = protocol.unpack_map_packet(data)
        state["map_w"] = w
        state["map_h"] = h
        state["tile_scale"] = tile_scale
        state["tiles"] = tiles
        state["match_ended"] = False
        _write_map(bram, tiles, w, h)
        if not _walkable(state, state["x"], state["y"]):
            state["x"] = state["y"] = 0.0
            state["angle"] = 0.0; state["angle_raw"] = 0
            print("[HW] snapped to origin — inside wall after map change")
        _write_pose(bram, state)

    elif pkt_type == protocol.PKT_GAME_STATE:
        _, rx_seq, rx_ts, game_mode, players, bits_mask = protocol.unpack_server_packet(data)
        state["players"] = players
        # find match_end flag for self
        for p in players:
            if p["player_id"] == state["player_id"]:
                state["match_ended"] = bool(p["flags"] & protocol.FLAG_MATCH_END)
                break
        now = time.monotonic()
        if now - state["last_log"] >= LOG_PERIOD_S:
            print(f"[STATE] tick={rx_seq} players={len(players)} "
                  f"self_id={state['player_id']} "
                  f"pose=({state['x']:.2f},{state['y']:.2f},{state['angle']:.2f})")
            state["last_log"] = now

    elif pkt_type == protocol.PKT_NODE_MODE:
        # ignore — always manual
        pass

def _drain(sock, state, bram):
    while True:
        try:
            data, _ = sock.recvfrom(4096)
            _handle(data, state, bram)
        except BlockingIOError:
            return
        except Exception as e:
            print(f"[RX_ERR] {e}")

# ── input ─────────────────────────────────────────────────────────────────────
def _apply_input(state, buttons):
    raw = buttons.read() & 0xF
    turn_step = state["turn_step"]
    if raw & BTN_LEFT:
        state["angle_raw"] = (state["angle_raw"] - turn_step) % ANGLE_STEPS
    if raw & BTN_RIGHT:
        state["angle_raw"] = (state["angle_raw"] + turn_step) % ANGLE_STEPS
    state["angle"] = (state["angle_raw"] * 2.0 * math.pi / ANGLE_STEPS) % (2.0 * math.pi)

    move = 0.0
    if raw & BTN_FWD:  move += state["move_speed"]
    if raw & BTN_BACK: move -= state["move_speed"]
    if move:
        nx = state["x"] + move * math.cos(state["angle"])
        ny = state["y"] + move * math.sin(state["angle"])
        state["x"], state["y"] = _resolve_move(state, nx, ny)

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server",   default=SERVER_IP)
    parser.add_argument("--port",     type=int, default=SERVER_PORT)
    parser.add_argument("--overlay",  default=OVERLAY_PATH)
    parser.add_argument("--username", default=os.environ.get("PYNQ_USERNAME", ""))
    parser.add_argument("--no-hw",    action="store_true")
    args = parser.parse_args()

    if args.no_hw:
        print("[HW] --no-hw: null stubs")
        bram, buttons = _NullBram(), _NullButtons()
    else:
        _, bram, buttons = _load_overlay(args.overlay)

    tiles = _fallback_map()
    _write_map(bram, tiles, MAP_COLS, MAP_ROWS)

    state = {
        "username":    args.username,
        "registered":  False,
        "player_id":   None,
        "seq":         0,
        "x": 0.0, "y": 0.0,
        "angle": 0.0, "angle_raw": 0,
        "match_ended": False,
        "map_w": MAP_COLS, "map_h": MAP_ROWS,
        "tile_scale": 8,
        "tiles": tiles,
        "players": [],
        "move_speed":  MOVE_SPEED,
        "turn_step":   TURN_STEP,
        "last_rx":     None,
        "last_reg_tx": 0.0,
        "last_state_tx": 0.0,
        "last_log":    0.0,
    }

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)
    sock.setblocking(False)
    addr = (args.server, args.port)

    print(f"[NET] target {args.server}:{args.port}")
    tick_interval  = 1.0 / TICK_RATE
    state_interval = 1.0 / SEND_RATE
    next_tick = time.monotonic()

    try:
        while True:
            next_tick += tick_interval
            now = time.monotonic()

            _drain(sock, state, bram)

            # silence timeout → re-register
            if state["registered"] and state["last_rx"] and now - state["last_rx"] > SERVER_SILENCE_S:
                print("[NET] server silent — re-registering")
                state["registered"] = False
                state["player_id"]  = None

            _apply_input(state, buttons)
            _write_pose(bram, state)

            if not state["registered"]:
                if now - state["last_reg_tx"] >= REGISTER_RETRY_S:
                    _send_register(sock, addr, state)
            else:
                if now - state["last_state_tx"] >= state_interval:
                    _send_state(sock, addr, state)

            sleep = next_tick - time.monotonic()
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
