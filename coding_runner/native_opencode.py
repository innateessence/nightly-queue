"""Compatibility import for vibearr_runner.native_opencode."""

import importlib
import sys

from vibearr_runner.native_opencode import *  # noqa: F403

sys.modules[__name__] = importlib.import_module("vibearr_runner.native_opencode")
