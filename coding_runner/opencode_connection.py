"""Compatibility import for vibearr_runner.opencode_connection."""

import importlib
import sys

from vibearr_runner.opencode_connection import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.opencode_connection")

sys.modules[__name__] = _module
