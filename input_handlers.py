"""Manual button input and autonomous steering logic."""

import math
import time

import protocol
from hardware import _hw_angle
from movement import _resolve_move, _path_step_target
from ai import _choose_auto_objective
from network import _wrap

# ── button constants (mirrors run_pynq.py section 1) ─────────────────────────
BTN_LEFT  = 1 << 0
BTN_BACK  = 1 << 1
BTN_FWD   = 1 << 2
BTN_RIGHT = 1 << 3

ANGLE_STEPS = 1 << 12

# ── auto steering config (mirrors run_pynq.py section 1) ─────────────────────
AUTO_TAGGER_SPEED              = 0.11
AUTO_RUNNER_SPEED              = 0.10
AUTO_FALLBACK_SPEED            = 0.09
AUTO_TAGGER_SHOOT_RANGE        = 26.0
AUTO_TAGGER_SHOOT_ARC          = 0.4
AUTO_TAGGER_SHOOT_PERIOD_TICKS = 4


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
