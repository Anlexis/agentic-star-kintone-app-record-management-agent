# CMN-C2-276 - Unit tests: inner KintoneWorkflowGraph (BaseGraph) contract.
#
# The compiled outer path is exercised end-to-end by
# tests/proof_of_boundary/; this module unit-checks the inner graph's identity,
# config forwarding, the caller-context bridge, routing, the output contract,
# and a direct inner invoke on the network-free stub.

from langgraph.graph import END

from framework.schemas.agent_status import AgentStatus

from src.graph.domain_workflow_graph import KintoneWorkflowGraph
from src.schemas.state import State, from_json


def _graph(config=None):
    return KintoneWorkflowGraph(config=config or {})


def test_inner_graph_identity():
    g = _graph()
    assert g.name == "kintone_app_record_workflow"
    assert g.state_schema is State


def test_extra_initial_state_injects_kintone_config_as_json():
    g = _graph({"configurable": {"kintone": {"base_url": "https://kintone.example.test/k/v1"}}})
    extra = g._extra_initial_state()
    # Forwarded as a JSON string, not a native dict.
    assert isinstance(extra["kintone_config"], str)
    assert from_json(extra["kintone_config"], {}) == {"base_url": "https://kintone.example.test/k/v1"}


def test_extra_initial_state_without_kintone_section_still_seeds_the_caller_channel():
    extra = _graph()._extra_initial_state()
    assert "kintone_config" not in extra
    assert extra["input_context"] == {}


def test_extra_initial_state_seeds_the_caller_contract_from_the_bridge():
    """The framework does not forward input_context into a subgraph, so the
    validated contract is read back off the bridge inside subgraph.invoke()."""
    from src.graph.context_bridge import set_caller_input_context

    set_caller_input_context({"app_hint": "17", "record_hint": "101"})
    try:
        extra = _graph()._extra_initial_state()
        assert extra["input_context"] == {"app_hint": "17", "record_hint": "101"}
    finally:
        set_caller_input_context(None)


def test_route_error_ends_graph():
    g = _graph()
    assert g.route({"status": AgentStatus.ERROR.value}) == END
    assert g.route({"status": AgentStatus.SUCCESS.value}) == "confirm"


def test_get_output_surfaces_record_fields():
    g = _graph()
    out = g.get_output(
        {
            "result": {"record_id": "101", "record_ref": "kintone://app/17/records/101", "confirmation": "ok"},
            "status": AgentStatus.SUCCESS.value,
            "intent": "lookup_record",
            "app_id": "17",
            "record_id": "101",
            "record_ref": "kintone://app/17/records/101",
            "record_title": "Record 101",
            "confirmation": "ok",
            "kintone_payload": "{}",
            "redaction_flags": "[]",
            "error_log": [],
            "trace_id": "tr",
            "correlation_id": "co",
            "node_history": ["ValidateInputNode", "ConfirmNode"],
        }
    )
    assert out["status"] == AgentStatus.SUCCESS.value
    assert out["intent"] == "lookup_record"
    assert out["app_id"] == "17"
    assert out["record_ref"] == "kintone://app/17/records/101"
    assert out["confirmation"] == "ok"
    assert out["output"] == {"record_id": "101", "record_ref": "kintone://app/17/records/101", "confirmation": "ok"}


def test_get_output_carries_error_log():
    g = _graph()
    out = g.get_output({"status": AgentStatus.ERROR.value, "error_log": ["boom"], "confirmation": ""})
    assert out["status"] == AgentStatus.ERROR.value
    assert out["error_log"] == ["boom"]


def test_inner_graph_compiles():
    g = _graph()
    g.compile()
    assert g._compiled is not None


def test_inner_invoke_lookup_on_the_stub_transport():
    """Direct inner invoke (default ANONYMOUS ctx - every inner node is
    ANONYMOUS): validate -> classify -> infer -> call(stub) -> confirm."""
    g = _graph({"configurable": {"kintone": {"base_url": "https://example.cybozu.com/k/v1"}}})
    g.compile()
    result = g.invoke(user_input="Look up record 101 in kintone app 17 and summarize what is on file.")
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["record_id"] == "101"
    assert result["record_ref"] == "kintone://app/17/records/101"
    assert result["intent"] == "lookup_record"
    assert result["confirmation"]
    history = result.get("node_history", [])
    assert history == [
        "ValidateInputNode",
        "ClassifyIntentNode",
        "InferKintoneFieldsNode",
        "CallKintoneApiNode",
        "ConfirmNode",
    ]
