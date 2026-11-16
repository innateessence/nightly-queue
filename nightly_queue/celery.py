"""Compatibility import for vibearr_app.celery."""

import importlib
import sys

from vibearr_app.celery import *  # noqa: F403

_module = importlib.import_module("vibearr_app.celery")

sys.modules[__name__] = _module
