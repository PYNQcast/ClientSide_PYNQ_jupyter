#!/usr/bin/env python3
"""Named launcher for the self-contained RTT benchmark."""

from __future__ import annotations

import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from udp_rtt import main


if __name__ == "__main__":
    raise SystemExit(main())
