"""Tests for knowledge-base loading and model-id resolution."""

from __future__ import annotations

from zing.knowledge import KnowledgeBase, load_knowledge_base
from zing.knowledge.schema import (
    FingerprintProbe,
    ModelProfile,
    ProviderProfile,
    ResolvedProfile,
    _normalize,
)


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def test_load_knowledge_base_has_providers(knowledge_base):
    assert isinstance(knowledge_base, KnowledgeBase)
    assert knowledge_base.providers, "expected packaged provider profiles"
    # Spot-check a couple of well-known providers ship with the package.
    assert "openai" in knowledge_base.providers
    assert "anthropic" in knowledge_base.providers


def test_load_knowledge_base_is_callable_fresh():
    kb = load_knowledge_base()
    assert kb.providers
    # all_models flattens (provider, model) pairs.
    pairs = kb.all_models()
    assert pairs
    assert all(isinstance(p, ProviderProfile) and isinstance(m, ModelProfile) for p, m in pairs)


# --------------------------------------------------------------------------- #
# _normalize
# --------------------------------------------------------------------------- #
def test_normalize_strips_separators_and_lowercases():
    assert _normalize("GPT-4o") == "gpt4o"
    assert _normalize("Claude 3.5 Sonnet") == "claude35sonnet"
    assert _normalize("deepseek_chat") == "deepseekchat"
    assert _normalize("") == ""


# --------------------------------------------------------------------------- #
# resolve — against a small synthetic KB (deterministic, independent of data) #
# --------------------------------------------------------------------------- #
def _synthetic_kb() -> KnowledgeBase:
    model = ModelProfile(
        id="acme-large",
        aliases=["acme-l", "acme-large-2024-01-01"],
        family="acme",
        context_window_tokens=128000,
        max_output_tokens=4096,
    )
    other = ModelProfile(id="acme-small", aliases=["acme-s"])
    provider = ProviderProfile(provider="acme", models=[model, other])
    return KnowledgeBase(providers={"acme": provider})


def test_resolve_exact_id():
    kb = _synthetic_kb()
    r = kb.resolve("acme-large")
    assert isinstance(r, ResolvedProfile)
    assert r.model.id == "acme-large"
    assert r.match_confidence == "exact"


def test_resolve_alias():
    kb = _synthetic_kb()
    r = kb.resolve("acme-l")
    assert r is not None
    assert r.model.id == "acme-large"
    assert r.match_confidence == "alias"


def test_resolve_normalized_exact_treated_as_alias():
    kb = _synthetic_kb()
    # Different casing/separators of an exact id resolve via the normalized path.
    r = kb.resolve("ACME_LARGE")
    assert r is not None
    assert r.model.id == "acme-large"
    assert r.match_confidence == "alias"


def test_resolve_fuzzy_substring():
    kb = _synthetic_kb()
    # A relay-decorated id that is a superset of the real id -> fuzzy match.
    r = kb.resolve("relay-acme-large-turbo")
    assert r is not None
    assert r.model.id == "acme-large"
    assert r.match_confidence == "fuzzy"


def test_resolve_unknown_returns_none():
    kb = _synthetic_kb()
    assert kb.resolve("totally-unrelated-xyz") is None


def test_resolve_empty_returns_none():
    kb = _synthetic_kb()
    assert kb.resolve("") is None


def test_resolve_provider_hint_narrows():
    other = ProviderProfile(
        provider="globex", models=[ModelProfile(id="acme-large", aliases=[])]
    )
    base = _synthetic_kb()
    base.providers["globex"] = other
    r = base.resolve("acme-large", provider_hint="globex")
    assert r is not None
    assert r.provider.provider == "globex"


# --------------------------------------------------------------------------- #
# resolve — against the real packaged KB
# --------------------------------------------------------------------------- #
def test_resolve_real_known_model(knowledge_base):
    r = knowledge_base.resolve("gpt-4o")
    assert r is not None
    assert r.model.id == "gpt-4o"
    assert r.provider.provider == "openai"
    assert r.match_confidence == "exact"
    # Native specs are populated for a known model.
    assert r.model.context_window_tokens > 0


# --------------------------------------------------------------------------- #
# ResolvedProfile.all_fingerprints
# --------------------------------------------------------------------------- #
def test_all_fingerprints_merges_provider_and_model():
    provider = ProviderProfile(
        provider="acme",
        fingerprints=[FingerprintProbe(id="p1", signal="s", prompt="q")],
        models=[
            ModelProfile(
                id="acme-large",
                fingerprints=[FingerprintProbe(id="m1", signal="s", prompt="q")],
            )
        ],
    )
    resolved = ResolvedProfile(provider=provider, model=provider.models[0])
    ids = [fp.id for fp in resolved.all_fingerprints()]
    assert ids == ["p1", "m1"]
