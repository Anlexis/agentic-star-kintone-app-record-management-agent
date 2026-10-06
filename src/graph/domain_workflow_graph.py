"""AgentCore Platform v1.0 - inner kintone workflow graph (Cat 2 domain workflow).

Instantiated by KintoneWorkflowGraphNode.get_subgraph() in graph.py. Inherits
BaseGraph directly for a fully custom linear topology:

    START -> validate_input -> classify_intent -> infer_kintone_fields
          -> call_kintone_api -> confirm -> END

Config (forwarded from the outer graph via _parent_config(), under
config["configurable"]):
    kintone  - integration settings (base_url, ...) from config/config.yaml;
               injected into State as the JSON `kintone_config` field via
               _extra_initial_state() so the no-arg nodes can read it
    agent    - runtime values (max_retry, timeout_s) from config/config.yaml

The validated caller contract arrives separately, over the context bridge:
the framework invokes a subgraph without forwarding input_context, so
_extra_initial_state() reads it back off the bridge and seeds it into the
inner state.

Nodes are registered WITHOUT constructor arguments (SDK v1 nodes are no-arg;
ctor args raise TypeError at graph build).
"""

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import get_caller_input_context
from src.nodes.validate_input_node import ValidateInputNode
from src.nodes.classify_intent_node import ClassifyIntentNode
from src.nodes.infer_kintone_fields_node import InferKintoneFieldsNode
from src.nodes.call_kintone_api_node import CallKintoneApiNode
from src.nodes.confirm_node import ConfirmNode
from src.schemas.state import State, to_json


class KintoneWorkflowGraph(BaseGraph):
    """Inner graph: NL -> validate -> classify -> infer -> call -> confirm."""

    @property
    def name(self) -> str:
        return "kintone_app_record_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # No mandatory config: the kintone section is optional (the client
        # falls back to the documented placeholder base_url + the network-free
        # stub transport), and a missing/unusable setting is handled at
        # CallKintoneApiNode.execute() as a graceful status=error rather than
        # a compile-time crash.
        pass

    def register_nodes(self) -> None:
        # No super() - BaseGraph.register_nodes() is abstract. Do NOT register
        # initialize / finalize (outer backbone concern). All nodes are no-arg.
        self._nodes["validate_input"] = ValidateInputNode()
        self._nodes["classify_intent"] = ClassifyIntentNode()
        self._nodes["infer_kintone_fields"] = InferKintoneFieldsNode()
        self._nodes["call_kintone_api"] = CallKintoneApiNode()
        self._nodes["confirm"] = ConfirmNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "validate_input")
        self._sg.add_edge("validate_input", "classify_intent")
        self._sg.add_edge("classify_intent", "infer_kintone_fields")
        self._sg.add_edge("infer_kintone_fields", "call_kintone_api")
        self._sg.add_edge("call_kintone_api", "confirm")
        self._sg.add_edge("confirm", END)

    def route(self, state: State) -> str:
        # Required by the BaseGraph ABC. Linear topology -> never called unless
        # an add_conditional_edges() references it. Annotated with this graph's
        # OWN State on purpose: a conditional-edge path callable's annotation
        # is read as its input schema, and a wider annotation would project
        # away the very fields the route reads.
        return END if state.get("status") == AgentStatus.ERROR.value else "confirm"

    def _extra_initial_state(self) -> "dict[str, Any]":
        # Forward the `kintone` section (arriving under config["configurable"]
        # from _parent_config()) into State as a JSON string (msgpack-safe) so
        # the no-arg CallKintoneApiNode can read it via state.get("kintone_config").
        extra: dict[str, Any] = {}
        configurable = self.config.get("configurable") or {}
        kintone = configurable.get("kintone") or {}
        if kintone:
            extra["kintone_config"] = to_json(kintone)
        # The framework does not forward input_context into a subgraph, so the
        # validated caller contract is read back off the bridge here - this hook
        # runs inside subgraph.invoke(), after the outer state is out of reach.
        extra["input_context"] = get_caller_input_context()
        return extra

    def get_output(self, state: AgentState) -> "dict[str, Any]":
        return {
            "output": state.get("result") or state.get("confirmation"),
            "status": state.get("status"),
            "intent": state.get("intent", ""),
            "app_id": state.get("app_id", ""),
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "record_title": state.get("record_title", ""),
            "confirmation": state.get("confirmation", ""),
            "kintone_payload": state.get("kintone_payload", ""),
            "redaction_flags": state.get("redaction_flags", ""),
            "error_log": state.get("error_log", []),
            "trace_id": state.get("trace_id", ""),
            "correlation_id": state.get("correlation_id", ""),
            "node_history": state.get("node_history", []),
        }
