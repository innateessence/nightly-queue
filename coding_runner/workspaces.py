"""Compatibility import for vibearr_runner.workspaces."""

import importlib
import sys

from vibearr_runner.workspaces import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.workspaces")

sys.modules[__name__] = _module
