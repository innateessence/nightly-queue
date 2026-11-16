"""Compatibility import for vibearr_runner.recovery."""

import importlib
import sys

from vibearr_runner.recovery import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.recovery")

sys.modules[__name__] = _module
