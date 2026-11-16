"""Compatibility import for vibearr_runner.plan_store."""

import importlib
import sys

from vibearr_runner.plan_store import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.plan_store")

sys.modules[__name__] = _module
