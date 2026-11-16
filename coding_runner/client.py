"""Compatibility import for vibearr_runner.client."""

import importlib
import sys

from vibearr_runner.client import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.client")

sys.modules[__name__] = _module
