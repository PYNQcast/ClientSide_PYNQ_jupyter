"""Objective selection, evasion logic, and active-bit targeting for auto mode."""

import math

import protocol
from movement import _path_step_target

# ── auto steering config (mirrors run_pynq.py section 1) ─────────────────────
AUTO_RUNNER_EVADE_DISTANCE = 42.0


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
