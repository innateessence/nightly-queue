"""Compatibility import for vibearr_app.asgi."""

import importlib
import sys

from vibearr_app.asgi import *  # noqa: F403

_module = importlib.import_module("vibearr_app.asgi")

sys.modules[__name__] = _module
