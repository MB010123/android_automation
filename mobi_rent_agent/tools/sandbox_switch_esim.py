"""Shim to the archived Pixel 7a sandbox switch tool."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ARCHIVE = Path(__file__).resolve().parents[2] / "pixel7a_sandbox" / "sandbox_switch_esim.py"


def main() -> int:
    spec = importlib.util.spec_from_file_location("sandbox_switch_esim_archive", _ARCHIVE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"missing archive tool {_ARCHIVE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return int(module.main())


if __name__ == "__main__":
    sys.exit(main())
