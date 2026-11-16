"""Compatibility import for vibearr_runner.implementation."""

import importlib
import sys

from vibearr_runner.implementation import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.implementation")

sys.modules[__name__] = _module
