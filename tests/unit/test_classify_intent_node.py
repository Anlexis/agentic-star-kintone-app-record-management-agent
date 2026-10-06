# CMN-C2-276 - Unit tests: ClassifyIntentNode (inner Step 2)
# Intents: lookup_record / create_record / update_record (deterministic
# keyword heuristic, no model; unknown falls back to the read-only lookup).
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input
# gate -> execute -> output gate); this inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.classify_intent_node import ClassifyIntentNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.classify_intent_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str) -> dict:
    return {
        "validated_input": text,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "classify-intent-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }


class TestClassifyIntentNode:
    def setup_method(self):
        self.node = ClassifyIntentNode()

    def test_keyword_lookup_record(self):
        result = self.node(_state("Look up record 101 in kintone app 17 and summarize what is on file."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_record"

    def test_keyword_create_record(self):
        result = self.node(_state("Register a new record in app 17 with the intake details"))
        assert result["intent"] == "create_record"

    def test_keyword_update_record(self):
        result = self.node(_state("Update record 101 in app 17 with the revised status"))
        assert result["intent"] == "update_record"

    def test_write_keyword_wins_over_lookup(self):
        # Priority order is writes-first: an "update ... then show it" style
        # request classifies as the write, never the read.
        result = self.node(_state("Update record 101 in app 17 and show the record"))
        assert result["intent"] == "update_record"

    def test_no_signal_defaults_to_readonly_lookup(self):
        result = self.node(_state("please handle this for the team"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_record"
        # Non-fatal low-confidence note travels in error_log; status stays SUCCESS.
        assert any("defaulted to lookup_record" in entry for entry in result.get("error_log", []))

    def test_empty_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_intent_label_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.classify_intent_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state("Look up record 101 in kintone app 17 and summarize what is on file."))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - the label, never the text.
        assert payloads["classify_intent_complete"]["intent"] == "lookup_record"
        assert payloads["classify_intent_complete"]["defaulted"] is False
