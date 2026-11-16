"""Compatibility import for vibearr.cli."""

import importlib
import sys

from vibearr.cli import *  # noqa: F403

_module = importlib.import_module("vibearr.cli")

if __name__ == "__main__":
    raise SystemExit(_module.main())

sys.modules[__name__] = _module
