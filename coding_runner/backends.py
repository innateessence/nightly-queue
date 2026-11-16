"""Compatibility import for vibearr_runner.backends."""

import importlib
import sys

from vibearr_runner.backends import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.backends")

sys.modules[__name__] = _module
