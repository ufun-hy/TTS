"""Shared CosyVoice3 prompt-text contract for zero-shot voice extraction."""

from __future__ import annotations

COSYVOICE3_PROMPT_PREFIX = "You are a helpful assistant.<|endofprompt|>"


def build_zero_shot_prompt_text(reference_text: str) -> str:
    """Return the exact CosyVoice3 zero-shot prompt text.

    Reference transcripts are literal spoken text. CosyVoice3 requires the
    end-of-prompt marker before that transcript so prompt context and target
    text stay on opposite sides of the LM boundary.
    """
    if not isinstance(reference_text, str):
        raise TypeError("reference_text must be a string")
    transcript = reference_text.strip()
    if not transcript:
        raise ValueError("reference transcript must not be empty")
    if transcript.startswith(COSYVOICE3_PROMPT_PREFIX):
        transcript = transcript[len(COSYVOICE3_PROMPT_PREFIX):].lstrip()
        if not transcript:
            raise ValueError("reference transcript must not be empty")
    elif "<|endofprompt|>" in transcript:
        raise ValueError("reference transcript must not contain prompt control tokens")
    return COSYVOICE3_PROMPT_PREFIX + transcript
