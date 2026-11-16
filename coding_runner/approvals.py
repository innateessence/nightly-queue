"""Compatibility import for vibearr_runner.approvals."""

import importlib
import sys

from vibearr_runner.approvals import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.approvals")

sys.modules[__name__] = _module
