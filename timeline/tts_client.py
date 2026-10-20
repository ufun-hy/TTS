"""Compatibility import for unmodified main business tests.

Implementation moved to server.tts_client for Windows. Keep this module as
an alias to that exact implementation, without duplicating client logic.
Remove only after the historical tests and imports are formally retired.
"""
import sys
from server import tts_client as _implementation

sys.modules[__name__] = _implementation
