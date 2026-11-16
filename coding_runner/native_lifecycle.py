"""Compatibility import for vibearr_runner.native_lifecycle."""

import importlib
import sys

from vibearr_runner.native_lifecycle import *  # noqa: F403

sys.modules[__name__] = importlib.import_module("vibearr_runner.native_lifecycle")
