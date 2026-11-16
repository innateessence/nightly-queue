"""Compatibility import for vibearr_runner.codex_backend."""

import importlib
import sys

from vibearr_runner.codex_backend import *  # noqa: F403

_module = importlib.import_module("vibearr_runner.codex_backend")

sys.modules[__name__] = _module
