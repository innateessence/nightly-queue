"""Compatibility import for vibearr_runner.model_worker."""

import importlib
import sys

from vibearr_runner.model_worker import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.model_worker")

if __name__ == "__main__":
    raise SystemExit(_module.main())

sys.modules[__name__] = _module
