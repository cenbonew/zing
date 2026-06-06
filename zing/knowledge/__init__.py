"""The detection knowledge base: native characteristics of each LLM platform.

YAML profiles under ``data/`` describe how genuine models behave; detectors
compare a relay's observed behavior against the matched profile to spot
substitution, downgrade, and truncation.
"""

from zing.knowledge.loader import load_knowledge_base
from zing.knowledge.schema import (
    FingerprintProbe,
    KnowledgeBase,
    ModelProfile,
    ProviderProfile,
    ResolvedProfile,
)

__all__ = [
    "load_knowledge_base",
    "KnowledgeBase",
    "ProviderProfile",
    "ModelProfile",
    "FingerprintProbe",
    "ResolvedProfile",
]
