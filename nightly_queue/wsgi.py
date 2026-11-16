"""Compatibility import for vibearr_app.wsgi."""

import importlib
import sys

from vibearr_app.wsgi import *  # noqa: F403

_module = importlib.import_module("vibearr_app.wsgi")

sys.modules[__name__] = _module
