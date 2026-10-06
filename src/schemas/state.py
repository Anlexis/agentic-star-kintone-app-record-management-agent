"""AgentCore Platform v1.0 - CMN-C2-276 Kintone App Record Agent state."""

# State must be a flat TypedDict - never a Pydantic BaseModel. LangGraph
# checkpoints use msgpack serialization; Pydantic objects (and nested dict/list
# containers) are not msgpack-safe. Extend AgentState with agent-specific
# fields only, and declare every domain field NotRequired[...] (fields are
# absent until their producer node writes them). kintone_payload /
# kintone_config / redaction_flags / caller_fields are dicts/lists at the point
# of use but are stored in State as JSON strings via to_json/from_json below.
# Do NOT add credentials, secrets, or Pydantic models. The kintone integration
# token is NEVER stored here - it is read via ctx.secrets in
# CallKintoneApiNode.

from __future__ import annotations

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a list/dict State value to a compact JSON string (msgpack-safe).

    Returns None for None so the field stays a true Optional[str].
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: Any, default: Any) -> Any:
    """Deserialize a JSON-string State value back to its list/dict form.

    Tolerant by design: None/empty -> default; an already-native list/dict (e.g. a value
    supplied directly in a unit test) passes through unchanged; a malformed string -> default.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class State(AgentState):
    """Kintone App Record agent state.

    Shared fields (user_input, validated_input, intent, result, status,
    formatted_output, session_id, node_history, error_log, correlation_id,
    trace_id, hitl_*, etc.) are inherited from AgentState and NOT re-declared.
    Only kintone-workflow fields are added below, all NotRequired (the state
    contract). All values are JSON/msgpack-serializable primitives - the
    kintone integration token is NEVER stored here (accessed via ctx.secrets).
    """

    # Caller-supplied target hints (kintone app id / record number from
    # input_context). Never inferred; resolution to a kintone app id / record
    # number is explicit-only (pass-through when the hint or request text
    # already carries the id).
    app_hint: NotRequired[str]
    record_hint: NotRequired[str]
    app_id: NotRequired[str]  # resolved kintone app id

    # The VALIDATED caller contract, as produced by PreProcessNode. Stored as a
    # JSON string (msgpack-safe) and read back at the graph boundary, where it
    # crosses into the inner graph over the context bridge.
    caller_fields: NotRequired[Optional[str]]

    # ValidateInput (deterministic sensitive-value scan)
    # JSON list[str] of pattern categories redacted from the text before
    # logging (stored as a JSON string; (de)serialize via to_json/from_json).
    redaction_flags: NotRequired[Optional[str]]

    # InferKintoneFields
    record_title: NotRequired[str]  # record title / display label
    # JSON - assembled kintone REST API request body (stored as a JSON string,
    # not a native dict; (de)serialize via to_json/from_json).
    kintone_payload: NotRequired[Optional[str]]

    # The `kintone:` section of config/config.yaml, forwarded by
    # _parent_config() and injected by the inner graph's
    # _extra_initial_state() (JSON string).
    kintone_config: NotRequired[Optional[str]]

    # CallKintoneApi
    record_id: NotRequired[str]  # record number / record id returned by kintone
    record_ref: NotRequired[str]  # human-readable reference (kintone://app/<app>/records/<id>)

    # Confirm
    confirmation: NotRequired[str]  # human-readable confirmation message
