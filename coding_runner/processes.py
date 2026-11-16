"""Compatibility import for vibearr_runner.processes."""

import importlib
import sys

from vibearr_runner.processes import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.processes")

sys.modules[__name__] = _module
