"""Compatibility import for vibearr_runner.journal."""

import importlib
import sys

from vibearr_runner.journal import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.journal")

sys.modules[__name__] = _module
