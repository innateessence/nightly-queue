"""Compatibility import for vibearr_runner.backend_registry."""

import importlib
import sys

from vibearr_runner.backend_registry import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.backend_registry")

sys.modules[__name__] = _module
