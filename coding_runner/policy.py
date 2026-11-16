"""Compatibility import for vibearr_runner.policy."""

import importlib
import sys

from vibearr_runner.policy import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.policy")

sys.modules[__name__] = _module
