"""Compatibility import for vibearr_runner.merge_github."""

import importlib
import sys

from vibearr_runner.merge_github import *  # noqa: F403

sys.modules[__name__] = importlib.import_module("vibearr_runner.merge_github")
