"""Compatibility import for vibearr_runner.research."""

import importlib
import sys

from vibearr_runner.research import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.research")

sys.modules[__name__] = _module
