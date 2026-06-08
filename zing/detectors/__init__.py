"""Detector package.

Importing this package imports every detector module, which runs each
``@register`` decorator and populates :data:`zing.detectors.base.REGISTRY`. The
runner relies on that side effect to discover detectors.
"""

from zing.detectors import (  # noqa: F401  -- imported for registration side effects
    billing,
    capability,
    connectivity,
    context_window,
    determinism,
    injected_prompt,
    integrity,
    model_identity,
    prompt_cache,
    protocol,
    quality_judge,  # noqa: F401
    reliability,
    security,
    streaming,
    vision,
)
from zing.detectors.base import (
    REGISTRY,
    Detector,
    register,
    run_detector,
    select_detectors,
)

__all__ = ["REGISTRY", "Detector", "register", "run_detector", "select_detectors"]
