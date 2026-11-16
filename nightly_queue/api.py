"""Compatibility import for vibearr_app.api."""

import importlib
import sys

from vibearr_app.api import *  # noqa: F403

_module = importlib.import_module("vibearr_app.api")

sys.modules[__name__] = _module
