#!/usr/bin/env python3

import math
import socket
import struct
import time

try:
    from pynq import Overlay
    from pynq.ps import Clocks
except ImportError as exc:
    raise SystemExit(
        "test_package_v1.py must run on the PYNQ board with the pynq package installed"
    ) from exc

import protocol


# Keep protocol.py beside this file on the board.
#
# Copy to board:
#   scp ref_files/test_package_v1.py pynq_full/interfacing/protocol.py \
#       xilinx@<PYNQ_IP>:/home/xilinx/jupyter_notebooks/
#
# Run on board:
#   cd /home/xilinx/jupyter_notebooks
#   python3 test_package_v1.py


# --- Network configuration ---
SERVER_IP = "3.9.71.204"
SERVER_PORT = 9000
USERNAME = ""
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
BUTTON_TURN_RIGHT_MASK = 1 << 0
BUTTON_BACKWARD_MASK = 1 << 1
BUTTON_FORWARD_MASK = 1 << 2
BUTTON_TURN_LEFT_MASK = 1 << 3


def _role_name(player_id: int) -> str:
    if player_id == 0:
        return "LOBBY"
    if player_id == 1:
        return "RUNNER"
    if player_id == 2:
        return "TAGGER"
    return f"PLAYER_{player_id}"


def _describe_packet(data: bytes) -> str:
    if len(data) < protocol.HEADER_SIZE:
        return f"short packet ({len(data)} bytes)"
    pkt_type, seq, timestamp = protocol.unpack_header(data)
    return f"type=0x{pkt_type:04x} seq={seq} ts={timestamp} len={len(data)}"


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
                word |= 1 << col
        rows.append(word)
    return rows


def _world_to_hw_q6_10(value: float, tile_scale: int, map_dim: int) -> int:
    tile_units = (value / tile_scale) + (map_dim / 2.0)
    raw = int(round(tile_units * (1 << HW_COORD_FRAC_BITS)))
    max_raw = (map_dim << HW_COORD_FRAC_BITS) - 1
    return max(0, min(max_raw, raw))


def _radians_to_hw_angle(angle_radians: float) -> int:
    turn = angle_radians % (2.0 * math.pi)
    return int(round(turn * HW_ANGLE_STEPS / (2.0 * math.pi))) & HW_ANGLE_MASK


def _load_overlay():
    print(f"[HW] loading overlay {OVERLAY_PATH}")
    overlay = Overlay(OVERLAY_PATH)
    bram = overlay.axi_bram_ctrl_0
    buttons = overlay.axi_gpio_0.channel1
    Clocks.fclk0_mhz = CLOCK_MHZ
    time.sleep(0.1)
    print(f"[HW] overlay ready, fclk0={CLOCK_MHZ:.1f}MHz")
    return overlay, bram, buttons


def _write_map_to_bram(bram, width: int, height: int, tiles: bytes):
    rows = _encode_map_rows_for_bram(width, height, tiles)
    for row_index, word in enumerate(rows):
        bram.write(row_index * 4, int(word) & 0xFFFFFFFF)
    print(f"[HW] wrote {len(rows)} map rows to BRAM ({width}x{height})")


def _write_pose_to_bram(bram, state: dict):
    x_q = _world_to_hw_q6_10(state["x"], state["tile_scale"], state["map_w"])
    y_q = _world_to_hw_q6_10(state["y"], state["tile_scale"], state["map_h"])
    bram.write(HW_PLAYER_POS_OFFSET, ((x_q & 0xFFFF) << 16) | (y_q & 0xFFFF))
    bram.write(HW_PLAYER_ANGLE_OFFSET, state["angle_raw"] & HW_ANGLE_MASK)


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


def _apply_manual_input(state: dict, buttons) -> None:
    raw = buttons.read() & 0xF
    if raw & BUTTON_TURN_LEFT_MASK:
        state["angle_raw"] = (state["angle_raw"] + TURN_STEP) % HW_ANGLE_STEPS
    if raw & BUTTON_TURN_RIGHT_MASK:
        state["angle_raw"] = (state["angle_raw"] - TURN_STEP) % HW_ANGLE_STEPS

    state["angle"] = (state["angle_raw"] * (2.0 * math.pi / HW_ANGLE_STEPS)) % (2.0 * math.pi)

    move_step = 0.0
    if raw & BUTTON_FORWARD_MASK:
        move_step += MOVE_SPEED
    if raw & BUTTON_BACKWARD_MASK:
        move_step -= MOVE_SPEED
    if move_step == 0.0:
        return

    desired_x = state["x"] + move_step * math.cos(state["angle"])
    desired_y = state["y"] + move_step * math.sin(state["angle"])
    state["x"], state["y"] = _resolve_move(state, desired_x, desired_y)


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


def _send_state(sock: socket.socket, server_address, state: dict):
    pkt_type = protocol.PKT_HEARTBEAT if state["match_ended"] else protocol.PKT_STATE_UPDATE
    pkt = protocol.pack_node_packet(
        pkt_type=pkt_type,
        seq=state["seq"],
        x=state["x"],
        y=state["y"],
        angle=state["angle"],
        flags=0,
        movement_mode=protocol.MOVEMENT_MODE_POSE,
    )
    sock.sendto(pkt, server_address)
    state["seq"] = (state["seq"] + 1) & 0xFFFF


def _update_local_pose_from_server(state: dict, players) -> None:
    player_id = state["player_id"]
    if player_id is None:
        return
    for player in players:
        if player["player_id"] != player_id:
            continue
        state["x"] = player["x"]
        state["y"] = player["y"]
        state["angle"] = player["angle"]
        state["angle_raw"] = _radians_to_hw_angle(player["angle"])
        state["match_ended"] = bool(player["flags"] & protocol.FLAG_MATCH_END)
        return


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
        if previous_player_id != player_id:
            print(f"[ACK] player_id={player_id} role={_role_name(player_id)} header_seq={seq} ts={timestamp}")
        return

    if pkt_type == protocol.PKT_MAP:
        width, height, tile_scale, tiles = protocol.unpack_map_packet(data)
        state["map_w"] = width
        state["map_h"] = height
        state["tile_scale"] = tile_scale
        state["tiles"] = tiles
        _write_map_to_bram(bram, width, height, tiles)
        return

    if pkt_type == protocol.PKT_BITS_INIT:
        bits = protocol.unpack_bits_init_packet(data)
        print(f"[BITS_INIT] count={len(bits)}")
        return

    if pkt_type == protocol.PKT_GAME_STATE:
        _, rx_seq, rx_ts, game_mode, players, bits_mask = protocol.unpack_server_packet(data)
        state["game_mode"] = game_mode
        state["bits_mask"] = bits_mask
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


def main():
    print(f"[NET] EC2 target {SERVER_IP}:{SERVER_PORT}")
    print(
        f"[CFG] username={USERNAME or '<none>'} role={PREFERRED_ROLE} "
        f"tick_rate={TICK_RATE} overlay={OVERLAY_PATH}"
    )

    overlay, bram, buttons = _load_overlay()
    _ = overlay

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    server_address = (SERVER_IP, SERVER_PORT)

    state = {
        "registered": False,
        "player_id": None,
        "seq": 0,
        "x": INITIAL_X,
        "y": INITIAL_Y,
        "angle_raw": INITIAL_ANGLE_RAW & HW_ANGLE_MASK,
        "angle": (INITIAL_ANGLE_RAW & HW_ANGLE_MASK) * (2.0 * math.pi / HW_ANGLE_STEPS),
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

            _apply_manual_input(state, buttons)
            _write_pose_to_bram(bram, state)

            if not state["registered"]:
                if now - state["last_register_tx_at"] >= REGISTER_RETRY_S:
                    _send_register(sock, server_address, state)
            else:
                _send_state(sock, server_address, state)

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
