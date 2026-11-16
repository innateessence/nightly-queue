"""Compatibility import for vibearr_app.urls."""

import importlib
import sys

from vibearr_app.urls import *  # noqa: F403

_module = importlib.import_module("vibearr_app.urls")

sys.modules[__name__] = _module
