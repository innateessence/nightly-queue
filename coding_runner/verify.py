"""Compatibility import for vibearr_runner.verify."""

import importlib
import sys

from vibearr_runner.verify import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.verify")

if __name__ == "__main__":
    raise SystemExit(_module.main())

sys.modules[__name__] = _module
