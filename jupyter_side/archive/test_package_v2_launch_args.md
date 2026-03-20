# `test_package_v2.py` / `test_package_v3.py` Launch Arguments

This file documents the current runtime launch surface for:

- [test_package_v2.py](/home/akendall/Documents/ServerSide_PYNQ_Raycaster/jupyter_side/test_package_v2.py)
- [test_package_v3.py](/home/akendall/Documents/ServerSide_PYNQ_Raycaster/jupyter_side/test_package_v3.py)

`test_package_v3.py` is the current stabilized copy of the PYNQ board client for `pynq_full`.
`test_package_v2.py` exposes the same runtime flags and remains compatible with the same launch surface.

Important runtime note:

- `test_package_v2.py` keeps the older `20 Hz` board loop baseline
- `test_package_v3.py` supports `--tick-rate`, and now defaults to `50 Hz`
- if you pass explicit movement values, those are treated as raw per-tick values
- both clients now blend small server pose corrections instead of immediately snapping; large corrections such as respawns still snap

## Basic usage

On the board:

```bash
cd /home/xilinx/jupyter_notebooks
python3 test_package_v3.py
```

Important:

- `protocol.py` or `as` `protocol_1` must be in the same folder as the script you run
- this script is meant to run on the PYNQ board with the `pynq` package installed

## Current CLI arguments

### `--server`

- Type: `str`
- Default: `3.9.71.204`
- Purpose: EC2/server IP or hostname

Example:

```bash
python3 test_package_v2.py --server 3.9.71.204
```

### `--port`

- Type: `int`
- Default: `9000`
- Purpose: UDP game server port

Example:

```bash
python3 test_package_v2.py --port 9000
```

### `--overlay`

- Type: `str`
- Default: `/home/xilinx/jupyter_notebooks/Final_project_test/design_1_wrapper.bit`
- Purpose: bitstream path loaded on the board

Example:

```bash
python3 test_package_v2.py --overlay /home/xilinx/jupyter_notebooks/my_overlay/design_1_wrapper.bit
```

### `--username`

- Type: `str`
- Default: empty unless `PYNQ_USERNAME` is set
- Purpose: profile/display identity sent to the server

Example:

```bash
python3 test_package_v2.py --username demo-board-1
```

### `--mode`

- Type: `manual|auto`
- Default: `manual` unless `PYNQ_MODE` is set
- Purpose:
  - `manual`: board buttons drive movement
  - `auto`: board runs built-in runner/tagger AI

Example:

```bash
python3 test_package_v2.py --mode manual
python3 test_package_v2.py --mode auto
```

### `--tick-rate`

- Type: `int`
- Default:
  - `test_package_v2.py`: `20`
  - `test_package_v3.py`: `50`
- Purpose: board-side control/send loop rate in Hz

For `test_package_v3.py`, the built-in default move/turn/auto speeds are automatically rescaled from the old `20 Hz` baseline so higher tick rates improve smoothness without automatically tripling world speed.

Example:

```bash
python3 test_package_v3.py --tick-rate 50
python3 test_package_v3.py --tick-rate 30
```

### `--move-speed`

- Type: `float`
- Default: `0.2`
- Purpose: manual forward/backward movement step per tick

Example:

```bash
python3 test_package_v2.py --mode manual --move-speed 0.2
```

### `--turn-step`

- Type: `int`
- Default: `64`
- Purpose: manual turn increment in 12-bit hardware angle units

Example:

```bash
python3 test_package_v2.py --turn-step 64
```

### `--auto-runner-speed`

- Type: `float`
- Default: `0.2`
- Purpose: auto-mode movement speed when the board is acting as runner

Example:

```bash
python3 test_package_v2.py --mode auto --auto-runner-speed 0.2
```

### `--auto-tagger-speed`

- Type: `float`
- Default: `0.26`
- Purpose: auto-mode movement speed when the board is acting as tagger

Example:

```bash
python3 test_package_v2.py --mode auto --auto-tagger-speed 0.26
```

### `--auto-fallback-speed`

- Type: `float`
- Default: `0.18`
- Purpose: auto-mode fallback roam speed when there is no strong target

Example:

```bash
python3 test_package_v2.py --mode auto --auto-fallback-speed 0.18
```

### `--auto-shoot-period`

- Type: `int`
- Default:
  - `test_package_v2.py`: `4`
  - `test_package_v3.py`: auto-scaled from the `20 Hz` baseline
- Purpose: auto tagger shot cadence in ticks

Example:

```bash
python3 test_package_v3.py --mode auto --tick-rate 50 --auto-shoot-period 10
```

## Environment variable equivalents

Every runtime tuning flag also has an env form:

- `PYNQ_TICK_RATE`
- `PYNQ_USERNAME`
- `PYNQ_MODE`
- `PYNQ_MOVE_SPEED`
- `PYNQ_TURN_STEP`
- `PYNQ_AUTO_RUNNER_SPEED`
- `PYNQ_AUTO_TAGGER_SPEED`
- `PYNQ_AUTO_FALLBACK_SPEED`
- `PYNQ_AUTO_SHOOT_PERIOD`

Example:

```bash
PYNQ_MODE=auto PYNQ_USERNAME=demo-board-2 python3 test_package_v3.py
```

## Common launch recipes

### Manual board with explicit known-good baseline

```bash
python3 test_package_v3.py \
  --mode manual \
  --tick-rate 50 \
  --username board-1
```

That keeps the old `0.2 / 64` feel approximately, because `v3` rescales its defaults automatically when you do choose a different tick rate.

### Auto board for one-human demo

```bash
python3 test_package_v3.py \
  --mode auto \
  --tick-rate 50 \
  --username board-2
```

### Custom overlay path

```bash
python3 test_package_v2.py \
  --overlay /home/xilinx/jupyter_notebooks/Final_project_test/design_1_wrapper.bit
```

## Bash examples for changing settings

### One-off change on the command line

This changes the value for just that launch:

```bash
python3 test_package_v3.py --mode manual --tick-rate 50 --move-speed 0.08
```

Another example:

```bash
python3 test_package_v3.py --mode auto --tick-rate 50 --auto-tagger-speed 0.10
```

### Export environment variables in Bash

This is useful if you want to keep the command shorter:

```bash
export PYNQ_TICK_RATE=50
export PYNQ_MODE=manual
unset PYNQ_MOVE_SPEED
unset PYNQ_TURN_STEP
unset PYNQ_AUTO_RUNNER_SPEED
unset PYNQ_AUTO_TAGGER_SPEED
unset PYNQ_AUTO_FALLBACK_SPEED
unset PYNQ_AUTO_SHOOT_PERIOD
python3 test_package_v3.py
```

Auto example:

```bash
export PYNQ_TICK_RATE=50
export PYNQ_MODE=auto
unset PYNQ_AUTO_RUNNER_SPEED
unset PYNQ_AUTO_TAGGER_SPEED
unset PYNQ_AUTO_FALLBACK_SPEED
unset PYNQ_AUTO_SHOOT_PERIOD
python3 test_package_v3.py
```

### Clear exported values

If you exported a value and want to go back to script defaults:

```bash
unset PYNQ_MODE
unset PYNQ_MOVE_SPEED
unset PYNQ_TURN_STEP
unset PYNQ_AUTO_RUNNER_SPEED
unset PYNQ_AUTO_TAGGER_SPEED
unset PYNQ_AUTO_FALLBACK_SPEED
unset PYNQ_AUTO_SHOOT_PERIOD
unset PYNQ_TICK_RATE
```

### Relaunch with new values

If the script is already running, stop it and start it again with the new args:

```bash
pkill -f 'test_package_v3.py'
cd /home/xilinx/jupyter_notebooks
python3 test_package_v3.py --mode manual --tick-rate 50
```

If you are running it inside a `tmux` pane, the usual flow is:

```bash
Ctrl+C
python3 test_package_v3.py --mode auto --tick-rate 50
```

## Runtime vs startup control

What can be changed from the PYNQ monitor at runtime:

- `manual` / `auto` mode per board

What is currently startup-only:

- `move-speed`
- `turn-step`
- `auto-runner-speed`
- `auto-tagger-speed`
- `auto-fallback-speed`
- `auto-shoot-period`
- `server`
- `port`
- `overlay`
- `username`

So if you want to tune movement or AI speeds, restart the board client with new launch args.

## Notes

- `test_package_v1.py` is the frozen legacy version
- `test_package_v3.py` is now the smoother high-rate runtime path for `pynq_full`
- `test_package_v2.py` keeps the older 20 Hz behavior
- if board movement looks dead, first confirm the board is actually running the version and args you think it is
