"""Compatibility import for vibearr_runner.budgets."""

import importlib
import sys

from vibearr_runner.budgets import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.budgets")

sys.modules[__name__] = _module
