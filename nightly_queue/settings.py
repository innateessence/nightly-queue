"""Compatibility import for vibearr_app.settings."""

import importlib
import sys

from vibearr_app.settings import *  # noqa: F403

_module = importlib.import_module("vibearr_app.settings")

sys.modules[__name__] = _module
