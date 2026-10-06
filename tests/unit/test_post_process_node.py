# CMN-C2-276 - Unit tests: PostProcessNode (outer backbone, domain output gate)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input gate
# -> execute -> output gate); this backbone formatter declares ANONYMOUS -> the
# state builder sets caller_trust_level = TrustLevel.ANONYMOUS.value. The domain
# gate is the MODULE-LEVEL _security_gate_output() helper (the framework gate
# methods are @final and the SDK auto-wraps _extra_ hooks), so the helper is
# also unit-tested directly as a plain function.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import PostProcessNode, _security_gate_output
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "record_id": "101",
        "record_ref": "kintone://app/17/records/101",
        "record_title": "Record 101",
        "intent": "lookup_record",
        "confirmation": "Retrieved kintone record 'Record 101' - ref=kintone://app/17/records/101 - id=101",
        "kintone_payload": to_json({"app": "17", "id": "101"}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "post-process-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestPostProcessNode:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_success_formats_output(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        # Regression guard: State carries the enum .value STRING, never the
        # bare AgentStatus enum member.
        assert not isinstance(result["status"], AgentStatus)
        out = result["formatted_output"]
        assert out["record_id"] == "101"
        assert out["record_ref"] == "kintone://app/17/records/101"
        assert out["intent"] == "lookup_record"
        assert out["confirmation"].startswith("Retrieved kintone record")
        # Round-trip: the JSON kintone_payload string surfaces parsed.
        assert out["kintone_payload"] == {"app": "17", "id": "101"}

    def test_error_status_preserved(self):
        """Inner-workflow error must not be masked as success. Real-SDK
        pipeline behavior: BaseNode.__call__ short-circuits on an incoming
        errored state (execute() is skipped), so the error status + error_log
        pass through untouched and no success shape is fabricated."""
        state = _state(
            status=AgentStatus.ERROR.value,
            record_id="",
            record_ref="",
            error_log=["CallKintoneApiNode: kintone API error 403: forbidden"],
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "kintone API error 403" in "\n".join(result["error_log"])
        assert "formatted_output" not in result

    def test_error_status_as_string_value_preserved(self):
        """The framework may carry status as the enum .value (string) at the boundary."""
        result = self.node(_state(status=AgentStatus.ERROR.value, error_log=["boom"]))
        assert result["status"] == AgentStatus.ERROR.value
        assert "formatted_output" not in result

    def test_gate_blocks_success_without_record_evidence(self):
        """Full node path: a SUCCESS output missing record_id/record_ref is blocked."""
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("output gate" in entry for entry in result["error_log"])


class TestContainmentOnViolation:
    """A violating gate must CLEAR the output-bearing fields.

    Raising, or returning an error status while leaving them set, is not
    containment: the response envelope falls back to state["result"] even on an
    error status, so the blocked content would still ship inside the error
    envelope."""

    def setup_method(self):
        self.node = PostProcessNode()

    def _blocked_state(self):
        bearer_like = "Bearer " + "a" * 24
        return _state(
            record_title=bearer_like,
            result={"record_id": "101", "confirmation": bearer_like},
            confirmation=bearer_like,
        )

    def test_every_output_bearing_field_is_cleared(self):
        result = self.node(self._blocked_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert result["result"] is None
        assert result["confirmation"] == ""
        assert result["kintone_payload"] is None
        assert result["record_title"] == ""
        assert result["record_ref"] == ""
        assert result["record_id"] == ""
        retained = [f for f in ("app_id", "intent") if f not in result or result[f]]
        assert not retained, f"output-bearing state not cleared on the gate path: {retained}"
        # The replacement envelope is the record-free withheld notice - TRUTHY
        # on purpose, so the `formatted_output or result` projection stops here
        # rather than falling back onto whatever survived in state.
        assert result["formatted_output"]
        assert result["formatted_output"].get("reason") == "output_withheld_by_gate"
        assert "record_id" not in result["formatted_output"]

    def test_the_blocked_value_never_appears_in_the_returned_delta(self):
        import json as _json

        result = self.node(self._blocked_state())
        assert "a" * 24 not in _json.dumps(result, default=str)

    def test_nested_credential_is_caught(self):
        """kintone_payload is a nested mapping - a scan that only looked at
        top-level strings would walk straight past a credential in a record
        field."""
        bearer_like = "Bearer " + "b" * 24
        state = _state(kintone_payload=to_json({"app": "17", "record": {"note": {"value": bearer_like}}}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"].get("reason") == "output_withheld_by_gate"
        assert bearer_like not in str(result["formatted_output"])

    def test_credential_in_a_nested_key_is_caught(self):
        bearer_like = "Bearer " + "c" * 24
        state = _state(kintone_payload=to_json({"app": "17", "record": {bearer_like: {"value": "x"}}}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_credential_in_the_inner_error_log_is_not_republished(self):
        """An inner error whose error_log carries credential-shaped text: the
        envelope is the reason code only and the log line goes no further - it
        is neither projected under another key nor re-emitted."""
        import json as _json

        bearer_like = "Bearer " + "d" * 24
        state = _state(status=AgentStatus.ERROR.value, error_log=[f"upstream said: {bearer_like}"])
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert result["result"] is None
        assert result["formatted_output"] == {"reason": "kintone_workflow_failed"}
        assert "error_log" not in result
        assert bearer_like not in _json.dumps(result, default=str)


class TestSecurityGateOutputHelper:
    """The module-level domain gate as a plain function (not a node call)."""

    def test_passes_success_with_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "101", "record_ref": "kintone://app/17/records/101", "confirmation": "ok"},
            is_success=True,
        )
        assert violations == []

    def test_blocks_success_without_record_evidence(self):
        violations = _security_gate_output(
            {"record_id": "", "record_ref": "", "confirmation": "looks done"},
            is_success=True,
        )
        assert len(violations) == 1
        assert "record_id/record_ref" in violations[0]

    def test_blocks_credential_shaped_value(self):
        # Built at runtime so no credential-shaped literal is committed.
        bearer_like = "Bearer " + "a" * 24
        violations = _security_gate_output(
            {"record_id": "101", "note": bearer_like},
            is_success=True,
        )
        assert any("note" in v for v in violations)

    # Assembled at runtime so no credential-shaped literal is committed - the
    # repository's own credential scan would (correctly) flag one.
    @pytest.mark.parametrize(
        "shape",
        [
            "Bearer " + "a" * 24,
            "sk-" + "b" * 24,
            "eyJ" + "c" * 20,
            "sk_live_" + "d" * 20,
            "AKIA" + "E" * 16,
            "postgresql://svc" + ":" + "pw" + "@db.internal:5432/records",
        ],
    )
    def test_every_credential_family_is_recognised(self, shape):
        """The stated invariant is "no credential material", so the gate has to
        recognise every family the platform's own scan does - otherwise a value
        it catches and this one misses is raised past the clearing and ships
        inside the error envelope."""
        violations = _security_gate_output({"record_id": "101", "note": shape}, is_success=True)
        assert violations, f"missed {shape!r}"

    def test_error_output_not_required_to_carry_evidence(self):
        violations = _security_gate_output({"record_id": "", "record_ref": ""}, is_success=False)
        assert violations == []


class TestExistingErrorPathContainment:
    """The pre-existing-ERROR branch must contain, not re-publish.

    molt source review (wave-8 batch, 2026-09-04): "the success-gate violation
    path has containment, but the independent existing-ERROR branch rebuilds a
    truthy response with record_id / record_ref and does not clear the merged
    output-bearing state."

    `record_id` / `record_ref` are the kintone WRITE EVIDENCE - the success
    branch of this node's own gate REFUSES a SUCCESS that lacks them. Returning
    them in an envelope whose status is ERROR tells a caller being informed of
    failure that a record was nonetheless touched, and which one.

    Three separate properties are asserted, because fixing one leaves the
    others open:
      1. the shipped envelope carries no record evidence, AND stays TRUTHY - a
         falsy formatted_output re-opens the framework's
         `formatted_output or result` projection (get_output(), no status
         check) onto whatever survived in state;
      2. the returned delta CLEARS the output-bearing state fields, so a
         checkpoint or a downstream reader cannot pick them up either;
      3. error_log is NOT projected into the envelope under any key and is not
         re-emitted - the caller receives a constant reason code only, so a
         log line that interpolated an id or echoed an upstream response body
         could not put back what the envelope omits.

    Reachability, stated honestly: in the compiled graph AgentBaseGraph.route()
    sends an errored state to `finalize` (bypassing post_process) and
    BaseNode.__call__ short-circuits on an incoming errored state before
    execute() runs, so this branch is source-level defence in depth, reachable
    by a direct execute(). It is not a live end-to-end leak - and it is still
    the shape a future route change or a direct caller would ship.
    """

    _RECORD_ID = "1001"
    _APP_ID = "8842"
    _RECORD_REF = "kintone://app/8842/records/1001"
    _TITLE = "Acme Trading K.K. renewal"

    # Every output-bearing State field: the envelope composes them, and
    # downstream formatting / the checkpoint read them.
    _OUTPUT_BEARING = (
        "result",
        "confirmation",
        "kintone_payload",
        "record_title",
        "record_id",
        "record_ref",
        "app_id",
        "intent",
    )

    def _errored_state(self, error_log=None) -> dict:
        """An error raised AFTER the kintone call resolved a record - the
        realistic shape (call succeeded, a later step failed), and the only
        shape in which record evidence is present on an error at all."""
        return _state(
            status=AgentStatus.ERROR.value,
            record_id=self._RECORD_ID,
            record_ref=self._RECORD_REF,
            app_id=self._APP_ID,
            record_title=self._TITLE,
            confirmation=f"Retrieved kintone record '{self._TITLE}' - ref={self._RECORD_REF}",
            kintone_payload=to_json({"app": self._APP_ID, "id": self._RECORD_ID}),
            result={
                "record_id": self._RECORD_ID,
                "record_ref": self._RECORD_REF,
                "record_title": self._TITLE,
            },
            error_log=error_log or ["ConfirmNode: downstream failure after the kintone call"],
        )

    def test_error_envelope_is_present_and_truthy(self):
        """Containment must not be achieved by emptying the envelope: the
        framework projects `formatted_output or result` with NO status check,
        so a falsy value hands the caller `result` instead."""
        result = PostProcessNode().execute(self._errored_state())
        assert "formatted_output" in result, "error path must ship an envelope"
        assert result["formatted_output"], (
            "error envelope must be TRUTHY - a falsy one re-opens the "
            "`formatted_output or result` fallback in AgentBaseGraph.get_output()"
        )

    def test_error_envelope_carries_no_record_evidence(self):
        """The leak itself: no record identifier or record content may ride the
        failure envelope back to the caller."""
        import json

        shipped = json.dumps(
            PostProcessNode().execute(self._errored_state())["formatted_output"],
            default=str,
            ensure_ascii=False,
        )
        leaked = [
            field
            for field, value in (
                ("record_id", self._RECORD_ID),
                ("record_ref", self._RECORD_REF),
                ("app_id", self._APP_ID),
                ("record_title", self._TITLE),
            )
            if value in shipped
        ]
        assert not leaked, f"error envelope leaked kintone record evidence: {leaked}"

    def test_error_return_clears_output_bearing_state(self):
        """ "Does not clear the merged output-bearing state": omitting a field
        from ONE envelope is not clearing it. The delta must blank every
        output-bearing field so no checkpoint or downstream reader recovers
        it."""
        result = PostProcessNode().execute(self._errored_state())
        retained = [field for field in self._OUTPUT_BEARING if field not in result or result[field]]
        assert not retained, f"output-bearing state not cleared on the error path: {retained}"

    def test_error_log_is_not_projected_into_the_envelope(self):
        """The envelope is the reason code and nothing else. error_log stays the
        internal channel - the reducer appends to it and the audit trail reads
        it - and is neither copied under another key nor re-emitted. So a log
        line that interpolated the record, or echoed an upstream body, still
        could not reach the caller through this node. (The producing nodes keep
        their reasons closed-set regardless - see TestErrorReasonsAreClosedSet
        in test_call_kintone_api_node.py - because the audit trail and the
        framework's own egress scan read the log.)
        """
        import json

        reasons = [
            f"CallKintoneApiNode: no record found for id {self._RECORD_ID} in app {self._APP_ID}",
            f"CallKintoneApiNode: kintone API error 403: denied for {self._TITLE}",
        ]
        result = PostProcessNode().execute(self._errored_state(error_log=reasons))
        assert result["formatted_output"] == {"reason": "kintone_workflow_failed"}
        assert "error_log" not in result, "the inner entries are already in error_log; re-emitting duplicates them"
        rendered = json.dumps(result, default=str, ensure_ascii=False)
        for reason in reasons:
            assert reason not in rendered

    def test_error_status_is_still_reported(self):
        """Containment must not mask the failure."""
        assert PostProcessNode().execute(self._errored_state())["status"] == AgentStatus.ERROR.value

    def test_clean_path_control_still_returns_the_answer(self):
        """CONTROL. Without it every containment assertion above passes
        vacuously - a node that returned an empty envelope would satisfy them
        all. The success path must still carry the record evidence."""
        result = PostProcessNode().execute(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        out = result["formatted_output"]
        assert out["record_id"] == "101"
        assert out["record_ref"] == "kintone://app/17/records/101"
        assert out["record_title"] == "Record 101"
        assert out["intent"] == "lookup_record"
        assert out["confirmation"].startswith("Retrieved kintone record")
        assert out["kintone_payload"] == {"app": "17", "id": "101"}
