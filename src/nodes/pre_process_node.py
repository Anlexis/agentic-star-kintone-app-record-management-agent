"""AgentCore Platform v1.0 - outer pre_process node.

Cat 2 outer backbone: validate the caller's request (raw NL text + the
structured caller fields) and serialize it into `validated_input` for the inner
kintone workflow graph. This node OWNS the caller-data contract: every field a
caller can supply is checked here, against explicit bounds, before any of it
reaches the workflow. Business rules (intent, entity extraction) live in the
inner graph.

Caller contract (`input_context`), every field optional:

    app_id / app / app_hint              target kintone app: a numeric app id
    record_id / record_no / record_hint  target record: a numeric record number

Rules applied to it:
  - the value must be a STRING or an INTEGER. A float, boolean, mapping or
    list is refused outright, never coerced: str(float("nan")) is "nan" and
    str(1e9) is "1000000000.0", so coercion would let a non-finite or
    unbounded value name the target of a write. An integer is accepted because
    a kintone app id is genuinely a number on the wire, but it goes through the
    same finite + in-range check as everything else.
  - the value must match the identifier SHAPE - 1 to 9 digits, no sign, no
    separators, in the range 1..999999999. The id renders into the
    caller-facing confirmation and into the `kintone://` reference, so
    anything but an inert numeric identifier there would be caller-controlled
    output.
  - instruction-override text (directives aimed at the MODEL: role
    reassignment, system-prompt manipulation, chat-template control tokens) is
    REFUSED, fail closed, on BOTH caller text channels - the raw instruction
    text and every decoded string in input_context, keys included, at any
    depth - before anything is carried forward. The screen is the template's
    own (src/services/security.py), never delegated to the framework gate.
  - a refusal names the FIELD plus a fixed reason code and never echoes the
    offending value; a hostile field NAME is masked, never echoed either.
  - an absent hint is simply absent: the workflow falls back to an id named in
    the instruction text, and an unresolvable target is a clean error rather
    than a guess.
"""

import json
import math
import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json
from src.services.security import contains_instruction_override, sanitize_query

# A kintone app id / record number: a short numeric identifier. The same shape
# the inner field-inference step applies to ids named in the request text.
_ID_SHAPE_RE = re.compile(r"^\d{1,9}$")
_ID_MIN = 1
_ID_MAX = 999_999_999

# Aliases accepted for each target, first supplied wins.
_APP_ALIASES = ("app_id", "app", "app_hint")
_RECORD_ALIASES = ("record_id", "record_no", "record_hint")


# Refusal codes for a caller field - the closed set of reasons a contract breach
# may be reported with. The field name is a contract alias (or the envelope key
# itself), so a refusal line is made of two closed-set parts and nothing the
# caller sent.
_CODE_NOT_NUMERIC = "must be a numeric identifier"
_CODE_EMPTY = "must not be empty"
_CODE_BAD_SHAPE = "is not a valid identifier"
_CODE_OUT_OF_RANGE = "is out of range"
_CODE_NOT_MAPPING = "must be a mapping"
CALLER_FIELD_CODES = frozenset({_CODE_NOT_NUMERIC, _CODE_EMPTY, _CODE_BAD_SHAPE, _CODE_OUT_OF_RANGE, _CODE_NOT_MAPPING})


class CallerFieldError(ValueError):
    """A caller-supplied field failed its contract.

    Carries the contract FIELD name and one of CALLER_FIELD_CODES - both closed
    sets - so the refusal written to error_log is built from those attributes
    and never from the exception's text. The rejected value is not part of the
    exception at all.
    """

    def __init__(self, field: str, code: str) -> None:
        self.field = field
        self.code = code
        super().__init__(f"'{field}' {code}")


# Contract field names that may be echoed into a refusal message. Any other
# input_context key is caller-controlled text, so its spot in the reported path
# shows a placeholder - a hostile field NAME is never echoed either.
_KNOWN_CONTEXT_FIELDS = frozenset(_APP_ALIASES + _RECORD_ALIASES)


def _find_instruction_override(value: object, path: str = "input_context") -> "str | None":
    """Depth-first scan of every decoded string in the mapping - keys included.

    Returns the path of the first string carrying an instruction-override
    directive, or None. The walk runs on the PARSED mapping, so JSON escaping
    cannot smuggle a phrase past it, and it covers undeclared keys too: the
    screen must hold on what the caller SENT, not only on what the contract
    keeps. Path components outside the declared contract are masked, so the
    returned path is always safe to name in an error message.
    """
    if isinstance(value, str):
        return path if contains_instruction_override(value) else None
    if isinstance(value, dict):
        for key, item in value.items():
            safe_key = key if isinstance(key, str) and key in _KNOWN_CONTEXT_FIELDS else "<unrecognised-field>"
            key_path = f"{path}.{safe_key}"
            if isinstance(key, str) and contains_instruction_override(key):
                return key_path
            found = _find_instruction_override(item, key_path)
            if found is not None:
                return found
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = _find_instruction_override(item, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _validate_numeric_id(raw: object, field: str) -> str:
    """Validate a target reference: a finite, in-range, inert numeric identifier.

    Fails CLOSED on every other shape. bool is rejected explicitly because
    `isinstance(True, int)` is True in Python, and a float is rejected outright
    rather than truncated - NaN and Infinity parse fine and then compare False
    against any bound, which is exactly how a non-finite value slips through a
    range check and ends up naming the target of a write.
    """
    if isinstance(raw, bool):
        raise CallerFieldError(field, _CODE_NOT_NUMERIC)
    if isinstance(raw, int):
        number = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise CallerFieldError(field, _CODE_EMPTY)
        if not _ID_SHAPE_RE.match(text):
            raise CallerFieldError(field, _CODE_BAD_SHAPE)
        number = int(text)
    else:
        # float included: a non-integral or non-finite value can never be a
        # kintone identifier, and math.isfinite() would still admit 1e9.
        raise CallerFieldError(field, _CODE_NOT_NUMERIC)
    if not math.isfinite(number) or not (_ID_MIN <= number <= _ID_MAX):
        raise CallerFieldError(field, _CODE_OUT_OF_RANGE)
    return str(number)


def validate_caller_fields(input_context: object) -> "dict[str, str]":
    """Validate the caller contract. Raises CallerFieldError on any breach."""
    if input_context in (None, {}):
        return {}
    if not isinstance(input_context, dict):
        raise CallerFieldError("input_context", _CODE_NOT_MAPPING)

    fields: dict[str, str] = {}
    for target, aliases in (("app_hint", _APP_ALIASES), ("record_hint", _RECORD_ALIASES)):
        supplied = [alias for alias in aliases if input_context.get(alias) is not None]
        if supplied:
            fields[target] = _validate_numeric_id(input_context[supplied[0]], supplied[0])
    return fields


class PreProcessNode(FunctionNode):
    """Validate the caller contract and shape the request for the inner graph."""

    # The outer backbone's SINGLE external trust gate. A real caller enters at
    # VERIFIED_EXTERNAL and the inner kintone call runs under this same
    # (unelevated) context, so the external gate lives HERE, not on the inner
    # API node. An under-trusted (ANONYMOUS) caller is denied at this gate
    # before any call.
    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only

        if not isinstance(user_input, str) or not user_input.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        # ── Instruction-override screen (template-owned, fail CLOSED) ────────
        # Runs on BOTH caller text channels before anything is carried forward:
        # a refusal leaves no validated_input, no hints and no caller_fields
        # for any downstream node. The screen lives in this node's own
        # execute() path - calling execute() directly still refuses, so the
        # guarantee does not depend on any framework gate being present or
        # configured on.
        if contains_instruction_override(user_input):
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "instruction_override", "where": "user_input"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: request refused - instruction-override content in user_input"],
            }

        override_path = _find_instruction_override(input_context) if isinstance(input_context, (dict, list)) else None
        if override_path is not None:
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "instruction_override", "where": override_path},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: request refused - instruction-override content in {override_path}"],
            }

        try:
            caller_fields = validate_caller_fields(input_context)
        except CallerFieldError as exc:
            # Fail closed, naming the field and a fixed code only. The line is
            # built from the exception's closed-set attributes, never from its
            # text, and the rejected value is never echoed into the log.
            field, code = exc.field, exc.code
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: rejected caller input - '{field}' {code}"],
            }

        # Strip markup + cap length before serialization.
        sanitized_input = sanitize_query(user_input.strip())

        app_hint = caller_fields.get("app_hint", "")
        record_hint = caller_fields.get("record_hint", "")
        validated_input = json.dumps({"text": sanitized_input})

        # Audit the shaped request - hint presence only, never the raw text.
        emit_trace_event(
            "pre_process_complete",
            {
                "has_app_hint": bool(app_hint),
                "has_record_hint": bool(record_hint),
                "caller_fields": sorted(caller_fields),
            },
            state,
        )

        return {
            "validated_input": validated_input,
            "app_hint": app_hint,
            "record_hint": record_hint,
            # Stored as a JSON string (the state contract keeps every value
            # msgpack-safe); the graph node reads it back at the boundary.
            "caller_fields": to_json(caller_fields),
            "status": AgentStatus.SUCCESS.value,
        }
