"""Compatibility import for vibearr_runner.sandbox."""

import importlib
import sys

from vibearr_runner.sandbox import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.sandbox")

sys.modules[__name__] = _module
