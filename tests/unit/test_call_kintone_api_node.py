# CMN-C2-276 - Unit tests: CallKintoneApiNode (inner Step 4, tool side-effect)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input
# gate -> execute -> output gate); this inner domain node ->
# caller_trust_level = TrustLevel.ANONYMOUS.value.
# The ONE documented exception: the config-override call passes a 2nd (config)
# argument, which __call__ cannot forward - that single test stays a DIRECT
# execute(state, config=...) call (an ANONYMOUS node, so the trust gate is
# unaffected).
#
# The node builds its client locally (SDK v1 nodes are no-arg), so error-path
# transports are exercised by monkeypatching the module's KintoneClient symbol
# (our own module attribute - never a sys.modules stub of shared.*).

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.nodes.call_kintone_api_node import CallKintoneApiNode
from src.services.kintone_client import KintoneApiError
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.call_kintone_api_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "kintone_payload": to_json({"app": "17", "id": "101"}),
        "intent": "lookup_record",
        "app_id": "17",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "call-kintone-test",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class _FakeErrorClient:
    """Stands in for KintoneClient: lookup raises the documented API error."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def get_record(self, app_id, record_id, api_token):
        raise KintoneApiError(403, "forbidden by API token permissions")


class _FakeLiveClient:
    """Stands in for KintoneClient with a LIVE (non-stub) transport."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = False

    def get_record(self, app_id, record_id, api_token):
        _FakeLiveClient.captured = {
            "app_id": app_id,
            "record_id": record_id,
            "api_token": api_token,
        }
        return {
            "record": {
                "$id": {"type": "__ID__", "value": record_id},
                "title": {"type": "SINGLE_LINE_TEXT", "value": f"Record {record_id}"},
            }
        }


class TestCallKintoneApiNode:
    def setup_method(self):
        self.node = CallKintoneApiNode()

    def test_lookup_success_via_default_v1_stub(self):
        # Default transport = deterministic, network-free v1 stub; no secret
        # provider bound -> the node runs on the documented stub placeholder.
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "101"
        assert result["record_ref"] == "kintone://app/17/records/101"
        assert result["app_id"] == "17"
        assert result["record_title"] == "Record 101"

    def test_create_success_via_default_v1_stub(self):
        state = _state(
            intent="create_record",
            kintone_payload=to_json({"app": "17", "record": {"title": {"value": "Onboarding"}}}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        # The stub receipt synthesizes the new record id deterministically.
        assert result["record_id"]
        assert result["record_ref"].startswith("kintone://app/17/records/")

    def test_update_success_via_default_v1_stub(self):
        state = _state(
            intent="update_record",
            kintone_payload=to_json({"app": "17", "id": "101", "record": {"Status": {"value": "closed"}}}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        # The stub echoes the request id so the caller can reference the record.
        assert result["record_id"] == "101"
        assert result["record_ref"] == "kintone://app/17/records/101"

    def test_kintone_config_state_field_sets_base_url(self):
        # The inner graph injects the manifest `kintone:` section as the JSON
        # kintone_config state field; the stub transport still serves the call.
        state = _state(kintone_config=to_json({"base_url": "https://kintone.example.test/k/v1"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_ref"] == "kintone://app/17/records/101"

    def test_config_override_direct_execute_call(self):
        # Documented canon exception: execute(state, config=...) takes a 2nd
        # argument that __call__ cannot forward, so this ONE test calls execute
        # directly (an ANONYMOUS node - the trust gate is not the subject here).
        config = {"configurable": {"kintone": {"base_url": "https://kintone.example.test/k/v1"}}}
        result = self.node.execute(_state(), config=config)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "101"

    def test_missing_payload_errors(self):
        result = self.node(_state(kintone_payload=None))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_unresolved_app_id_errors(self):
        state = _state(app_id="", kintone_payload=to_json({"app": "", "id": "101"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved kintone app id" in entry for entry in result["error_log"])

    def test_lookup_with_unresolved_record_number_errors(self):
        state = _state(kintone_payload=to_json({"app": "17", "id": ""}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved record number" in entry for entry in result["error_log"])

    def test_update_with_unresolved_record_number_errors(self):
        state = _state(
            intent="update_record",
            kintone_payload=to_json({"app": "17", "id": "", "record": {}}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_unknown_intent_errors(self):
        result = self.node(_state(intent="delete_record"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unknown intent" in entry for entry in result["error_log"])

    def test_api_error_surfaces_status_error(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_kintone_api_node.KintoneClient", _FakeErrorClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("403" in entry for entry in result["error_log"])

    def test_live_transport_without_secret_refuses_call(self, monkeypatch):
        # With a LIVE transport a missing KINTONE_API_TOKEN is a hard
        # error - a real API is never called unauthenticated.
        monkeypatch.setattr("src.nodes.call_kintone_api_node.KintoneClient", _FakeLiveClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unauthenticated" in entry for entry in result["error_log"])

    def test_live_transport_reads_token_from_ctx_secrets(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_kintone_api_node.KintoneClient", _FakeLiveClient)
        _FakeLiveClient.captured = {}
        with bound_secrets(InMemoryProvider({"KINTONE_API_TOKEN": "mock-token-for-testing"})):
            result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert _FakeLiveClient.captured["api_token"] == "mock-token-for-testing"
        assert _FakeLiveClient.captured["app_id"] == "17"
        assert _FakeLiveClient.captured["record_id"] == "101"

    def test_audit_emits_side_effect_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.call_kintone_api_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        payload = payloads["call_kintone_api_complete"]
        assert payload["intent"] == "lookup_record"
        assert payload["has_record_id"] is True
        assert payload["stub_transport"] is True


class TestErrorReasonsAreClosedSet:
    """Every error_log entry this node writes is a closed-set label.

    molt source review (wave-8 batch, 2026-09-04) - the second leak channel.
    `error_log` is the internal channel: post_process publishes a constant
    reason code and never projects the log to the caller. It still has to be
    closed-set - the audit trail reads it, a checkpoint keeps it, and the
    framework's own egress scan raises on credential-shaped text anywhere in a
    node result, which would replace the contained result with a bare error. A
    reason that echoes the upstream body is the worst case: a live tenant's
    error text is unbounded third-party content.

    The signal must still travel - the fixed phrase, the HTTP STATUS, the
    exception TYPE. It is the interpolated value that is removed, not the
    diagnosis.
    """

    APP_ID = "8842"
    RECORD_ID = "1001"

    def setup_method(self):
        self.node = CallKintoneApiNode()

    def _state_for(self, **overrides) -> dict:
        return _state(
            app_id=self.APP_ID,
            kintone_payload=to_json({"app": self.APP_ID, "id": self.RECORD_ID}),
            **overrides,
        )

    def test_no_matching_record_reason_omits_the_record_and_app_ids(self, monkeypatch):
        class _EmptyClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def get_record(self, app_id, record_id, api_token):
                return {"record": {}}

        monkeypatch.setattr("src.nodes.call_kintone_api_node.KintoneClient", _EmptyClient)
        result = self.node(self._state_for())
        assert result["status"] == AgentStatus.ERROR.value
        reasons = " ".join(result["error_log"])
        assert "no record found" in reasons, "the diagnosis must survive"
        assert self.RECORD_ID not in reasons, f"reason interpolated the record id: {reasons}"
        assert self.APP_ID not in reasons, f"reason interpolated the app id: {reasons}"

    def test_api_error_reason_carries_the_status_not_the_upstream_body(self, monkeypatch):
        class _LeakyErrorClient:
            """A tenant whose 403 body quotes the record it refused."""

            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def get_record(self, app_id, record_id, api_token):
                raise KintoneApiError(403, f"denied for 'Acme Trading K.K. renewal' record {record_id} in {app_id}")

        monkeypatch.setattr("src.nodes.call_kintone_api_node.KintoneClient", _LeakyErrorClient)
        result = self.node(self._state_for())
        assert result["status"] == AgentStatus.ERROR.value
        reasons = " ".join(result["error_log"])
        assert "403" in reasons, "the closed-set signal must still travel"
        assert "Acme Trading" not in reasons
        assert self.RECORD_ID not in reasons
        assert self.APP_ID not in reasons

    def test_transport_failure_reason_carries_the_exception_type_only(self, monkeypatch):
        class _BoomClient:
            """A transport error string carrying the URL, which carries the ids."""

            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def get_record(self, app_id, record_id, api_token):
                raise RuntimeError(f"GET https://x.cybozu.example/k/v1/record.json?app={app_id}&id={record_id} refused")

        monkeypatch.setattr("src.nodes.call_kintone_api_node.KintoneClient", _BoomClient)
        result = self.node(self._state_for())
        assert result["status"] == AgentStatus.ERROR.value
        reasons = " ".join(result["error_log"])
        assert "RuntimeError" in reasons, "the exception TYPE is the signal that must travel"
        assert self.RECORD_ID not in reasons
        assert self.APP_ID not in reasons
        assert "https://" not in reasons

    def test_control_the_clean_call_still_returns_record_evidence(self):
        """CONTROL. Without it the assertions above pass vacuously - a node
        that errored on everything, or wrote no reasons at all, would satisfy
        them."""
        result = self.node(self._state_for())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"]
        assert result["record_ref"].startswith(f"kintone://app/{self.APP_ID}/records/")
