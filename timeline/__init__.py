"""Timeline speech generalization for the TTS project."""

from .engine import TimelineEngine, TimelineError, estimate_duration

__all__ = ["TimelineEngine", "TimelineError", "estimate_duration"]
