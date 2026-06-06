"""Load provider YAML profiles into a :class:`KnowledgeBase`.

Profiles ship inside the package under ``data/``. Users can drop additional or
override YAML files via ``--kb-dir`` (or the ``ZING_KB_DIR`` env var) without
forking the project.
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

import yaml

from zing.knowledge.schema import KnowledgeBase, ProviderProfile

_DATA_PACKAGE = "zing.knowledge.data"


def _parse_provider(text: str, source: str) -> ProviderProfile:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"Knowledge profile {source} is not a YAML mapping")
    try:
        return ProviderProfile(**data)
    except Exception as exc:  # pragma: no cover - surfaced to the user
        raise ValueError(f"Invalid knowledge profile {source}: {exc}") from exc


def _load_packaged() -> dict[str, ProviderProfile]:
    providers: dict[str, ProviderProfile] = {}
    root = resources.files(_DATA_PACKAGE)
    for entry in root.iterdir():
        name = entry.name
        if not name.endswith((".yaml", ".yml")):
            continue
        text = entry.read_text(encoding="utf-8")
        profile = _parse_provider(text, name)
        providers[profile.provider] = profile
    return providers


def _load_dir(directory: Path) -> dict[str, ProviderProfile]:
    providers: dict[str, ProviderProfile] = {}
    if not directory.is_dir():
        return providers
    for path in sorted(directory.glob("*.y*ml")):
        profile = _parse_provider(path.read_text(encoding="utf-8"), str(path))
        providers[profile.provider] = profile
    return providers


def load_knowledge_base(extra_dirs: list[Path] | None = None) -> KnowledgeBase:
    """Build the knowledge base from packaged profiles plus optional overrides.

    Later sources win, so a user-supplied profile for ``provider: openai``
    overrides the packaged one.
    """
    providers = _load_packaged()

    dirs: list[Path] = list(extra_dirs or [])
    env_dir = os.environ.get("ZING_KB_DIR")
    if env_dir:
        dirs.append(Path(env_dir))
    for directory in dirs:
        providers.update(_load_dir(directory))

    return KnowledgeBase(providers=providers)
