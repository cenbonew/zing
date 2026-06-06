"""zing — LLM relay reality check.

A local-first CLI that audits whether an OpenAI-compatible API relay actually
serves the model it claims to (货不对板检测): real context window, model identity
and downgrade fingerprinting, capability claims, token/billing sanity, streaming
authenticity, reliability and static security signals.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
