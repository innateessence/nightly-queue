"""Compatibility import for vibearr_runner.plans."""

import importlib
import sys

from vibearr_runner.plans import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.plans")

sys.modules[__name__] = _module
