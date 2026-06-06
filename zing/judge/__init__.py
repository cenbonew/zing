"""Optional LLM-as-judge backend for the code+LLM hybrid detection mode.

The pure-code detectors decide everything they can deterministically. When the
user opts into ``--judge``, detectors may additionally consult a *trusted* model
(configured separately from the target — never the target itself) to assess
fuzzy signals like quality, style, or semantic equivalence.
"""

from zing.judge.judge import Judge, JudgeUnavailable

__all__ = ["Judge", "JudgeUnavailable"]
