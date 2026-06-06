"""The runtime context handed to every detector.

A detector receives one ``AuditContext`` and returns a ``DetectorResult``. The
context bundles the live target client, the resolved knowledge-base profile for
the claimed model, run options, and (optionally) a baseline client and an LLM
judge. Detectors must not mutate it.
"""

from __future__ import annotations

from dataclasses import dataclass

from zing.clients import Client
from zing.config import AuditOptions
from zing.judge import Judge
from zing.knowledge import KnowledgeBase, ResolvedProfile
from zing.models import TargetConfig


@dataclass
class AuditContext:
    target: TargetConfig
    client: Client
    options: AuditOptions
    kb: KnowledgeBase
    profile: ResolvedProfile | None = None
    baseline: TargetConfig | None = None
    baseline_client: Client | None = None
    judge: Judge | None = None

    @property
    def has_judge(self) -> bool:
        return self.judge is not None

    @property
    def has_baseline(self) -> bool:
        return self.baseline_client is not None

    def declared_context_window(self) -> int | None:
        """The context window the relay claims, from config override or KB profile."""
        if self.target.declared_context_window:
            return self.target.declared_context_window
        if self.profile and self.profile.model.context_window_tokens > 0:
            return self.profile.model.context_window_tokens
        return None

    def declared_max_output(self) -> int | None:
        if self.target.declared_max_output:
            return self.target.declared_max_output
        if self.profile and self.profile.model.max_output_tokens > 0:
            return self.profile.model.max_output_tokens
        return None

    def tokenizer_hint(self) -> str | None:
        if self.profile and self.profile.model.tokenizer:
            return self.profile.model.tokenizer
        return None
