"""Compatibility import for vibearr_runner.native_claude."""

import importlib
import sys

from vibearr_runner.native_claude import *  # noqa: F403

sys.modules[__name__] = importlib.import_module("vibearr_runner.native_claude")
