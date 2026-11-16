"""Compatibility import for vibearr_runner.qualification_trace."""

import importlib
import sys

from vibearr_runner.qualification_trace import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.qualification_trace")

sys.modules[__name__] = _module
