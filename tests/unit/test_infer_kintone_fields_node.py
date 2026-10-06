# CMN-C2-276 - Unit tests: InferKintoneFieldsNode (inner Step 3)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input
# gate -> execute -> output gate); this inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value.
# Positive payloads are PII-free: the framework input mask rewrites Title-Case
# bigrams in validated_input, so quoted record titles use a single word and
# "Key: value" record-field values stay lower-case.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.infer_kintone_fields_node import InferKintoneFieldsNode
from src.schemas.state import from_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.infer_kintone_fields_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str, intent: str = "lookup_record", app_hint: str = "", record_hint: str = "", **overrides) -> dict:
    state = {
        "validated_input": text,
        "intent": intent,
        "app_hint": app_hint,
        "record_hint": record_hint,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "infer-fields-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestInferKintoneFieldsNode:
    def setup_method(self):
        self.node = InferKintoneFieldsNode()

    def test_lookup_extracts_ids_from_text(self):
        result = self.node(_state("Look up record 101 in kintone app 17 and summarize what is on file."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["app_id"] == "17"
        # kintone_payload is stored as a JSON string, not a native dict.
        assert isinstance(result["kintone_payload"], str)
        assert from_json(result["kintone_payload"], {}) == {"app": "17", "id": "101"}

    def test_id_shaped_hints_used_when_text_has_no_ids(self):
        result = self.node(_state("Summarize the record on file", app_hint="17", record_hint="101"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["app_id"] == "17"
        assert from_json(result["kintone_payload"], {}) == {"app": "17", "id": "101"}

    def test_record_number_resolved_from_kv_field(self):
        # "id: 101" resolves via the "Key: value" fields path (the text regex
        # requires an explicit "record ..." mention); app comes from the hint.
        result = self.node(_state("Summarize what is on file\nid: 101", app_hint="17"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert from_json(result["kintone_payload"], {}) == {"app": "17", "id": "101"}

    def test_create_builds_record_body(self):
        # Field values stay lower-case: the framework name mask rewrites
        # Title-Case word pairs even ACROSS newlines before execute() sees the text.
        text = 'Create a record titled "Onboarding" in app 17\nDepartment: sales\nPriority: high'
        result = self.node(_state(text, intent="create_record"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["app_id"] == "17"
        assert result["record_title"] == "Onboarding"
        payload = from_json(result["kintone_payload"], {})
        assert payload["app"] == "17"
        assert "id" not in payload
        record = payload["record"]
        # kintone record body shape: {field_code: {"value": ...}}.
        assert record["title"] == {"value": "Onboarding"}
        assert record["Department"] == {"value": "sales"}
        assert record["Priority"] == {"value": "high"}

    def test_update_builds_record_body_with_id(self):
        text = "Update record 101 in app 17\nStatus: closed"
        result = self.node(_state(text, intent="update_record"))
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = from_json(result["kintone_payload"], {})
        assert payload["app"] == "17"
        assert payload["id"] == "101"
        assert payload["record"]["Status"] == {"value": "closed"}

    def test_id_like_keys_excluded_from_record_body(self):
        # App / record / title keys are the target address, never record fields.
        text = 'Create a record titled "Kickoff"\napp: 17\nOwner: ops'
        result = self.node(_state(text, intent="create_record"))
        payload = from_json(result["kintone_payload"], {})
        record = payload["record"]
        assert "app" not in record
        assert record["Owner"] == {"value": "ops"}

    def test_unresolved_ids_left_empty_never_invented(self):
        result = self.node(_state("Summarize the flagged item on file"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["app_id"] == ""
        assert from_json(result["kintone_payload"], {}) == {"app": "", "id": ""}

    def test_non_id_shaped_hint_left_unresolved(self):
        result = self.node(_state("Summarize the flagged item on file", app_hint="seventeen!"))
        assert result["app_id"] == ""

    def test_missing_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_field_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.infer_kintone_fields_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state("Look up record 101 in kintone app 17 and summarize what is on file."))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - signals only, never content.
        payload = payloads["infer_kintone_fields_complete"]
        assert payload["intent"] == "lookup_record"
        assert payload["has_app_id"] is True
        assert payload["has_record_no"] is True
        assert payload["llm_enhanced"] is False
        assert "text" not in payload


# -- Step 8e: optional LLM enhancement (record_title / custom record fields) --
# app_id / record_no are asserted UNCHANGED across every case below - the LLM
# never touches them, in every case (see the node's own risk-mitigation note).


class _FakeLLM:
    """Test-double matching AzureOpenAIClient.complete()'s canonical shape:
    complete(messages) -> {"content": <str>, ...}. Never a real network call."""

    def __init__(self, content: str | None = None, raises: Exception | None = None):
        self._content = content
        self._raises = raises
        self.calls: list[list[dict]] = []

    def complete(self, messages: list[dict]) -> dict:
        self.calls.append(messages)
        if self._raises is not None:
            raise self._raises
        return {"content": self._content}


class TestInferKintoneFieldsNodeLLMEnhancement:
    def test_well_formed_llm_response_overrides_the_heuristic(self):
        # "priority: high" is not a literal "Key: value" line the regex parser
        # catches (natural-language phrasing) - only the LLM path finds it.
        llm = _FakeLLM(content='{"record_title": "Kickoff Meeting", "fields": {"priority": "high"}}')
        node = InferKintoneFieldsNode(llm=llm)
        text = "Create a record in app 17 - set the priority to high please"
        result = node(_state(text, intent="create_record"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["app_id"] == "17"  # unaffected by the LLM - regex-only
        assert result["record_title"] == "Kickoff Meeting"
        record = from_json(result["kintone_payload"], {})["record"]
        assert record["priority"] == {"value": "high"}
        assert len(llm.calls) == 1

    def test_prose_wrapped_response_still_parses(self):
        # extract_json_object() harvests the {...} span even wrapped in prose
        # or a markdown fence.
        llm = _FakeLLM(content='Sure, here it is:\n```json\n{"record_title": null, "fields": {"status": "done"}}\n```')
        node = InferKintoneFieldsNode(llm=llm)
        result = node(_state("Update record 101 in app 17 - mark it done", intent="update_record"))
        assert result["status"] == AgentStatus.SUCCESS.value
        record = from_json(result["kintone_payload"], {})["record"]
        assert record["status"] == {"value": "done"}

    def test_malformed_response_falls_back_to_heuristic(self):
        llm = _FakeLLM(content="not valid json at all")
        node = InferKintoneFieldsNode(llm=llm)
        text = 'Create a record titled "Onboarding" in app 17\nDepartment: sales'
        result = node(_state(text, intent="create_record"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_title"] == "Onboarding"
        record = from_json(result["kintone_payload"], {})["record"]
        assert record["Department"] == {"value": "sales"}

    def test_wrong_shape_response_falls_back_to_heuristic(self):
        # "fields" must be an object, not a list - reject the whole response.
        llm = _FakeLLM(content='{"record_title": "X", "fields": ["not", "a", "dict"]}')
        node = InferKintoneFieldsNode(llm=llm)
        text = 'Create a record titled "Onboarding" in app 17\nDepartment: sales'
        result = node(_state(text, intent="create_record"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_title"] == "Onboarding"  # heuristic value, not "X"
        record = from_json(result["kintone_payload"], {})["record"]
        assert record["Department"] == {"value": "sales"}

    def test_llm_raising_falls_back_to_heuristic(self):
        llm = _FakeLLM(raises=RuntimeError("upstream API error"))
        node = InferKintoneFieldsNode(llm=llm)
        text = "Look up record 101 in kintone app 17 and summarize what is on file."
        result = node(_state(text))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["app_id"] == "17"
        assert from_json(result["kintone_payload"], {}) == {"app": "17", "id": "101"}

    def test_no_llm_injected_and_no_secret_bound_falls_back_to_heuristic(self):
        # Real production shape in any environment without a configured Azure
        # key: default construction (no llm=), resolve_llm() cannot build a
        # real client (NullProvider / missing lifecycle fields on this
        # fixture), so it returns None and the regex baseline is used as-is.
        node = InferKintoneFieldsNode()
        text = "Look up record 101 in kintone app 17 and summarize what is on file."
        result = node(_state(text))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert from_json(result["kintone_payload"], {}) == {"app": "17", "id": "101"}

    def test_empty_input_never_calls_the_llm(self):
        llm = _FakeLLM(content='{"record_title": "should not be used", "fields": {}}')
        node = InferKintoneFieldsNode(llm=llm)
        result = node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert not llm.calls

    def test_audit_flags_llm_enhancement_when_it_contributes(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.infer_kintone_fields_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        llm = _FakeLLM(content='{"record_title": null, "fields": {"priority": "high"}}')
        node = InferKintoneFieldsNode(llm=llm)
        node(_state("Create a record in app 17 - set the priority to high", intent="create_record"))
        payloads = {args[0]: args[1] for args in events}
        assert payloads["infer_kintone_fields_complete"]["llm_enhanced"] is True
