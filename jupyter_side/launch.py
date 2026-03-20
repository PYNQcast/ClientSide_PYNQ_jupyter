#!/usr/bin/env python3
"""launch.py — PYNQcast TUI launcher for script.py

Drop alongside run_pynq.py on the PYNQ board. Run:
    python3 launch.py

Features:
  - Branded splash screen
  - Saves last config to ~/.pynqcast.json — press Enter to quick-launch
  - Interactive prompts: username, role, mode, server
  - Zero dependencies beyond Python 3 stdlib
  - Launches script.py with assembled CLI args

Copy to board:
  scp jupyter_side/launch.py xilinx@<PYNQ_IP>:/home/xilinx/jupyter_notebooks/Final_project_test/
"""

import json
import os
import subprocess
import sys
import time

# ── paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
CLIENT_SCRIPT = os.path.join(SCRIPT_DIR, "run_pynq.py")
CONFIG_PATH   = os.path.expanduser("~/.pynqcast.json")

# ── defaults ─────────────────────────────────────────────────────────────────
DEFAULTS = {
    "username": "",
    "role":     "any",
    "mode":     "manual",
    "server":   "3.9.71.204",
    "port":     9000,
    "no_hw":    False,
}

ROLES = ["any", "runner", "tagger"]
MODES = ["manual", "auto", "replay"]

# ── ANSI helpers ─────────────────────────────────────────────────────────────
R   = "\033[0m"       # reset
RED = "\033[38;5;196m"
GRY = "\033[38;5;245m"
WHT = "\033[1;37m"
GRN = "\033[38;5;46m"
YEL = "\033[38;5;220m"
CYN = "\033[38;5;51m"
BLD = "\033[1m"
DIM = "\033[2m"

def clear():
    os.system("clear" if os.name != "nt" else "cls")


# ── config persistence ───────────────────────────────────────────────────────

def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for k in DEFAULTS:
                if k in saved:
                    cfg[k] = saved[k]
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        pass
    return cfg


def save_config(cfg: dict):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except (PermissionError, OSError):
        pass


# ── display helpers ──────────────────────────────────────────────────────────

LOGO = rf"""
{RED}    ██████╗ ██╗   ██╗███╗   ██╗ ██████╗  {WHT}██████╗ ██████╗ ███████╗████████╗
{RED}    ██╔══██╗╚██╗ ██╔╝████╗  ██║██╔═══██╗ {WHT}██╔════╝██╔══██╗██╔════╝╚══██╔══╝
{RED}    ██████╔╝ ╚████╔╝ ██╔██╗ ██║██║   ██║ {WHT}██║     ███████║███████╗   ██║
{RED}    ██╔═══╝   ╚██╔╝  ██║╚██╗██║██║▄▄ ██║ {WHT}██║     ██╔══██║╚════██║   ██║
{RED}    ██║        ██║   ██║ ╚████║╚██████╔╝ {WHT}╚██████╗██║  ██║███████║   ██║
{RED}    ╚═╝        ╚═╝   ╚═╝  ╚═══╝ ╚══▀▀═╝  {WHT}╚═════╝╚═╝  ╚═╝╚══════╝   ╚═╝{R}
"""

def show_splash(cfg: dict):
    clear()
    print(LOGO)
    print(f"    {GRY}FPGA Raycaster  •  Multiplayer Tag  •  Imperial College London{R}")
    print()

    has_saved = os.path.exists(CONFIG_PATH)
    if has_saved and cfg.get("username"):
        print(f"    {DIM}Last session:{R}  {CYN}{cfg['username']}{R}  "
              f"{GRY}│{R}  {cfg['role']}  {GRY}│{R}  {cfg['mode']}  "
              f"{GRY}│{R}  {cfg['server']}:{cfg['port']}")
        print()
        print(f"    {GRN}▸ Press Enter{R} to quick-launch with these settings")
        print(f"    {YEL}▸ Press  c   {R} to configure a new session")
        print(f"    {GRY}▸ Press  q   {R} to quit")
    else:
        print(f"    {YEL}▸ Press Enter{R} to set up your first session")
        print(f"    {GRY}▸ Press  q   {R} to quit")
    print()


def show_summary(cfg: dict):
    print()
    print(f"  {BLD}┌─────────────────────────────────────┐{R}")
    print(f"  {BLD}│{R}  {WHT}PYNQcast Session{R}                    {BLD}│{R}")
    print(f"  {BLD}├─────────────────────────────────────┤{R}")
    print(f"  {BLD}│{R}  Player    {CYN}{cfg['username'] or '<anon>':>24s}{R}  {BLD}│{R}")
    print(f"  {BLD}│{R}  Role      {YEL}{cfg['role']:>24s}{R}  {BLD}│{R}")
    print(f"  {BLD}│{R}  Mode      {GRN}{cfg['mode']:>24s}{R}  {BLD}│{R}")
    print(f"  {BLD}│{R}  Server    {GRY}{cfg['server'] + ':' + str(cfg['port']):>24s}{R}  {BLD}│{R}")
    hw = "disabled" if cfg.get("no_hw") else "enabled"
    print(f"  {BLD}│{R}  Hardware  {GRY}{hw:>24s}{R}  {BLD}│{R}")
    print(f"  {BLD}└─────────────────────────────────────┘{R}")
    print()


# ── input helpers ────────────────────────────────────────────────────────────

def prompt(label: str, default: str = "", choices: list | None = None) -> str:
    """Prompt with an optional default and optional numbered choices."""
    if choices:
        print(f"  {WHT}{label}{R}")
        for i, c in enumerate(choices, 1):
            marker = f"{GRN}▸{R}" if c == default else " "
            tag = f" {DIM}(current){R}" if c == default else ""
            print(f"    {marker} {YEL}{i}{R}  {c}{tag}")
        while True:
            raw = input(f"  {GRY}[1-{len(choices)}, Enter={default}]:{R} ").strip()
            if not raw:
                return default
            if raw.isdigit() and 1 <= int(raw) <= len(choices):
                return choices[int(raw) - 1]
            print(f"  {RED}Pick 1-{len(choices)}{R}")
    else:
        hint = f" {DIM}[Enter={default}]{R}" if default else ""
        raw = input(f"  {WHT}{label}{hint}{GRY}:{R} ").strip()
        return raw if raw else default


def prompt_yn(label: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    raw = input(f"  {WHT}{label} {GRY}[{hint}]:{R} ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


# ── interactive config ───────────────────────────────────────────────────────

def configure(cfg: dict) -> dict:
    print()
    print(f"  {BLD}── Session Setup ──{R}")
    print()

    cfg["username"] = prompt("Username", cfg.get("username", ""))
    print()
    cfg["role"] = prompt("Role", cfg.get("role", "any"), ROLES)
    print()
    cfg["mode"] = prompt("Mode", cfg.get("mode", "manual"), MODES)
    print()

    # Only ask about --no-hw if pynq package is missing (likely running on a PC)
    try:
        import pynq  # noqa: F401
    except ImportError:
        cfg["no_hw"] = prompt_yn("No hardware (--no-hw)?", cfg.get("no_hw", True))
        print()

    return cfg


# ── launch ───────────────────────────────────────────────────────────────────

def build_cmd(cfg: dict) -> list[str]:
    cmd = [sys.executable, CLIENT_SCRIPT]
    if cfg.get("username"):
        cmd += ["--username", cfg["username"]]
    cmd += ["--role", cfg.get("role", "any")]
    cmd += ["--mode", cfg.get("mode", "manual")]
    cmd += ["--server", cfg.get("server", DEFAULTS["server"])]
    cmd += ["--port", str(cfg.get("port", DEFAULTS["port"]))]
    if cfg.get("no_hw"):
        cmd.append("--no-hw")
    return cmd


def launch(cfg: dict):
    show_summary(cfg)

    cmd = build_cmd(cfg)
    print(f"  {GRY}${R} {' '.join(cmd)}")
    print()

    # Countdown — gives time to abort with Ctrl-C
    for i in range(3, 0, -1):
        print(f"\r  {YEL}Launching in {i}...{R}  ", end="", flush=True)
        time.sleep(0.6)
    print(f"\r  {GRN}Launching!       {R}")
    print()

    save_config(cfg)

    try:
        proc = subprocess.run(cmd, cwd=SCRIPT_DIR)
        sys.exit(proc.returncode)
    except KeyboardInterrupt:
        print(f"\n  {GRY}Session ended.{R}")
        sys.exit(0)


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    if not os.path.isfile(CLIENT_SCRIPT):
        print(f"{RED}Error:{R} cannot find {CLIENT_SCRIPT}")
        print(f"Place launch.py next to script.py")
        sys.exit(1)

    cfg = load_config()
    show_splash(cfg)

    has_saved = os.path.exists(CONFIG_PATH) and cfg.get("username")

    try:
        choice = input(f"    {GRY}>{R} ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(0)

    if choice == "q":
        sys.exit(0)
    elif choice in ("c", "config", "new"):
        cfg = configure(cfg)
        launch(cfg)
    elif choice == "" and has_saved:
        # Quick-launch with saved config
        launch(cfg)
    else:
        # First time or explicit Enter without saved config
        cfg = configure(cfg)
        launch(cfg)


if __name__ == "__main__":
    main()
