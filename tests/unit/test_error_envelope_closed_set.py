# CMN-C2-276 - Unit tests: the caller-visible ERROR envelope is closed-set.
#
# molt source review (Wave 9 CMN family, 2026-09-04): an ERROR return that
# clears every output-bearing field can still publish state["error_log"] under
# formatted_output. That log is node-authored text - and wherever a node
# interpolates an exception, upstream response text - so truncation, path
# stripping or credential-only redaction is not a closed-set error contract.
#
# Three properties, each over EVERY non-success path of PostProcessNode:
#   1. the envelope is {"reason": <one of ERROR_REASONS>} and nothing else;
#   2. a sentinel seeded into error_log and every output-bearing field appears
#      nowhere in the returned mapping - nested keys and values walked;
#   3. the envelope stays truthy (AgentBaseGraph.get_output() projects
#      `formatted_output or result` with no status check).
# The same delta is then pushed through the outer agent's get_output() - the
# caller's last hop - and the two refusal lines built from a runtime value
# (a caller field, an intent label) are pinned to their closed sets.
#
# Paths are driven through execute() - the node's own contract, and the only
# way an incoming ERROR reaches it (BaseNode.__call__ short-circuits on one) -
# and, where the framework wrapper allows, through node(state) as well.

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import KintoneAppRecordAgent
from src.nodes.call_kintone_api_node import CallKintoneApiNode
from src.nodes.post_process_node import (
    _REASON_OUTPUT_WITHHELD,
    _REASON_WORKFLOW_FAILED,
    ERROR_REASONS,
    PostProcessNode,
)
from src.nodes.pre_process_node import CALLER_FIELD_CODES, PreProcessNode
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)
    monkeypatch.setattr("src.nodes.pre_process_node.emit_trace_event", lambda *a, **k: None)
    monkeypatch.setattr("src.nodes.call_kintone_api_node.emit_trace_event", lambda *a, **k: None)


# What an upstream error body is most likely to quote: a person, an address, a
# token-shaped fragment. Assembled at runtime so no credential-shaped literal
# is committed, and the fragment is kept below every detector's floor on
# purpose: the framework's own egress scan must not be the thing that stops a
# republished log line - that would mask a regression by raising first.
_SENTINEL_PARTS = ("A. Tanaka", "a.tanaka@example.com", "sk-live-" + "x" * 3)
_SENTINEL = (
    "boom: upstream said {'customer':'"
    + _SENTINEL_PARTS[0]
    + "','email':'"
    + _SENTINEL_PARTS[1]
    + "','token':'"
    + _SENTINEL_PARTS[2]
    + "'}"
)

_OUTPUT_BEARING = (
    "result",
    "confirmation",
    "kintone_payload",
    "record_title",
    "record_ref",
    "record_id",
    "app_id",
    "intent",
)


def _bearer(fill: str, n: int = 24) -> str:
    """A bearer-shaped string, assembled at runtime (never a literal)."""
    return "Bearer " + fill * n


def _state(**overrides: Any) -> "dict[str, Any]":
    """A SUCCESS state with the sentinel in error_log AND in every output-bearing field."""
    state: dict[str, Any] = {
        "status": AgentStatus.SUCCESS.value,
        "record_id": "101",
        "record_ref": "kintone://app/17/records/101",
        "app_id": "17",
        "record_title": _SENTINEL,
        "intent": "lookup_record",
        "confirmation": _SENTINEL,
        "kintone_payload": to_json({"app": "17", "id": "101", "record": {"note": {"value": _SENTINEL}}}),
        "result": {"record_id": "101", "record_ref": "kintone://app/17/records/101", "confirmation": _SENTINEL},
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "error-envelope-test",
        "node_history": [],
        "error_log": [_SENTINEL],
        "execution_time": {},
    }
    state.update(overrides)
    return state


@dataclass(frozen=True)
class _Path:
    name: str
    reason: str
    build: Callable[[], "dict[str, Any]"]
    through_call: bool  # reachable through BaseNode.__call__ as well as execute()


_PATHS = (
    _Path("existing_error", _REASON_WORKFLOW_FAILED, lambda: _state(status=AgentStatus.ERROR.value), False),
    _Path(
        "existing_error_credential_in_log",
        _REASON_WORKFLOW_FAILED,
        lambda: _state(status=AgentStatus.ERROR.value, error_log=[_SENTINEL, "upstream said: " + _bearer("d")]),
        False,
    ),
    _Path("gate_missing_record_evidence", _REASON_OUTPUT_WITHHELD, lambda: _state(record_id="", record_ref=""), True),
    _Path(
        "gate_credential_value_nested",
        _REASON_OUTPUT_WITHHELD,
        lambda: _state(kintone_payload=to_json({"app": "17", "record": {"note": {"value": _bearer("b")}}})),
        True,
    ),
    _Path(
        "gate_credential_key_nested",
        _REASON_OUTPUT_WITHHELD,
        lambda: _state(kintone_payload=to_json({"app": "17", "record": {_bearer("k"): {"value": _bearer("v")}}})),
        True,
    ),
)


def _cases():
    for path in _PATHS:
        yield pytest.param(path, "execute", id=f"{path.name}-execute")
        if path.through_call:
            yield pytest.param(path, "call", id=f"{path.name}-call")


def _run(path: _Path, via: str) -> "dict[str, Any]":
    node = PostProcessNode()
    state = path.build()
    return node(state) if via == "call" else node.execute(state)


def _strings(value: object, where: str = "$"):
    """Every string in a mapping - keys and values - however deeply nested."""
    if isinstance(value, str):
        yield where, value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield f"{where}(key)", key
            yield from _strings(item, f"{where}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _strings(item, f"{where}[{index}]")


class TestEnvelopeIsClosedSet:
    @pytest.mark.parametrize("path,via", _cases())
    def test_envelope_is_the_reason_code_and_nothing_else(self, path, via):
        result = _run(path, via)
        assert result["status"] == AgentStatus.ERROR.value
        envelope = result["formatted_output"]
        assert envelope, "a falsy envelope re-opens the `formatted_output or result` fallback"
        assert envelope == {"reason": path.reason}
        assert envelope["reason"] in ERROR_REASONS

    @pytest.mark.parametrize("path,via", _cases())
    def test_every_envelope_value_is_a_declared_constant(self, path, via):
        envelope = _run(path, via)["formatted_output"]
        assert all(isinstance(value, str) and value in ERROR_REASONS for value in envelope.values())

    @pytest.mark.parametrize("path,via", _cases())
    def test_sentinel_appears_nowhere_in_the_returned_mapping(self, path, via):
        result = _run(path, via)
        for where, text in _strings(result):
            for part in (_SENTINEL, *_SENTINEL_PARTS):
                assert part not in text, f"{part!r} reached {where}"
        assert _SENTINEL not in json.dumps(result, default=str, ensure_ascii=False)

    @pytest.mark.parametrize("path,via", _cases())
    def test_error_log_is_not_projected_under_any_key(self, path, via):
        result = _run(path, via)
        assert "error" not in result["formatted_output"]
        if path.reason == _REASON_WORKFLOW_FAILED:
            # The inner entries are already in error_log (the reducer appends);
            # re-emitting them would duplicate every line, and none may travel.
            assert "error_log" not in result
        else:
            labels = result["error_log"]
            assert labels and all(label.startswith("PostProcess output gate:") for label in labels)

    @pytest.mark.parametrize("path,via", _cases())
    def test_every_output_bearing_field_is_cleared(self, path, via):
        result = _run(path, via)
        retained = [field for field in _OUTPUT_BEARING if field not in result or result[field]]
        assert not retained, f"output-bearing state not cleared: {retained}"


class TestViolationLabels:
    """Gate violations name a PATH and go to error_log only - and the label
    itself must be safe to carry: the framework's own credential scan raises on
    a credential-shaped string anywhere in the node result and would replace
    the cleared result with a bare error, re-opening the fallback."""

    def test_credential_shaped_key_is_withheld_from_the_path_label(self):
        key, value = _bearer("k"), _bearer("v")
        result = PostProcessNode().execute(
            _state(kintone_payload=to_json({"app": "17", "record": {key: {"value": value}}}))
        )
        rendered = json.dumps(result, default=str)
        assert "k" * 24 not in rendered, "the mapping key was quoted into a label"
        assert "v" * 24 not in rendered, "the value was quoted into a label"
        assert any("<withheld>" in label for label in result["error_log"])
        assert any("(key)" in label for label in result["error_log"])

    def test_an_ordinary_key_is_still_named(self):
        """CONTROL: withholding is for credential-shaped keys only - the path
        of an ordinary field stays diagnosable."""
        result = PostProcessNode().execute(
            _state(kintone_payload=to_json({"app": "17", "record": {"note": {"value": _bearer("b")}}}))
        )
        assert any("['record']['note']['value']" in label for label in result["error_log"])


@pytest.fixture(scope="module")
def agent() -> KintoneAppRecordAgent:
    return KintoneAppRecordAgent()


class TestInvokeEnvelope:
    """The caller's last hop: AgentBaseGraph.get_output() on the merged state."""

    @pytest.mark.parametrize("path,via", _cases())
    def test_invoke_body_carries_the_reason_only(self, agent, path, via):
        state = path.build()
        delta = _run(path, via)
        merged = {**state, **delta}
        # The reducer APPENDS error_log - model the worst case, every line kept.
        merged["error_log"] = list(state["error_log"]) + list(delta.get("error_log", []))
        body = agent.get_output(merged)
        assert body["status"] == AgentStatus.ERROR.value
        assert body["output"] == {"reason": path.reason}
        assert "error_log" not in body
        rendered = json.dumps(body, default=str, ensure_ascii=False)
        for part in (_SENTINEL, *_SENTINEL_PARTS):
            assert part not in rendered


# --- the two refusal lines built from a runtime value -------------------------

_REFUSAL_RE = re.compile(r"^PreProcessNode: rejected caller input - '(?P<field>[a-z_]+)' (?P<code>.+)$")
_CONTRACT_FIELDS = ("app_id", "app", "app_hint", "record_id", "record_no", "record_hint")
_MARKER = "wontbeechoed" + "z" * 12
_BAD_VALUES = [_MARKER, "", "12-34", 0, 1_000_000_000, True, 1.5, ["17"], {"v": "17"}]


def _pre_state(**overrides: Any) -> "dict[str, Any]":
    state: dict[str, Any] = {
        "user_input": "Look up record 101 in kintone app 17.",
        "input_context": {},
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "correlation_id": "error-envelope-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


def _api_state(**overrides: Any) -> "dict[str, Any]":
    state: dict[str, Any] = {
        "kintone_payload": to_json({"app": "17", "id": "101"}),
        "intent": "lookup_record",
        "app_id": "17",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "error-envelope-test",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestRefusalLinesAreClosedSet:
    @pytest.mark.parametrize("bad", _BAD_VALUES, ids=repr)
    @pytest.mark.parametrize("field", _CONTRACT_FIELDS)
    def test_caller_field_refusal_is_a_contract_field_plus_a_fixed_code(self, field, bad):
        result = PreProcessNode().execute(_pre_state(input_context={field: bad}))
        assert result["status"] == AgentStatus.ERROR.value
        (line,) = result["error_log"]
        match = _REFUSAL_RE.match(line)
        assert match, line
        assert match["field"] == field
        assert match["code"] in CALLER_FIELD_CODES
        assert _MARKER not in line

    def test_non_mapping_context_refusal_is_closed_set(self):
        result = PreProcessNode().execute(_pre_state(input_context="app_id=17"))
        assert result["status"] == AgentStatus.ERROR.value
        (line,) = result["error_log"]
        match = _REFUSAL_RE.match(line)
        assert match, line
        assert match["field"] == "input_context"
        assert match["code"] in CALLER_FIELD_CODES

    def test_unknown_intent_reason_does_not_echo_the_intent_value(self):
        marker = "delete_record_" + "q" * 12
        result = CallKintoneApiNode().execute(_api_state(intent=marker))
        assert result["status"] == AgentStatus.ERROR.value
        reasons = " ".join(result["error_log"])
        assert "unknown intent" in reasons, "the diagnosis must survive"
        assert marker not in reasons, f"reason interpolated the intent value: {reasons}"
