# CMN-C2-276 - Unit tests: config file sanity.
#
# Two files, two jobs:
#   config/agent.yaml  - the static registry manifest, read at ROOT level.
#   config/config.yaml - the runtime parameters the graph is constructed with.
# A value that lives in the wrong file is read by nobody, so both are asserted
# here, and the runtime file is asserted through the loader the code actually
# uses rather than by re-parsing it independently.

import pathlib

import pytest

try:
    import yaml  # pyyaml (transitive dep of the framework wheel)

    _YAML_ERROR = None
except Exception as exc:  # pragma: no cover
    yaml = None
    _YAML_ERROR = exc

_MANIFEST_PATH = pathlib.Path(__file__).parents[2] / "config" / "agent.yaml"

pytestmark = pytest.mark.skipif(_YAML_ERROR is not None, reason=f"pyyaml unavailable: {_YAML_ERROR}")


def _manifest():
    return yaml.safe_load(_MANIFEST_PATH.read_text())


def test_manifest_is_flat():
    """Registry keys live at the root - a nested `agent:` block reads as absent."""
    data = _manifest()
    assert "agent" not in data
    assert data["id"] == "CMN-C2-276"
    assert data["category"] == "Cat 2"
    assert data["industry"] == "CMN"
    assert data["base_type"] == "ToolCallingAgent"
    assert data["namespace"] == "cmn"
    assert data["enabled"] is True


def test_manifest_entry_point():
    """One dotted import path, not a split module:/class: pair."""
    data = _manifest()
    assert data["class"] == "src.graph.graph.KintoneAppRecordAgent"


def test_manifest_security():
    data = _manifest()
    # Agent-level entry trust, enforced by the outer backbone pre_process gate
    # (VERIFIED_EXTERNAL); inner domain nodes stay ANONYMOUS.
    assert data["required_trust_level"] == "VERIFIED_EXTERNAL"


def test_manifest_declares_no_compile_time_secrets():
    """`requires.secrets` is a COMPILE-TIME contract: every key listed must be
    provisioned or the agent fails to start. KINTONE_API_TOKEN and the three
    AZURE_OPENAI_* keys are all read with ctx.secrets.get()/.require() only
    inside execute(), on paths that already degrade gracefully on absence
    (the network-free transport; the Step 8e regex fallback) - declaring any
    of them here would make the default (key-less) configuration
    unstartable. `extras` is a real, unconditional install-time dependency
    (AzureOpenAIClient wraps langchain_openai.ChatOpenAI), so it IS declared."""
    requires = _manifest()["requires"]
    assert requires["secrets"] == []
    assert requires["extras"] == ["openai"]


def test_manifest_generation_mode_matches_the_code():
    """InferKintoneFieldsNode optionally calls an LLM (Step 8e); every other
    node stays deterministic."""
    assert _manifest()["generation_mode"] == "llm"


def test_runtime_config_is_loaded_by_the_code_that_needs_it():
    """Asserted through the loader the graph uses, so a value that moved out of
    reach fails here rather than silently reverting to a default."""
    from src.graph.graph import load_runtime_config

    runtime = load_runtime_config()
    assert isinstance(runtime["max_retry"], int)
    assert isinstance(runtime["timeout_s"], int)
    assert runtime["kintone"]["base_url"] == "https://example.cybozu.com/k/v1"


def test_runtime_values_reach_the_inner_graph():
    """The declared runtime values are forwarded to the subgraph - the failure
    this guards is a forwarder that returns {} and leaves every setting dead."""
    from src.graph.graph import KintoneWorkflowGraphNode

    configurable = KintoneWorkflowGraphNode()._parent_config()["configurable"]
    assert configurable["kintone"]["base_url"] == "https://example.cybozu.com/k/v1"
    assert configurable["agent"]["max_retry"] == 3
    assert configurable["agent"]["timeout_s"] == 30
