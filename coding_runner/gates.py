"""Compatibility import for vibearr_runner.gates."""

import importlib
import sys

from vibearr_runner.gates import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.gates")

sys.modules[__name__] = _module
