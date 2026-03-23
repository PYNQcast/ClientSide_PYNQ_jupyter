"""Hardware stubs, overlay loading, fixed-point helpers, and BRAM write routines."""

import math
import time

try:
    from pynq import Overlay
    from pynq.ps import Clocks
except ImportError:
    Overlay = None
    Clocks = None

import protocol

# ── BRAM memory map (mirrors run_pynq.py section 1) ──────────────────────────
MAP_ROWS = MAP_COLS = 32
PLAYER_POS_OFFSET     = 32 * 4
PLAYER_ANGLE_OFFSET   = 33 * 4
ENTITY_BASE_OFFSET    = 34 * 4
ENTITY_STRIDE         = 2
MAX_ENTITIES          = 4
BITS_COUNT_OFFSET     = (34 + ENTITY_STRIDE * MAX_ENTITIES) * 4
BITS_MASK_OFFSET      = BITS_COUNT_OFFSET + 4
BITS_BASE_OFFSET      = BITS_MASK_OFFSET  + 4
MAX_BITS              = 16
COORD_FRAC_BITS       = 10
ANGLE_STEPS           = 1 << 12
ANGLE_MASK            = ANGLE_STEPS - 1
CLOCK_MHZ             = 50.0


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

# Write remote entities (including ghosts) and bit markers to BRAM sprite/bits region.
def _write_sprites(bram, state):
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

# ── fallback map ──────────────────────────────────────────────────────────────
def _fallback_map():
    tiles = bytearray(MAP_ROWS * MAP_COLS)
    for c in range(MAP_COLS):
        tiles[c] = tiles[(MAP_ROWS-1)*MAP_COLS+c] = 1
    for r in range(MAP_ROWS):
        tiles[r*MAP_COLS] = tiles[r*MAP_COLS+MAP_COLS-1] = 1
    return tiles
