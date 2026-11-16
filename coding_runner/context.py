"""Compatibility import for vibearr_runner.context."""

import importlib
import sys

from vibearr_runner.context import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.context")

sys.modules[__name__] = _module
