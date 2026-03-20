"""Load run_pynq from a nearby file path for button-triggered RTT mode."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_run_pynq():
    errors = []
    candidates = [
        Path(__file__).with_name("run_pynq.py"),
        Path(__file__).resolve().parent.parent / "run_pynq.py",
    ]
    for path in candidates:
        if not path.exists():
            continue
        spec = importlib.util.spec_from_file_location("pynq_client_tests_local_run_pynq", path)
        if spec is None or spec.loader is None:
            continue
        module_dir = str(path.parent)
        original_path = list(sys.path)
        try:
            if module_dir not in sys.path:
                sys.path.insert(0, module_dir)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        except Exception as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
        finally:
            sys.path[:] = original_path
    if errors:
        raise ImportError("Failed to load run_pynq.py from RTT folder candidates: " + " | ".join(errors))
    raise ImportError("Could not find run_pynq.py next to the RTT tools or in the parent jupyter_side folder")
