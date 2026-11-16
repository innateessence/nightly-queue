"""Compatibility alias for vibearr_runner.native_clone."""

import sys
from vibearr_runner import native_clone as implementation

sys.modules[__name__] = implementation
