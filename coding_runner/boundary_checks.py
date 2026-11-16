"""Compatibility import for vibearr_runner.boundary_checks."""

import importlib
import sys

from vibearr_runner.boundary_checks import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.boundary_checks")

sys.modules[__name__] = _module
