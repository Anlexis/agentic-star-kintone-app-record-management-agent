# CMN-C2-276 - Unit tests: ConfirmNode (inner Step 5)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input
# gate -> execute -> output gate); this inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.confirm_node import ConfirmNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.confirm_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "record_id": "101",
        "record_ref": "kintone://app/17/records/101",
        "record_title": "Record 101",
        "intent": "lookup_record",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "confirm-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestConfirmNode:
    def setup_method(self):
        self.node = ConfirmNode()

    def test_lookup_confirmation(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Retrieved kintone record" in result["confirmation"]
        assert "Record 101" in result["confirmation"]
        assert "ref=kintone://app/17/records/101" in result["confirmation"]
        assert "id=101" in result["confirmation"]
        assert result["result"]["record_id"] == "101"
        assert result["result"]["record_ref"] == "kintone://app/17/records/101"

    def test_create_verb(self):
        result = self.node(
            _state(
                intent="create_record",
                record_id="202",
                record_ref="kintone://app/17/records/202",
                record_title="Onboarding",
            )
        )
        assert "Created kintone record" in result["confirmation"]

    def test_update_verb(self):
        result = self.node(_state(intent="update_record"))
        assert "Updated kintone record" in result["confirmation"]

    def test_unknown_intent_uses_generic_verb(self):
        result = self.node(_state(intent="mystery"))
        assert "Processed kintone record" in result["confirmation"]

    def test_id_only_no_ref(self):
        result = self.node(_state(record_ref=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "id=101" in result["confirmation"]
        assert "ref=" not in result["confirmation"]

    def test_falls_back_to_record_id_when_title_missing(self):
        result = self.node(_state(record_title=""))
        assert "'101'" in result["confirmation"]

    def test_missing_record_evidence_errors(self):
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_confirmation_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.confirm_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - intent + presence only.
        assert payloads["confirm_complete"]["intent"] == "lookup_record"
        assert payloads["confirm_complete"]["has_record_ref"] is True
