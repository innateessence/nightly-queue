"""Compatibility import for vibearr_runner.controller."""

import importlib
import sys

from vibearr_runner.controller import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.controller")

sys.modules[__name__] = _module
