"""Compatibility import for vibearr_runner.release_evidence."""

import importlib
import sys

from vibearr_runner.release_evidence import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.release_evidence")

sys.modules[__name__] = _module
