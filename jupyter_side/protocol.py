# pynq_full/interfacing/protocol.py — UDP packet format (node ↔ server).
# Asserts catch size mismatches at startup. Used by PYNQ board code and EC2 server.

import struct
import time

# ── Packet types ──────────────────────────────────────────────────────────────

PKT_STATE_UPDATE = 0x0001   # node  → server: position this tick
PKT_GAME_STATE   = 0x0002   # server → node:  all player positions
PKT_HEARTBEAT    = 0x0010   # node  → server: keepalive
PKT_REGISTER     = 0x0020   # node  → server: first contact, triggers ACK
                             #   optional trailer after the 24-byte NodePacket:
                             #   username_len (1 byte) + UTF-8 username bytes

PKT_ACK          = 0x0030   # server → node:  confirms registration
                             #   byte 8 = player_id (1=RUNNER, 2=TAGGER) — wait for this
                             #   before sending STATE_UPDATEs. No node-to-node comms needed;
                             #   opponent position arrives in every PKT_GAME_STATE broadcast.
                             
PKT_MAP          = 0x0040   # server → node:  map tile data, sent once after PKT_ACK
                             #   header (8 bytes) + MapHeader (4 bytes) + tiles (width*height bytes)
                             #   node stores tiles in DRAM; FPGA raycaster reads them each frame.

PKT_BITS_INIT    = 0x0050   # server → node:  bit positions, sent once at match start
                             #   header (8 bytes) + count (1 byte) + N × BitEntry (6 bytes each)
                             #   node stores positions; server sends bitmask each tick to flag collection.
PKT_NODE_MODE    = 0x0060   # server → node:  runtime control mode switch (manual/auto/replay)
PKT_PERF         = 0x0070   # node  → server: board performance telemetry (sent ~every 2 s)
                             #   header (8 bytes) + PerfPayload (8 bytes)
                             #   fields: tick_rate_hz(B) cpu_temp_c(B) bram_write_us(H)
                             #           worst_overrun_us(h — signed) pad(2x)
PKT_RTT_PING     = 0x0080   # node  → server: direct UDP RTT probe for report benchmarks
PKT_RTT_PONG     = 0x0081   # server → node: echo reply for the RTT probe

# ── Flags bitmask (uint8 flags field) ─────────────────────────────────────────
#
# The same byte is reused in both directions:
# - node -> server: input / intent flags
# - server -> node: authoritative state flags

FLAG_INPUT_SHOOT = 0x01   # client intent: fired this tick
FLAG_TAGGED      = 0x02   # server state: player tagged this tick
FLAG_MATCH_END   = 0x04   # server state: match is over
FLAG_GHOST       = 0x08   # server state: this player is a server-controlled ghost

CLIENT_INPUT_FLAGS = FLAG_INPUT_SHOOT
SERVER_STATE_FLAGS = FLAG_TAGGED | FLAG_MATCH_END | FLAG_GHOST

# Backward-compatible alias used by older call sites.
FLAG_SHOOTING = FLAG_INPUT_SHOOT

# ── Role selection (sent in PKT_REGISTER reserved byte) ───────────────────────
# Node declares preferred role on first contact; server assigns final role in ACK.

ROLE_ANY    = 0x00   # no preference — server assigns by join order
ROLE_RUNNER = 0x01   # player wants to be the runner
ROLE_TAGGER = 0x02   # player wants to be the tagger

# ── Game modes ────────────────────────────────────────────────────────────────
# Packed into PKT_GAME_STATE header extension so nodes know which mode is active.

GAME_MODE_CHASE      = 0x00   # runner vs tagger, win by tag count
GAME_MODE_CHASE_BITS = 0x01   # runner collects bits, tagger tries to tag before all bits gone

# ── Node movement modes ───────────────────────────────────────────────────────

NODE_PROTOCOL_VERSION = 1  # Version of the 24-byte NodePacket wire format; bump only for breaking packet-layout changes.
MAX_USERNAME_BYTES = 32

MOVEMENT_MODE_POSE = 0x00  # Node sends a raw pose update; server treats x/y/angle as the movement payload for this tick.
MOVEMENT_MODE_INTENT_ONLY = 0x01  # Node is only declaring intent/inputs; server should ignore predicted x/y/angle and advance movement itself.
MOVEMENT_MODE_INTENT_WITH_PREDICTION = 0x02  # Node sends inputs plus its locally predicted pose; server validates/corrects that prediction instead of trusting it blindly.

# ── Runtime board control modes ───────────────────────────────────────────────

NODE_CONTROL_MODE_MANUAL = 0x00
NODE_CONTROL_MODE_AUTO   = 0x01
NODE_CONTROL_MODE_REPLAY = 0x02

# ── Wire format ───────────────────────────────────────────────────────────────
#
# Node → Server (NodePacket): 24 bytes, little-endian
#
#   Offset  Size  Fmt  Field
#     0       2    H   type              packet type
#     2       2    H   seq               sender sequence number (wraps at 65535)
#     4       4    I   timestamp         milliseconds since epoch (truncated to 32-bit)
#     8       4    f   pred_x            client predicted world-space X
#    12       4    f   pred_y            client predicted world-space Y
#    16       4    f   pred_angle        client predicted view angle, radians
#    20       1    B   input_flags       node -> server input / intent bitmask
#    21       1    B   movement_mode     pose / intent_only / intent_with_prediction
#    22       1    B   protocol_version  node packet version
#    23       1    B   reserved          reserved for future use, send zero

NODE_FMT  = '<HHIfffBBBB'
NODE_SIZE = struct.calcsize(NODE_FMT)
assert NODE_SIZE == 24, f"NodePacket must be 24 bytes, got {NODE_SIZE}"

# Server → Node header (ServerPacketHeader): 8 bytes
#
#   Offset  Size  Fmt  Field
#     0       2    H   type
#     2       2    H   seq
#     4       4    I   timestamp

HEADER_FMT  = '<HHI'
HEADER_SIZE = struct.calcsize(HEADER_FMT)
assert HEADER_SIZE == 8, f"ServerPacketHeader must be 8 bytes, got {HEADER_SIZE}"

# Per-player entry (PlayerEntry): 14 bytes
#
#   Offset  Size  Fmt  Field
#     0       1    B   player_id
#     1       4    f   x
#     5       4    f   y
#     9       4    f   angle
#    13       1    B   flags
#    14       :    x   (implicit : 14 bytes total, no tail padding needed)

PLAYER_FMT  = '<BfffB'
PLAYER_SIZE = struct.calcsize(PLAYER_FMT)
assert PLAYER_SIZE == 14, f"PlayerEntry must be 14 bytes, got {PLAYER_SIZE}"

# MapHeader (follows the 8-byte ServerPacketHeader in a PKT_MAP packet): 4 bytes
#
#   Offset  Size  Fmt  Field
#     0       1    B   width       map width in tiles
#     1       1    B   height      map height in tiles
#     2       1    B   tile_scale  world units per tile (e.g. 8 → each tile = 8×8 world units)
#     3       1    x   pad         reserved, zero
#
# Followed immediately by width*height bytes of tile data (row-major, 0=empty 1=wall).
# Total PKT_MAP size: 8 + 4 + width*height bytes.
# For a 20×16 map: 8 + 4 + 320 = 332 bytes — well within UDP MTU.

MAP_HEADER_FMT  = '<BBBx'
MAP_HEADER_SIZE = struct.calcsize(MAP_HEADER_FMT)
assert MAP_HEADER_SIZE == 4, f"MapHeader must be 4 bytes, got {MAP_HEADER_SIZE}"

# PKT_GAME_STATE extension (follows player entries): 3 bytes
#
#   Offset  Size  Fmt  Field
#     0       1    B   game_mode    GAME_MODE_CHASE or GAME_MODE_CHASE_BITS
#     1       1    B   player_count N players that follow in player entries
#     2       2    H   bits_mask    bitmask of active bits (bit N=1 → bit N still on map)
#                                   only meaningful in GAME_MODE_CHASE_BITS
#
# Full PKT_GAME_STATE layout:
#   header (8) + game_mode (1) + player_count (1) + players (14*N) + bits_mask (2)

GAME_STATE_EXT_FMT  = '<BBH'   # game_mode, player_count, bits_mask
GAME_STATE_EXT_SIZE = struct.calcsize(GAME_STATE_EXT_FMT)
assert GAME_STATE_EXT_SIZE == 4, f"GameStateExt must be 4 bytes, got {GAME_STATE_EXT_SIZE}"

# BitEntry (in PKT_BITS_INIT): 6 bytes
#
#   Offset  Size  Fmt  Field
#     0       1    B   bit_id   0-based index (matches bitmask position)
#     1       4    f   x        world-space X
#     5       1    f   y        world-space Y  ← actually offset 5, size 4 → total 9? No:
#
# Corrected: bit_id (B=1) + x (f=4) + y (f=4) = 9 bytes — use explicit fmt

BIT_ENTRY_FMT  = '<Bff'
BIT_ENTRY_SIZE = struct.calcsize(BIT_ENTRY_FMT)
assert BIT_ENTRY_SIZE == 9, f"BitEntry must be 9 bytes, got {BIT_ENTRY_SIZE}"

NODE_MODE_FMT = '<B'
NODE_MODE_SIZE = struct.calcsize(NODE_MODE_FMT)
assert NODE_MODE_SIZE == 1, f"NodeMode payload must be 1 byte, got {NODE_MODE_SIZE}"

# ── Pack helpers (build outgoing packets) ─────────────────────────────────────

# Build PKT_BITS_INIT: header + count byte + N × BitEntry (9 bytes each)
# Sent once at match start so nodes know where bits are; only bitmask is sent per-tick after.
def pack_bits_init_packet(seq, bits):
    timestamp = int(time.time() * 1000) & 0xFFFFFFFF
    header    = struct.pack(HEADER_FMT, PKT_BITS_INIT, seq & 0xFFFF, timestamp)
    payload   = struct.pack('<B', len(bits) & 0xFF)
    for i, (x, y) in enumerate(bits):
        payload += struct.pack(BIT_ENTRY_FMT, i, float(x), float(y))
    return header + payload

# Unpack PKT_BITS_INIT — returns list of (bit_id, x, y) tuples
def unpack_bits_init_packet(data):
    if len(data) < HEADER_SIZE + 1:
        raise ValueError(f"PKT_BITS_INIT too short: {len(data)} bytes")
    count = struct.unpack_from('<B', data, HEADER_SIZE)[0]
    bits  = []
    offset = HEADER_SIZE + 1
    for _ in range(count):
        bit_id, x, y = struct.unpack_from(BIT_ENTRY_FMT, data, offset)
        bits.append((bit_id, x, y))
        offset += BIT_ENTRY_SIZE
    return bits


# Build PKT_NODE_MODE: header + one byte mode payload for runtime board control.
def pack_node_mode_packet(seq, mode):
    timestamp = int(time.time() * 1000) & 0xFFFFFFFF
    header = struct.pack(HEADER_FMT, PKT_NODE_MODE, seq & 0xFFFF, timestamp)
    return header + struct.pack(NODE_MODE_FMT, mode & 0xFF)


# Unpack PKT_NODE_MODE — returns NODE_CONTROL_MODE_MANUAL/AUTO/REPLAY.
def unpack_node_mode_packet(data):
    if len(data) < HEADER_SIZE + NODE_MODE_SIZE:
        raise ValueError(f"PKT_NODE_MODE too short: {len(data)} bytes")
    return struct.unpack_from(NODE_MODE_FMT, data, HEADER_SIZE)[0]

# Build PKT_MAP: 8-byte header + 4-byte MapHeader + tiles (0=empty, 1=wall)
def pack_map_packet(seq, width, height, tile_scale, tiles):
    timestamp = int(time.time() * 1000) & 0xFFFFFFFF
    header    = struct.pack(HEADER_FMT, PKT_MAP, seq & 0xFFFF, timestamp)
    map_hdr   = struct.pack(MAP_HEADER_FMT, width & 0xFF, height & 0xFF, tile_scale & 0xFF)
    return header + map_hdr + bytes(tiles)


# Build PKT_GAME_STATE: header + ext + N player entries.
def pack_game_state_packet(seq, game_mode, players, bits_mask, *, timestamp=None):
    if timestamp is None:
        timestamp = int(time.time() * 1000) & 0xFFFFFFFF
    header = struct.pack(HEADER_FMT, PKT_GAME_STATE, seq & 0xFFFF, int(timestamp) & 0xFFFFFFFF)
    ext = struct.pack(
        GAME_STATE_EXT_FMT,
        int(game_mode) & 0xFF,
        len(players) & 0xFF,
        int(bits_mask) & 0xFFFF,
    )
    entries = b"".join(
        struct.pack(
            PLAYER_FMT,
            int(player["player_id"]) & 0xFF,
            float(player["x"]),
            float(player["y"]),
            float(player["angle"]),
            int(player["flags"]) & 0xFF,
        )
        for player in players
    )
    return header + ext + entries

# Pack a 24-byte NodePacket for sending to the server
def pack_node_packet(pkt_type, seq, x, y, angle, flags=0,
                     movement_mode=MOVEMENT_MODE_INTENT_WITH_PREDICTION,
                     protocol_version=NODE_PROTOCOL_VERSION,
                     reserved=0):
    timestamp = int(time.time() * 1000) & 0xFFFFFFFF
    return struct.pack(NODE_FMT, pkt_type, seq & 0xFFFF, timestamp,
                       float(x), float(y), float(angle),
                       flags & 0xFF, movement_mode & 0xFF,
                       protocol_version & 0xFF, reserved & 0xFF)


def pack_register_packet(seq, x, y, angle, *,
                         preferred_role=ROLE_ANY,
                         username="",
                         flags=0,
                         movement_mode=MOVEMENT_MODE_INTENT_WITH_PREDICTION,
                         protocol_version=NODE_PROTOCOL_VERSION):
    username_bytes = (username or "").encode("utf-8")[:MAX_USERNAME_BYTES]
    base = pack_node_packet(
        PKT_REGISTER,
        seq,
        x,
        y,
        angle,
        flags=flags,
        movement_mode=movement_mode,
        protocol_version=protocol_version,
        reserved=preferred_role,
    )
    return base + struct.pack("<B", len(username_bytes)) + username_bytes


def client_input_flags(*, shooting=False):
    flags = 0
    if shooting:
        flags |= FLAG_INPUT_SHOOT
    return flags


def decode_flag_names(flags, *, direction):
    names = []
    if direction == "client_to_server":
        if flags & FLAG_INPUT_SHOOT:
            names.append("shoot")
    elif direction == "server_to_client":
        if flags & FLAG_TAGGED:
            names.append("tagged")
        if flags & FLAG_MATCH_END:
            names.append("match_end")
    else:
        raise ValueError(f"unknown direction: {direction}")
    return names


def decode_movement_mode(mode):
    return {
        MOVEMENT_MODE_POSE: "pose",
        MOVEMENT_MODE_INTENT_ONLY: "intent_only",
        MOVEMENT_MODE_INTENT_WITH_PREDICTION: "intent_with_prediction",
    }.get(mode, f"unknown({mode})")


def decode_node_control_mode(mode):
    return {
        NODE_CONTROL_MODE_MANUAL: "manual",
        NODE_CONTROL_MODE_AUTO: "auto",
        NODE_CONTROL_MODE_REPLAY: "replay",
    }.get(mode, f"unknown({mode})")


# ── Unpack helpers (decode incoming packets) ──────────────────────────────────

# Unpack a raw NodePacket into a dict of named fields
def unpack_node_packet(data):
    if len(data) < NODE_SIZE:
        raise ValueError(f"Packet too short for node packet: {len(data)} bytes")
    pkt_type, seq, timestamp, x, y, angle, input_flags, movement_mode, protocol_version, reserved = (
        struct.unpack(NODE_FMT, data[:NODE_SIZE])
    )
    return {
        "pkt_type": pkt_type,
        "seq": seq,
        "timestamp": timestamp,
        "x": x,
        "y": y,
        "angle": angle,
        "input_flags": input_flags,
        "movement_mode": movement_mode,
        "protocol_version": protocol_version,
        "reserved": reserved,
    }


def unpack_register_packet(data):
    pkt = unpack_node_packet(data)
    if pkt["pkt_type"] != PKT_REGISTER:
        raise ValueError(f"expected PKT_REGISTER, got 0x{pkt['pkt_type']:04x}")

    username = ""
    if len(data) > NODE_SIZE:
        username_len = data[NODE_SIZE]
        available = len(data) - (NODE_SIZE + 1)
        if username_len > available:
            raise ValueError(
                f"PKT_REGISTER username truncated: wanted {username_len} bytes, only {available} available"
            )
        username_bytes = data[NODE_SIZE + 1 : NODE_SIZE + 1 + username_len]
        username = username_bytes.decode("utf-8", errors="replace").strip()

    pkt["preferred_role"] = pkt["reserved"]
    pkt["username"] = username
    return pkt

# Unpack the 8-byte server header — returns (pkt_type, seq, timestamp)
def unpack_header(data):
    if len(data) < HEADER_SIZE:
        raise ValueError(f"Packet too short for header: {len(data)} bytes")
    return struct.unpack(HEADER_FMT, data[:HEADER_SIZE])

# Unpack all PlayerEntry records from the post-header payload of a GAME_STATE packet
def unpack_player_entries(payload):
    players = []
    n = len(payload) // PLAYER_SIZE
    for i in range(n):
        chunk = payload[i * PLAYER_SIZE : (i + 1) * PLAYER_SIZE]
        player_id, x, y, angle, flags = struct.unpack(PLAYER_FMT, chunk)
        players.append({
            "player_id": player_id,
            "x":         x,
            "y":         y,
            "angle":     angle,
            "flags":     flags,
        })
    return players

# Unpack a PKT_MAP packet — returns (width, height, tile_scale, tiles bytearray)
def unpack_map_packet(data):
    if len(data) < HEADER_SIZE + MAP_HEADER_SIZE:
        raise ValueError(f"PKT_MAP too short: {len(data)} bytes")
    width, height, tile_scale = struct.unpack_from(MAP_HEADER_FMT, data, HEADER_SIZE)
    tile_start = HEADER_SIZE + MAP_HEADER_SIZE
    tiles = bytearray(data[tile_start : tile_start + width * height])
    return width, height, tile_scale, tiles

# Unpack extended PKT_GAME_STATE — returns (pkt_type, seq, timestamp, game_mode, players, bits_mask)
# Falls back gracefully to legacy format (no extension bytes) for older servers.
def unpack_server_packet(data):
    pkt_type, seq, timestamp = unpack_header(data)
    payload = data[HEADER_SIZE:]
    # New format: game_mode (1) + player_count (1) + players + bits_mask (2)
    if pkt_type == PKT_GAME_STATE and len(payload) >= GAME_STATE_EXT_SIZE:
        game_mode, player_count, bits_mask = struct.unpack_from(GAME_STATE_EXT_FMT, payload, 0)
        players = unpack_player_entries(payload[GAME_STATE_EXT_SIZE : GAME_STATE_EXT_SIZE + player_count * PLAYER_SIZE])
        return pkt_type, seq, timestamp, game_mode, players, bits_mask
    # Legacy / non-game-state packets — no extension
    players = unpack_player_entries(payload)
    return pkt_type, seq, timestamp, GAME_MODE_CHASE, players, 0xFFFF


def pack_rtt_ping_packet(seq, *, timestamp=None):
    if timestamp is None:
        timestamp = int(time.time() * 1000) & 0xFFFFFFFF
    return struct.pack(HEADER_FMT, PKT_RTT_PING, seq & 0xFFFF, int(timestamp) & 0xFFFFFFFF)


def pack_rtt_pong_packet(seq, *, timestamp=None):
    if timestamp is None:
        timestamp = int(time.time() * 1000) & 0xFFFFFFFF
    return struct.pack(HEADER_FMT, PKT_RTT_PONG, seq & 0xFFFF, int(timestamp) & 0xFFFFFFFF)


def unpack_rtt_packet(data):
    pkt_type, seq, timestamp = unpack_header(data)
    if pkt_type not in {PKT_RTT_PING, PKT_RTT_PONG}:
        raise ValueError(f"expected RTT packet, got 0x{pkt_type:04x}")
    return {
        "pkt_type": pkt_type,
        "seq": seq,
        "timestamp": timestamp,
    }


# ── PKT_PERF pack / unpack ─────────────────────────────────────────────────────
# PerfPayload (8 bytes, follows the 8-byte header):
#   B  tick_rate_hz      — achieved tick rate over the last reporting window
#   B  cpu_temp_c        — CPU temperature in °C (0 if unavailable)
#   H  bram_write_us     — last BRAM write duration in microseconds
#   h  worst_overrun_us  — worst tick overrun (signed; negative = on time, positive = late)
#   xx pad               — 2 reserved bytes, zero

PERF_FMT  = '<BBHhxx'
PERF_SIZE = struct.calcsize(PERF_FMT)
assert PERF_SIZE == 8, f"PerfPayload must be 8 bytes, got {PERF_SIZE}"


def pack_perf_packet(seq, *, tick_rate_hz, cpu_temp_c, bram_write_us, worst_overrun_us):
    timestamp = int(time.time() * 1000) & 0xFFFFFFFF
    header = struct.pack(HEADER_FMT, PKT_PERF, seq & 0xFFFF, timestamp)
    payload = struct.pack(
        PERF_FMT,
        min(255, max(0, int(tick_rate_hz))),
        min(255, max(0, int(cpu_temp_c))),
        min(65535, max(0, int(bram_write_us))),
        max(-32768, min(32767, int(worst_overrun_us))),
    )
    return header + payload


def unpack_perf_packet(data):
    if len(data) < HEADER_SIZE + PERF_SIZE:
        raise ValueError(f"PKT_PERF too short: {len(data)} bytes")
    pkt_type, seq, timestamp = struct.unpack_from(HEADER_FMT, data, 0)
    tick_rate_hz, cpu_temp_c, bram_write_us, worst_overrun_us = struct.unpack_from(PERF_FMT, data, HEADER_SIZE)
    return {
        "pkt_type": pkt_type,
        "seq": seq,
        "timestamp": timestamp,
        "tick_rate_hz": tick_rate_hz,
        "cpu_temp_c": cpu_temp_c,
        "bram_write_us": bram_write_us,
        "worst_overrun_us": worst_overrun_us,
    }
