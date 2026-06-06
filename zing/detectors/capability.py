"""Capability-claim verification detector.

Compares the capabilities a model is *claimed* to have (from the resolved KB
profile, when present) against what the relay actually delivers: tool calling,
JSON mode, strict JSON schema, and effective max output. A relay can claim a
premium model yet quietly serve a cheaper substitute that lacks — or, tellingly,
*over*-delivers — these features. Findings stay observation-first; when no
profile is resolved we report observed behavior only and make no downgrade claim.

Budget: up to 4 chat completions.
"""

from __future__ import annotations

from zing.context import AuditContext
from zing.detectors.base import Detector, register
from zing.detectors.helpers import first_json_object
from zing.models import DetectorResult, Dimension, Finding, RequestSpec, Severity, Status

_WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}

_PERSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "person",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name", "age"],
            "additionalProperties": False,
        },
    },
}


@register
class CapabilityDetector(Detector):
    id = "capability"
    name = "Capability-claim verification"
    dimension = Dimension.CAPABILITY
    min_suite = "standard"

    async def run(self, ctx: AuditContext) -> DetectorResult:
        result = self.new_result()
        model = ctx.profile.model if ctx.profile else None
        result.evidence["has_profile"] = model is not None
        score_parts: list[float] = []

        await self._check_tools(ctx, result, model, score_parts)
        await self._check_json_mode(ctx, result, model, score_parts)
        await self._check_json_schema(ctx, result, model, score_parts)
        await self._check_max_output(ctx, result, model, score_parts)

        result.score = round(sum(score_parts) / len(score_parts), 1) if score_parts else None
        result.status = self._roll_up_status(result)
        return result

    # -- sub-checks -------------------------------------------------------- #
    async def _check_tools(self, ctx, result, model, score_parts) -> None:
        """Probe function/tool calling and inspect the arguments encoding."""
        spec = RequestSpec(
            messages=[
                {
                    "role": "user",
                    "content": "Use the tool to get the weather in Paris. Do not answer directly.",
                }
            ],
            tools=[_WEATHER_TOOL],
            tool_choice="auto",
            temperature=0.0,
            max_tokens=128,
        )
        outcome = await ctx.client.complete(spec)
        claimed = bool(model and model.supports_tools)

        if not outcome.ok:
            result.findings.append(
                Finding(
                    id="capability.tools",
                    title="Tool-calling probe failed to complete",
                    status=Status.INCONCLUSIVE,
                    severity=Severity.LOW,
                    summary=outcome.error_message or f"HTTP {outcome.status_code}",
                    evidence={"status_code": outcome.status_code, "error_type": outcome.error_type},
                )
            )
            score_parts.append(50.0)
            return

        call = outcome.tool_calls[0] if outcome.tool_calls else None
        fn = call.get("function") if isinstance(call, dict) else None
        fn_name = fn.get("name") if isinstance(fn, dict) else None
        has_call = bool(fn_name)

        if has_call:
            # Native OpenAI returns function.arguments as a JSON *string*. A dict
            # here suggests a non-OpenAI engine behind an OpenAI-shaped relay.
            args = fn.get("arguments") if isinstance(fn, dict) else None
            non_openai_encoding = isinstance(args, (dict, list))
            evidence = {
                "tool_name": fn_name,
                "arguments_type": type(args).__name__,
                "non_openai_arguments_encoding": non_openai_encoding,
            }
            result.findings.append(
                Finding(
                    id="capability.tools",
                    title="Tool calling delivered",
                    status=Status.PASS,
                    severity=Severity.INFO,
                    summary=f"Returned a tool call to '{fn_name}'.",
                    evidence=evidence,
                )
            )
            score_parts.append(100.0)
            if non_openai_encoding:
                result.findings.append(
                    Finding(
                        id="capability.tools.encoding",
                        title="Non-OpenAI tool arguments encoding",
                        status=Status.WARN,
                        severity=Severity.LOW,
                        summary=(
                            "function.arguments arrived as a structured object rather than a "
                            "JSON string; genuine OpenAI-compatible APIs return a string "
                            "(possible substitute engine)."
                        ),
                        evidence=evidence,
                    )
                )
        elif claimed:
            result.findings.append(
                Finding(
                    id="capability.tools",
                    title="Claimed tool-calling not delivered",
                    status=Status.FAIL,
                    severity=Severity.MEDIUM,
                    summary=(
                        "Profile claims tool-calling support but the relay returned no tool "
                        "call for an explicit tool-use request."
                    ),
                    evidence={"finish_reason": outcome.finish_reason, "had_content": outcome.has_content()},
                    recommendation="Confirm the served engine actually supports function calling.",
                )
            )
            score_parts.append(0.0)
        else:
            # No claim to verify against — observed-only.
            result.findings.append(
                Finding(
                    id="capability.tools",
                    title="No tool call returned",
                    status=Status.INFO,
                    severity=Severity.INFO,
                    summary="Relay returned no tool call; capability not claimed in any profile.",
                    evidence={"finish_reason": outcome.finish_reason},
                )
            )

    async def _check_json_mode(self, ctx, result, model, score_parts) -> None:
        """Verify response_format=json_object yields a parseable object."""
        spec = RequestSpec(
            messages=[
                {
                    "role": "user",
                    "content": 'Return a JSON object {"status":"ok","value":7429}. Only JSON.',
                }
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=80,
        )
        outcome = await ctx.client.complete(spec)
        claimed = bool(model and model.supports_json_mode)

        if not outcome.ok:
            result.findings.append(
                Finding(
                    id="capability.json_mode",
                    title="JSON-mode probe failed to complete",
                    status=Status.INCONCLUSIVE,
                    severity=Severity.LOW,
                    summary=outcome.error_message or f"HTTP {outcome.status_code}",
                    evidence={"status_code": outcome.status_code, "error_type": outcome.error_type},
                )
            )
            score_parts.append(50.0)
            return

        parsed = first_json_object(outcome.content)
        value_ok = parsed is not None and parsed.get("value") == 7429

        if parsed is not None and value_ok:
            result.findings.append(
                Finding(
                    id="capability.json_mode",
                    title="JSON mode delivered",
                    status=Status.PASS,
                    severity=Severity.INFO,
                    summary="response_format=json_object produced a parseable object with the requested value.",
                    evidence={"parsed_keys": sorted(parsed.keys())},
                )
            )
            score_parts.append(100.0)
        elif claimed:
            result.findings.append(
                Finding(
                    id="capability.json_mode",
                    title="Claimed JSON mode not delivered",
                    status=Status.FAIL,
                    severity=Severity.MEDIUM,
                    summary=(
                        "Profile claims JSON-mode support but the relay did not return a valid "
                        "JSON object carrying the requested value."
                    ),
                    evidence={"parsed": parsed is not None, "value_matched": value_ok},
                    recommendation="Verify the served engine honors response_format=json_object.",
                )
            )
            score_parts.append(0.0)
        else:
            status = Status.INFO if parsed is not None else Status.WARN
            result.findings.append(
                Finding(
                    id="capability.json_mode",
                    title="JSON mode observed without strong claim",
                    status=status,
                    severity=Severity.INFO,
                    summary=(
                        "Returned a JSON object but value mismatched."
                        if parsed is not None
                        else "No parseable JSON object returned; capability not claimed."
                    ),
                    evidence={"parsed": parsed is not None, "value_matched": value_ok},
                )
            )
            score_parts.append(70.0 if parsed is not None else 50.0)

    async def _check_json_schema(self, ctx, result, model, score_parts) -> None:
        """Strict json_schema: verify when claimed; flag over-delivery when not.

        If the profile says the genuine model lacks strict json_schema but the
        relay enforces it flawlessly, that is a substitute red flag. If the model
        is claimed to support it, just verify it works. Single call; skipped when
        we have no profile to anchor the claim.
        """
        if model is None:
            return  # Nothing to compare against — skip to stay within budget.

        spec = RequestSpec(
            messages=[
                {
                    "role": "user",
                    "content": 'Return a person object for "Ada Lovelace", age 36. Only JSON.',
                }
            ],
            response_format=_PERSON_SCHEMA,
            temperature=0.0,
            max_tokens=80,
        )
        outcome = await ctx.client.complete(spec)

        if not outcome.ok:
            result.findings.append(
                Finding(
                    id="capability.json_schema",
                    title="JSON-schema probe failed to complete",
                    status=Status.INCONCLUSIVE,
                    severity=Severity.LOW,
                    summary=outcome.error_message or f"HTTP {outcome.status_code}",
                    evidence={"status_code": outcome.status_code, "error_type": outcome.error_type},
                )
            )
            score_parts.append(50.0)
            return

        parsed = first_json_object(outcome.content)
        enforced = self._matches_person_schema(parsed)

        if model.supports_json_schema:
            if enforced:
                result.findings.append(
                    Finding(
                        id="capability.json_schema",
                        title="Strict JSON schema honored",
                        status=Status.PASS,
                        severity=Severity.INFO,
                        summary="Relay returned an object conforming to the strict schema, as claimed.",
                        evidence={"conforms": True},
                    )
                )
                score_parts.append(100.0)
            else:
                result.findings.append(
                    Finding(
                        id="capability.json_schema",
                        title="Claimed strict JSON schema not enforced",
                        status=Status.WARN,
                        severity=Severity.LOW,
                        summary="Profile claims json_schema support but the response did not conform.",
                        evidence={"conforms": False, "parsed": parsed is not None},
                    )
                )
                score_parts.append(40.0)
        else:
            # Claimed model does NOT support strict json_schema. A conforming object
            # here is only a faint hint of a substitute: the person schema is trivial
            # enough that plain instruction-following satisfies it without real schema
            # enforcement, so this is INFO-only and never escalates risk on its own.
            if enforced:
                result.findings.append(
                    Finding(
                        id="capability.json_schema",
                        title="Returned a schema-conforming object though claimed model lacks strict json_schema",
                        status=Status.INFO,
                        severity=Severity.INFO,
                        summary=(
                            "The response conformed to the requested schema even though the claimed "
                            "model is not documented to support strict json_schema. This trivial "
                            "schema is satisfiable by instruction-following alone, so it is not "
                            "reliable substitute evidence — informational only."
                        ),
                        evidence={"conforms": True, "claimed_supports_json_schema": False},
                    )
                )
                score_parts.append(90.0)
            else:
                result.findings.append(
                    Finding(
                        id="capability.json_schema",
                        title="Strict JSON schema not enforced (consistent with claim)",
                        status=Status.INFO,
                        severity=Severity.INFO,
                        summary="Relay did not enforce strict json_schema, consistent with the claimed model.",
                        evidence={"conforms": False},
                    )
                )
                score_parts.append(100.0)

    async def _check_max_output(self, ctx, result, model, score_parts) -> None:
        """Flag gross under-delivery of output length against the claimed max.

        Cost-bounded: we request at most 2048 tokens, so this only catches a model
        that gives up far below the cap, not the full claimed ceiling.
        """
        declared = ctx.declared_max_output()
        cap = min(declared or 2048, 2048)
        spec = RequestSpec(
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Output the integers from 1 upward, one per line (1, 2, 3, ...). "
                        "Continue as long as you can."
                    ),
                }
            ],
            temperature=0.0,
            max_tokens=cap,
        )
        outcome = await ctx.client.complete(spec)

        if not outcome.ok:
            result.findings.append(
                Finding(
                    id="capability.max_output",
                    title="Max-output probe failed to complete",
                    status=Status.INCONCLUSIVE,
                    severity=Severity.LOW,
                    summary=outcome.error_message or f"HTTP {outcome.status_code}",
                    evidence={"status_code": outcome.status_code, "error_type": outcome.error_type},
                )
            )
            score_parts.append(50.0)
            return

        content = outcome.content or ""
        lines = [ln for ln in content.splitlines() if ln.strip()]
        line_count = len(lines)
        char_count = len(content)
        finish = outcome.finish_reason
        evidence = {
            "declared_max_output": declared,
            "requested_max_tokens": cap,
            "finish_reason": finish,
            "line_count": line_count,
            "char_count": char_count,
            "note": "probe capped at 2048 tokens; full declared ceiling not exercised",
        }

        if finish == "length":
            # Hit the cap — expected, the model kept producing as asked.
            result.findings.append(
                Finding(
                    id="capability.max_output",
                    title="Sustained output to the request cap",
                    status=Status.PASS,
                    severity=Severity.INFO,
                    summary=f"Produced ~{line_count} lines and stopped at the {cap}-token cap (finish_reason=length).",
                    evidence=evidence,
                )
            )
            score_parts.append(100.0)
            return

        # Stopped early. Gauge how far below the cap by a rough token estimate.
        produced_ratio = (char_count / 4) / cap if cap else 0.0
        large_claim = bool(declared and declared >= 4096)

        if produced_ratio < 0.25 and large_claim:
            # LOW, not MEDIUM: a 2048-token probe is weak evidence — models stop
            # early on tedious tasks for benign reasons, so this must not escalate
            # overall risk on its own.
            result.findings.append(
                Finding(
                    id="capability.max_output",
                    title="Output stopped far below requested length",
                    status=Status.WARN,
                    severity=Severity.LOW,
                    summary=(
                        f"Model gave up early (finish_reason={finish}) at roughly "
                        f"{produced_ratio*100:.0f}% of the {cap}-token request despite a large "
                        f"declared max_output of {declared}. Weak signal (short probe)."
                    ),
                    evidence=evidence,
                    recommendation="Long-form generation may under-deliver relative to the claimed ceiling; confirm with a longer probe.",
                )
            )
            score_parts.append(60.0)
        elif produced_ratio < 0.25:
            result.findings.append(
                Finding(
                    id="capability.max_output",
                    title="Output stopped early",
                    status=Status.WARN,
                    severity=Severity.LOW,
                    summary=f"Model stopped early (finish_reason={finish}) at ~{produced_ratio*100:.0f}% of the request cap.",
                    evidence=evidence,
                )
            )
            score_parts.append(70.0)
        else:
            result.findings.append(
                Finding(
                    id="capability.max_output",
                    title="Output length plausible",
                    status=Status.PASS,
                    severity=Severity.INFO,
                    summary=f"Produced ~{line_count} lines (finish_reason={finish}); no gross under-delivery.",
                    evidence=evidence,
                )
            )
            score_parts.append(90.0)

    # -- helpers ----------------------------------------------------------- #
    @staticmethod
    def _matches_person_schema(parsed) -> bool:
        """True only if ``parsed`` strictly conforms to the person schema."""
        if not isinstance(parsed, dict):
            return False
        if set(parsed.keys()) != {"name", "age"}:
            return False
        return isinstance(parsed.get("name"), str) and isinstance(parsed.get("age"), int) and not isinstance(
            parsed.get("age"), bool
        )

    @staticmethod
    def _roll_up_status(result: DetectorResult) -> Status:
        """Derive the detector status from the worst sub-check finding."""
        statuses = {f.status for f in result.findings}
        if Status.FAIL in statuses:
            return Status.FAIL
        if Status.WARN in statuses:
            return Status.WARN
        if statuses & {Status.PASS}:
            return Status.PASS
        if statuses and statuses <= {Status.INFO}:
            return Status.INFO
        return Status.INCONCLUSIVE
