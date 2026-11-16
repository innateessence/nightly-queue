"""Compatibility import for vibearr_runner.native_evidence."""

import importlib
import sys

from vibearr_runner.native_evidence import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.native_evidence")

sys.modules[__name__] = _module
