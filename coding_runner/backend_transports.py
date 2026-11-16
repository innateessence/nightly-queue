"""Compatibility import for vibearr_runner.backend_transports."""

import importlib
import sys

from vibearr_runner.backend_transports import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.backend_transports")

sys.modules[__name__] = _module
