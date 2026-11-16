"""Compatibility import for vibearr_runner.opencode_backend."""

import importlib
import sys

from vibearr_runner.opencode_backend import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.opencode_backend")

sys.modules[__name__] = _module
