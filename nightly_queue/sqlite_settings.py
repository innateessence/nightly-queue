"""Compatibility import for vibearr_app.sqlite_settings."""

import importlib
import sys

from vibearr_app.sqlite_settings import *  # noqa: F403

_module = importlib.import_module("vibearr_app.sqlite_settings")

sys.modules[__name__] = _module
