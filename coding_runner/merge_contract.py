"""Compatibility import for vibearr_runner.merge_contract."""

import importlib
import sys

from vibearr_runner.merge_contract import *  # noqa: F403

sys.modules[__name__] = importlib.import_module("vibearr_runner.merge_contract")
