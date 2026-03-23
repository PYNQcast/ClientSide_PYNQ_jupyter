"""Socket send/receive helpers and inbound packet handling."""

import errno
import math
import struct
import time

import protocol
from hardware import _write_pose, _write_sprites, _hw_angle, _write_map
from movement import _walkable

# ── timing config (mirrors run_pynq.py section 1) ────────────────────────────
MAP_SYNC_INPUT_GRACE_S = 0.35
LOG_PERIOD_S           = 1.0
SERVER_POSE_SNAP_DISTANCE = 8.0
SERVER_POSE_SNAP_ANGLE    = 0.75


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


# ── pose sync (used by _handle and input_handlers) ───────────────────────────
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


def _wrap(a):
    """Wrap angle to [-pi, pi]."""
    return (a + math.pi) % (2 * math.pi) - math.pi
