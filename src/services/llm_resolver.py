"""AgentCore Platform v1.0 - CMN-C2-276 LLM resolution (optional enhancement, Step 8e).

Builds a fresh AzureOpenAIClient per invocation from ctx.secrets - never at node
construction, never cached on self. Node instances persist for the process
lifetime (the outer graph is compiled once at server startup; the inner
subgraph's nodes are rebuilt per GraphNode.get_subgraph() call but still must
not carry a client resolved from one caller's secrets into another's request),
so the client has to come from the per-invocation secrets binding, not from
anything set in __init__/register_nodes().

resolve_llm() never raises: any failure to resolve (no secret bound, malformed
InvocationContext) returns None, and the only call site (InferKintoneFieldsNode)
already treats llm=None as "use the deterministic regex baseline"
(src/services/service.py).
"""

from __future__ import annotations

from typing import Any

from framework.schemas.invocation_context import InvocationContext
from shared.services.llm.azure_openai_client import AzureOpenAIClient


def resolve_llm(constructor_llm: Any, state: dict[str, Any]) -> Any:
    """Return constructor_llm if injected (test-double seam only - production
    wiring in register_nodes() never passes one), else build a real
    AzureOpenAIClient from the invocation's bound secrets. Returns None on any
    failure (missing secret, absent lifecycle identity fields on a bare state,
    malformed context) rather than raising.
    """
    if constructor_llm is not None:
        return constructor_llm
    try:
        ctx = InvocationContext.from_state(state)
        return AzureOpenAIClient(
            {
                "api_key": ctx.secrets.require("AZURE_OPENAI_API_KEY"),
                "azure_endpoint": ctx.secrets.require("AZURE_OPENAI_ENDPOINT"),
                "azure_deployment": ctx.secrets.require("AZURE_OPENAI_DEPLOYMENT"),
            }
        )
    except Exception:
        return None
