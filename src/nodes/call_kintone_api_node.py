"""AgentCore Platform v1.0 - inner workflow Step 4: CallKintoneApi (tool side-effect).

Performs the lookup/create/update call against the kintone REST API record
endpoints via src/services/kintone_client.py.

Security posture:
  Trust: required_trust_level = ANONYMOUS. The single external trust gate lives
       on the OUTER backbone pre_process (VERIFIED_EXTERNAL), not on this inner
       node. GraphNode.execute() passes the caller's InvocationContext into the
       inner subgraph UNCHANGED (no trust elevation), so a real external caller
       runs this call under its own VERIFIED_EXTERNAL context; declaring
       INTERNAL here would deny that already-gated external caller before the
       call ever runs. The node therefore stays ANONYMOUS.
  Secrets: the integration token is read via
       ctx.secrets.get("KINTONE_API_TOKEN") (InvocationContext.from_state) -
       never os.environ, never stored in state. While the deterministic
       NETWORK-FREE stub transport is active a missing token is tolerated (a
       sentinel placeholder is used - it is never sent anywhere because no
       request leaves the process); with a LIVE transport injected, a missing
       token is a hard status=error - a real API is never called
       unauthenticated. The key is read with .get() rather than .require(), so
       it is not declared as a compile-time requirement in the manifest: a
       declared-but-unprovisioned secret would fail the agent at compile time,
       which would make the network-free default unusable.
  Audit: emit_trace_event() is called on the success path - a side-effect
       against an external system; HTTP 4xx/5xx surfaces as status=error +
       error_log (no silent pass).
  Error reasons: every error_log entry this node writes carries CLOSED-SET
       labels only - the HTTP status, the exception type, a fixed phrase - and
       never a record id, an app id, a record title, an intent value, or an
       upstream response body. error_log is the INTERNAL channel (post_process
       publishes a constant reason code and never projects it), and it still
       has to be closed-set: the audit trail reads it, and the framework's own
       egress scan raises on credential-shaped text anywhere in a node result
       - which would replace the contained result with a bare error. An
       upstream body is unbounded third-party text that can quote the record
       it refused.

Configuration: this node takes NO constructor arguments (SDK v1 nodes are
no-arg). Kintone settings (base_url) arrive as the JSON `kintone_config` state
field - injected by the inner graph's _extra_initial_state() from the
config/config.yaml section forwarded by
KintoneWorkflowGraphNode._parent_config() - or via the optional
`config["configurable"]["kintone"]` argument for direct invocation. The client
is constructed locally per call (no module-global mutation).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json
from src.services.kintone_client import KintoneApiError, KintoneClient

_SECRET_KEY = "KINTONE_API_TOKEN"
# Placeholder handed to the network-free stub transport when no secret is
# provisioned. Never sent over any network (the stub performs no I/O) and never
# written to state or logs.
_STUB_PLACEHOLDER = "stub-transport-no-credential"


class CallKintoneApiNode(FunctionNode):
    """Look up / create / update a kintone app record via the REST API."""

    # The external trust gate is enforced UPSTREAM on the outer backbone
    # pre_process (VERIFIED_EXTERNAL). This inner node runs under the caller's
    # UNELEVATED context (GraphNode does not elevate trust for the subgraph), so
    # it must stay ANONYMOUS - declaring INTERNAL would deny a real external
    # caller before the call runs.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any], config: "dict[str, Any] | None" = None) -> dict[str, Any]:
        payload = from_json(state.get("kintone_payload"), None)
        if not payload:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallKintoneApiNode: missing kintone_payload"],
            }

        intent = state.get("intent", "lookup_record") or "lookup_record"

        # Settings: manifest section from state (graph-injected), overridable via
        # an explicit config["configurable"]["kintone"] for direct invocation.
        # Merged into a LOCAL dict - module globals are never mutated.
        settings = dict(from_json(state.get("kintone_config"), {}) or {})
        override = ((config or {}).get("configurable") or {}).get("kintone") or {}
        settings.update(override)

        # Client built locally per call; with no injected transport it uses the
        # deterministic NETWORK-FREE stub (documented limitation, docs/02).
        base_url = str(settings.get("base_url", "") or "").strip()
        client = KintoneClient(base_url=base_url) if base_url else KintoneClient()

        # Token from the bound secret provider - never os.environ / state.
        ctx = InvocationContext.from_state(state)
        api_token = ctx.secrets.get(_SECRET_KEY)
        if api_token is None:
            if client.uses_stub_transport:
                # Stub limitation: no request leaves the process, so run with
                # a non-credential placeholder (see module docstring).
                api_token = _STUB_PLACEHOLDER
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        f"CallKintoneApiNode: secret {_SECRET_KEY} unavailable - "
                        "refusing to call a live transport unauthenticated"
                    ],
                }

        app_id = state.get("app_id", "") or str(payload.get("app", "") or "")
        if not app_id:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallKintoneApiNode: unresolved kintone app id - cannot address an app record"],
            }
        record_no = str(payload.get("id", "") or "")

        try:
            if intent == "lookup_record":
                if not record_no:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallKintoneApiNode: unresolved record number - cannot look up record"],
                    }
                resp = client.get_record(app_id, record_no, api_token) or {}
                record = resp.get("record") or {}
                if not record:
                    # Closed-set reason, no record or app id: a log line that
                    # named the record would carry the write evidence the error
                    # envelope deliberately omits into the audit trail and any
                    # checkpoint reader.
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallKintoneApiNode: no record found for the requested id in the target app"],
                    }
                record_id = str((record.get("$id") or {}).get("value", "")) or record_no
                title_field = record.get("title") or {}
                record_title = state.get("record_title", "") or str(title_field.get("value", ""))
            elif intent in ("create_record", "update_record"):
                if intent == "update_record" and not record_no:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallKintoneApiNode: unresolved record number - cannot update record"],
                    }
                if intent == "create_record":
                    resp = client.add_record(payload, api_token) or {}
                else:
                    resp = client.update_record(payload, api_token) or {}
                record_id = str(resp.get("id", "")) or record_no
                record_title = state.get("record_title", "")
            else:
                # The intent label is, by definition here, NOT one of the
                # closed set - so it is the one value this branch must not echo.
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": ["CallKintoneApiNode: unknown intent - refusing to act on an unrecognised operation"],
                }
        except KintoneApiError as exc:
            # HTTP status only. A live tenant's error body is unbounded
            # third-party text that can quote the very record it refused (title,
            # field values, the requester); the exception's own text embeds it
            # (see KintoneApiError), so the closed-set signal is logged and the
            # message is not.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallKintoneApiNode: kintone API error {exc.status_code}"],
            }
        except Exception as exc:  # transport failure - no silent pass
            # Exception TYPE only, for the same reason: a transport error string
            # can carry the request URL, and the URL carries the app and record
            # ids.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallKintoneApiNode: kintone call failed ({type(exc).__name__})"],
            }

        record_ref = f"kintone://app/{app_id}/records/{record_id}" if record_id else ""

        # Audit the tool side-effect - intent + presence signals only,
        # never record content or credentials.
        emit_trace_event(
            "call_kintone_api_complete",
            {
                "intent": intent,
                "has_record_id": bool(record_id),
                "stub_transport": client.uses_stub_transport,
            },
            state,
        )

        return {
            "record_id": record_id,
            "record_ref": record_ref,
            "app_id": app_id,
            "record_title": record_title,
            "status": AgentStatus.SUCCESS.value,
        }
