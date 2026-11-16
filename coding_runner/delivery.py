"""Compatibility import for vibearr_runner.delivery."""

import importlib
import sys

from vibearr_runner.delivery import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.delivery")

sys.modules[__name__] = _module
