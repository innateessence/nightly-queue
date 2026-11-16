"""Compatibility import for vibearr_runner.lifecycle."""

import importlib
import sys

from vibearr_runner.lifecycle import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.lifecycle")

sys.modules[__name__] = _module
