"""Compatibility import for vibearr_runner.maintenance."""

import importlib
import sys

from vibearr_runner.maintenance import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.maintenance")

sys.modules[__name__] = _module
