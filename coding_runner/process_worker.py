"""Compatibility import for vibearr_runner.process_worker."""

import importlib
import sys

from vibearr_runner.process_worker import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.process_worker")

if __name__ == "__main__":
    raise SystemExit(_module.main())

sys.modules[__name__] = _module
