"""Compatibility import for vibearr_runner.run_logs."""

import importlib
import sys

from vibearr_runner.run_logs import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.run_logs")

sys.modules[__name__] = _module
