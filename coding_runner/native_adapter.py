"""Compatibility import for vibearr_runner.native_adapter."""

import importlib
import sys

from vibearr_runner.native_adapter import *  # noqa: F403

sys.modules[__name__] = importlib.import_module("vibearr_runner.native_adapter")
