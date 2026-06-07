"""Model identity & downgrade fingerprinting — the core 货不对板 detector.

A relay can claim to serve an expensive model while quietly routing to a cheaper
substitute. This detector triangulates identity three ways: a direct self-id
prompt checked against the genuine brand words (and rival brands that betray a
swap), the knowledge base's pure-code behavioral fingerprints (knowledge cutoff,
tokenizer quirks, etc.), and the ``model`` field the relay echoes back. Divergence
between the claim and what the model actually behaves like is the signal.
"""

from __future__ import annotations

import re

from zing.context import AuditContext
from zing.detectors.base import Detector, register
from zing.detectors.helpers import (
    contains_all,
    contains_any,
    contains_word_any,
    words_present,
)
from zing.models import DetectorResult, Dimension, Finding, RequestSpec, Severity, Status

# Substrings that name a cheaper/smaller tier of a family. If the returned model
# field carries one of these and the requested id does not, that is a downgrade.
_DOWNGRADE_HINTS = ("mini", "nano", "flash", "lite", "small", "tiny", "8b", "7b", "3b", "1b")

# Known LLM vendor/brand words (incl. Chinese). A model that self-identifies with
# any of these — OTHER than its own brand — betrays a substitution, even when the KB
# profile's identity_forbidden didn't list it (e.g. a Doubao/ByteDance model resold
# as DeepSeek). Matched whole-word; the model's own identity_keywords are removed.
_RIVAL_BRANDS = (
    "openai", "gpt", "chatgpt",
    "anthropic", "claude",
    "google", "gemini", "deepmind",
    "deepseek", "深度求索",
    "qwen", "tongyi", "通义", "通义千问", "alibaba", "阿里云", "阿里巴巴",
    "doubao", "豆包", "bytedance", "字节", "字节跳动", "seed",
    "kimi", "moonshot", "月之暗面",
    "glm", "chatglm", "zhipu", "智谱",
    "ernie", "文心", "文心一言", "baidu", "百度",
    "hunyuan", "混元", "tencent", "腾讯",
    "llama", "meta",
    "mistral", "mixtral",
    "grok", "xai",
    "cohere", "command",
    "minimax", "abab",
    "讯飞", "星火", "spark",
    "零一万物", "yi",
    "阶跃", "step",
)

_SELF_ID_PROMPT = (
    "What model are you and which company built you? "
    "Answer in one short sentence."
)


@register
class ModelIdentityDetector(Detector):
    id = "model_identity"
    name = "Model identity & downgrade fingerprinting"
    dimension = Dimension.MODEL_IDENTITY
    min_suite = "standard"

    # Cap on fingerprint probes actually issued (cost control, ~8-call budget).
    MAX_FINGERPRINTS = 6
    # Hard ceiling on output tokens per fingerprint probe. Identity fingerprints
    # only need a short answer; without this a KB probe declaring max_tokens=40000
    # (e.g. a max-output check) would bill tens of thousands of tokens per audit
    # for zero identity signal. The real max-output test lives in capability.py.
    MAX_FINGERPRINT_OUTPUT = 512

    async def run(self, ctx: AuditContext) -> DetectorResult:
        result = self.new_result()

        if ctx.profile is None:
            result.findings.append(
                Finding(
                    id="model_identity.no_profile",
                    title="Claimed model not in knowledge base",
                    status=Status.INCONCLUSIVE,
                    severity=Severity.INFO,
                    summary=(
                        "Claimed model not found in knowledge base; "
                        "pass --declared-provider or add a KB profile."
                    ),
                    evidence={"claimed_model": ctx.target.claimed, "requested_model": ctx.target.model},
                )
            )
            result.status = Status.INCONCLUSIVE
            result.score = None
            return result

        model = ctx.profile.model
        has_high = False
        has_medium = False

        # 1) Direct self-identification + brand check. -------------------- #
        forbidden_hit = await self._check_self_id(ctx, model, result)
        if forbidden_hit:
            has_high = True

        # 2) Pure-code behavioral fingerprints. -------------------------- #
        score = 100.0
        probes = [fp for fp in ctx.profile.all_fingerprints() if fp.pure_code_checkable]
        probes = probes[: self.MAX_FINGERPRINTS]
        total_weight = sum(max(fp.weight, 0.0) for fp in probes)
        violated = 0
        # Non-identity (behavioral) fingerprint divergences are individually noisy,
        # so a single one stays LOW and does not escalate overall risk; two or more
        # together raise a MEDIUM aggregate.
        soft_violations: list[str] = []
        for fp in probes:
            outcome = await ctx.client.complete(
                RequestSpec(
                    messages=[{"role": "user", "content": fp.prompt}],
                    temperature=fp.temperature,
                    max_tokens=min(fp.max_tokens, self.MAX_FINGERPRINT_OUTPUT),
                )
            )
            if not (outcome.ok and outcome.has_content()):
                # A relay that errors or stays silent on a probe is not evidence of
                # a downgrade by itself — record it but do not penalize the score.
                result.findings.append(
                    Finding(
                        id=f"model_identity.fp.{fp.id}",
                        title=f"Fingerprint inconclusive: {fp.signal}",
                        status=Status.INCONCLUSIVE,
                        severity=Severity.INFO,
                        summary=outcome.error_message or f"No usable response (HTTP {outcome.status_code}).",
                        evidence={"probe": fp.id, "status_code": outcome.status_code},
                    )
                )
                continue

            text = outcome.content
            forbidden_hits = words_present(text, self._forbidden_brands(model))
            # A rival brand only contradicts identity when the GENUINE brand is
            # absent; an honest model contrasting itself ("not Claude") is benign —
            # same gating as the direct self-id check.
            genuine_present = contains_word_any(text, model.identity_keywords or [])
            forbidden_contradiction = bool(forbidden_hits) and not genuine_present
            is_violation = self._fingerprint_violated(fp, text) or forbidden_contradiction
            if is_violation:
                violated += 1
                if total_weight > 0:
                    # Cap a single probe's penalty so one fragile fingerprint can't
                    # tank the identity score on its own.
                    score -= min((max(fp.weight, 0.0) / total_weight) * 100.0, 25.0)
                if forbidden_contradiction:
                    has_high = True
                    hit = forbidden_hits[0]
                    summary = (
                        f"Probe '{fp.signal}' surfaced a forbidden brand ({hit}); "
                        f"expected behavior consistent with {model.id}."
                    )
                    severity = Severity.HIGH
                    status = Status.FAIL
                else:
                    soft_violations.append(fp.signal)
                    summary = (
                        f"Probe '{fp.signal}' diverged from native behavior. "
                        f"Expected: {fp.native_expected or self._expectation_text(fp)}."
                    )
                    severity = Severity.LOW
                    status = Status.WARN
                result.findings.append(
                    Finding(
                        id=f"model_identity.fp.{fp.id}",
                        title=f"Fingerprint divergence: {fp.signal}",
                        status=status,
                        severity=severity,
                        summary=summary,
                        evidence={
                            "probe": fp.id,
                            "native_expected": fp.native_expected,
                            "downgrade_signal": fp.downgrade_signal,
                            "observed": text[:400],
                            "weight": fp.weight,
                        },
                    )
                )
            else:
                result.findings.append(
                    Finding(
                        id=f"model_identity.fp.{fp.id}",
                        title=f"Fingerprint consistent: {fp.signal}",
                        status=Status.PASS,
                        severity=Severity.INFO,
                        summary=f"Behavior consistent with {model.id}.",
                        evidence={"probe": fp.id, "observed": text[:200]},
                    )
                )

        # Two or more behavioral divergences together justify a MEDIUM aggregate.
        if len(soft_violations) >= 2:
            has_medium = True
            result.findings.append(
                Finding(
                    id="model_identity.fp_aggregate",
                    title="Multiple behavioral fingerprints diverged",
                    status=Status.WARN,
                    severity=Severity.MEDIUM,
                    summary=(
                        f"{len(soft_violations)} pure-code fingerprints diverged from native "
                        f"{model.id} behavior ({', '.join(soft_violations)}); investigate "
                        f"possible substitution."
                    ),
                    evidence={"diverged": soft_violations},
                    recommendation="Run `zing compare` against a trusted baseline to corroborate.",
                )
            )

        # 3) Echoed model field on a plain call. ------------------------- #
        if await self._check_model_field(ctx, result):
            has_medium = True

        # Score & status. ----------------------------------------------- #
        score = max(0.0, min(100.0, score))
        if has_high:
            score = min(score, 20.0)
        result.score = round(score, 1)

        if has_high:
            result.status = Status.FAIL
        elif has_medium or soft_violations:
            result.status = Status.WARN
        else:
            result.status = Status.PASS

        result.evidence["fingerprints_run"] = len(probes)
        result.evidence["fingerprints_violated"] = violated
        result.evidence["match_confidence"] = ctx.profile.match_confidence
        return result

    # ------------------------------------------------------------------ #
    async def _check_self_id(
        self, ctx: AuditContext, model, result: DetectorResult
    ) -> bool:
        """Run the direct self-id prompt; append a finding. Returns True on a
        forbidden-brand (substitution) hit."""
        outcome = await ctx.client.complete(
            RequestSpec(
                messages=[{"role": "user", "content": _SELF_ID_PROMPT}],
                temperature=0.0,
                max_tokens=64,
            )
        )
        evidence: dict = {"prompt": "self-id"}

        # Optional baseline corroboration (informational only).
        if ctx.has_baseline and ctx.baseline_client is not None:
            base = await ctx.baseline_client.complete(
                RequestSpec(
                    messages=[{"role": "user", "content": _SELF_ID_PROMPT}],
                    temperature=0.0,
                    max_tokens=64,
                )
            )
            evidence["baseline_answer"] = (
                base.content[:300] if (base.ok and base.has_content()) else None
            )

        if not (outcome.ok and outcome.has_content()):
            result.findings.append(
                Finding(
                    id="model_identity.self_id",
                    title="Self-identification unavailable",
                    status=Status.INCONCLUSIVE,
                    severity=Severity.INFO,
                    summary=outcome.error_message or f"No usable response (HTTP {outcome.status_code}).",
                    evidence={**evidence, "status_code": outcome.status_code},
                )
            )
            return False

        text = outcome.content
        evidence["target_answer"] = text[:300]
        # Whole-word matching so a brand inside a longer token ("meta" in "metadata",
        # "gpt" in "ChatGPT") doesn't fire, and only treat a rival brand as a swap
        # when the GENUINE brand is ABSENT — an honest model contrasting itself
        # ("I am Claude, not GPT or Gemini") names rivals without being a substitute.
        forbidden_hits = words_present(text, self._forbidden_brands(model))
        keyword_present = contains_word_any(text, model.identity_keywords or [])

        if forbidden_hits and not keyword_present:
            hit = forbidden_hits[0]
            result.findings.append(
                Finding(
                    id="model_identity.self_id",
                    title="Self-identifies as a rival brand",
                    status=Status.FAIL,
                    severity=Severity.HIGH,
                    summary=(
                        f"Self-identifies as a rival brand ({hit}) under the claimed "
                        f"model id {ctx.target.claimed}, without naming the genuine brand."
                    ),
                    evidence={**evidence, "forbidden_hits": forbidden_hits},
                    recommendation="A response naming a different vendor strongly suggests model substitution.",
                )
            )
            return True

        if forbidden_hits and keyword_present:
            result.findings.append(
                Finding(
                    id="model_identity.self_id",
                    title="Self-id names both the genuine and a rival brand",
                    status=Status.WARN,
                    severity=Severity.LOW,
                    summary=(
                        f"Self-id mentions genuine brand words for {model.id} but also a rival "
                        f"brand ({', '.join(forbidden_hits)}); commonly a benign contrast "
                        "rather than a swap. Corroborate before treating as substitution."
                    ),
                    evidence={**evidence, "forbidden_hits": forbidden_hits},
                    recommendation="Run `zing compare` against a trusted baseline to disambiguate.",
                )
            )
            return False

        if not keyword_present:
            result.findings.append(
                Finding(
                    id="model_identity.self_id",
                    title="Evasive self-identification",
                    status=Status.WARN,
                    severity=Severity.LOW,
                    summary=(
                        "Self-id response names neither the genuine brand nor a rival; "
                        "treat as weak/evasive evidence."
                    ),
                    evidence=evidence,
                )
            )
            return False

        result.findings.append(
            Finding(
                id="model_identity.self_id",
                title="Self-identification consistent",
                status=Status.PASS,
                severity=Severity.INFO,
                summary=f"Self-id mentions genuine brand words for {model.id}.",
                evidence=evidence,
            )
        )
        return False

    async def _check_model_field(self, ctx: AuditContext, result: DetectorResult) -> bool:
        """Compare the echoed ``model`` field to the requested id. Returns True if
        it clearly names a different/cheaper model."""
        outcome = await ctx.client.complete(
            RequestSpec(
                messages=[{"role": "user", "content": "Say OK."}],
                temperature=0.0,
                max_tokens=8,
            )
        )
        requested = ctx.target.model
        returned = (outcome.model_returned or "").strip()
        if not (outcome.ok) or not returned:
            result.findings.append(
                Finding(
                    id="model_identity.model_field",
                    title="No usable echoed model field",
                    status=Status.INCONCLUSIVE,
                    severity=Severity.INFO,
                    summary=(
                        outcome.error_message
                        or "Response omitted a 'model' field; cannot compare."
                    ),
                    evidence={"requested": requested, "returned": returned or None},
                )
            )
            return False

        if self._model_field_diverges(requested, returned):
            result.findings.append(
                Finding(
                    id="model_identity.model_field",
                    title="Echoed model field differs from requested",
                    status=Status.WARN,
                    severity=Severity.MEDIUM,
                    summary=(
                        f"Response 'model' field ({returned}) differs from requested "
                        f"({requested})."
                    ),
                    evidence={"requested": requested, "returned": returned},
                    recommendation="A smaller-tier or different-family model id in the response suggests routing to a substitute.",
                )
            )
            return True

        result.findings.append(
            Finding(
                id="model_identity.model_field",
                title="Echoed model field consistent",
                status=Status.PASS,
                severity=Severity.INFO,
                summary=f"Response 'model' field ({returned}) matches requested.",
                evidence={"requested": requested, "returned": returned},
            )
        )
        return False

    # ------------------------------------------------------------------ #
    @staticmethod
    def _forbidden_brands(model) -> list[str]:
        """Brands that would betray a substitution for this model: the global rival
        set plus the profile's explicit list, minus the model's own identity words."""
        own = {k.lower() for k in (model.identity_keywords or [])}
        seen: set[str] = set()
        out: list[str] = []
        for brand in (*(model.identity_forbidden or []), *_RIVAL_BRANDS):
            bl = brand.lower()
            if bl in own or bl in seen:
                continue
            seen.add(bl)
            out.append(brand)
        return out

    @staticmethod
    def _fingerprint_violated(fp, text: str) -> bool:
        """Apply the probe's deterministic expectations to the response text."""
        if fp.expect_contains and not contains_all(text, fp.expect_contains):
            return True
        if fp.expect_contains_any and not contains_any(text, fp.expect_contains_any):
            return True
        if fp.expect_not_contains and contains_any(text, fp.expect_not_contains):
            return True
        if fp.expect_regex:
            try:
                if not re.search(fp.expect_regex, text, re.IGNORECASE):
                    return True
            except re.error:
                # A malformed KB regex is a KB bug, not relay evidence — skip it.
                return False
        return False

    @staticmethod
    def _expectation_text(fp) -> str:
        if fp.expect_contains:
            return f"contains all of {fp.expect_contains}"
        if fp.expect_contains_any:
            return f"contains any of {fp.expect_contains_any}"
        if fp.expect_not_contains:
            return f"does not contain {fp.expect_not_contains}"
        if fp.expect_regex:
            return f"matches /{fp.expect_regex}/"
        return "native behavior"

    @staticmethod
    def _model_field_diverges(requested: str, returned: str) -> bool:
        """Lenient comparison: ignore snapshot suffixes/aliases, flag a clear
        downgrade-tier word or a different family."""
        req = requested.lower()
        ret = returned.lower()
        if req == ret:
            return False

        # A downgrade-tier word present in the returned id but not the requested one.
        # Checked BEFORE the snapshot/alias substring tolerance below, otherwise the
        # most common substitution — gpt-4o -> gpt-4o-mini, where the requested id is
        # a substring of the returned one — would be waved through as an "alias".
        for hint in _DOWNGRADE_HINTS:
            if hint in ret and hint not in req:
                return True

        def norm(name: str) -> str:
            return "".join(ch for ch in name if ch.isalnum())

        nreq, nret = norm(req), norm(ret)
        # Snapshot/alias tolerance: one being a prefix/substring of the other
        # (e.g. "gpt-4o" vs "gpt-4o-2024-08-06") is not a divergence.
        if nreq and nret and (nreq in nret or nret in nreq):
            return False

        # Different leading family token (e.g. "gpt" vs "qwen", "claude" vs "llama").
        req_family = re.split(r"[-_/:.\s]", req, maxsplit=1)[0]
        ret_family = re.split(r"[-_/:.\s]", ret, maxsplit=1)[0]
        return bool(req_family and ret_family and req_family != ret_family)
