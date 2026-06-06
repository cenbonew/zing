"""zing — LLM relay reality check.

A local-first CLI that audits whether an OpenAI-compatible or Anthropic-native API
relay actually serves the model it claims to (货不对板检测): real context window,
model identity and downgrade fingerprinting, capability claims, token/billing
sanity, streaming authenticity, reliability, and security signals.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the installed package metadata (pyproject `version`).
    __version__ = version("zing-audit")
except PackageNotFoundError:  # not installed (e.g. running from a source tree)
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
