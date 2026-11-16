"""Compatibility alias for vibearr_runner.native_reads."""

import sys
from vibearr_runner import native_reads as implementation

sys.modules[__name__] = implementation
