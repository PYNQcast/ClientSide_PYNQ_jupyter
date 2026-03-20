"""Load the RTT test protocol from this folder explicitly.

This avoids accidentally importing an older top-level protocol.py elsewhere on
the board that does not include the RTT packet helpers.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


_PROTOCOL_PATH = Path(__file__).with_name("protocol.py")
_SPEC = importlib.util.spec_from_file_location("pynq_client_tests_local_protocol", _PROTOCOL_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Could not load local protocol module from {_PROTOCOL_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

globals().update(_MODULE.__dict__)
