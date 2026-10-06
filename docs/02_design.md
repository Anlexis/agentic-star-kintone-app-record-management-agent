# Template Design Specification — CMN-C2-276 Kintone App Record Agent

## Position in AgentCore Architecture

| Aspect | Value |
|---|---|
| Agent class | `KintoneAppRecordAgent` (`src/graph/graph.py`) |
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
| Category | Cat 2 (multi-step domain workflow, ToolCallingAgent) |
| Base type | ToolCallingAgent — classify intent, extract app-record fields, build a kintone REST API request, call the tool, format the confirmation. No retrieval, no autonomous loop. |

The outer `AgentBaseGraph` provides the fixed 5-node backbone; the domain
pipeline is encapsulated in a `GraphNode` (`main` slot) wrapping an inner
`BaseGraph` (`src/graph/domain_workflow_graph.py`).

**Three-layer separation:**

- State: flat TypedDict `State(AgentState)` (no Pydantic — not msgpack-safe)
- Node: framework inheritance (Template Method: override `execute(self, state) -> dict` only)
- Graph: composition (`register_nodes()` + `super().register_nodes()`; `add_edges()`
  is not overridden on the outer graph)

## Architecture Overview

### Outer graph — node configuration (`src/graph/graph.py`)

| Node | Responsibility | Input State | Output State | Trust | Inherits/Overrides |
|------|---------------|-------------|--------------|-------|-------------------|
| initialize | framework setup (schema, session, trust) | user_input | session/trust fields | framework default | InitializeNode (default) |
| pre_process | validate the caller contract; screen both caller channels; sanitize and serialize the request into `validated_input` | user_input, input_context | validated_input, app_hint, record_hint, caller_fields | **VERIFIED_EXTERNAL** (the single external gate) | PreProcessNode (FunctionNode) |
| main | run the inner kintone workflow subgraph | validated_input, caller_fields | result, intent, app_id, record_id, record_ref, record_title, confirmation, kintone_payload | GraphNode (caller ctx forwarded unchanged) | KintoneWorkflowGraphNode (GraphNode) |
| post_process | shape caller-facing `formatted_output`; module-level `_security_gate_output()` scan; clear output-bearing fields on a violation | inner-result fields | formatted_output | ANONYMOUS | PostProcessNode (FunctionNode) |
| finalize | framework finalize (metadata, timing) | — | response_metadata | framework default | FinalizeNode (default) |

### Inner workflow — node configuration (`src/graph/domain_workflow_graph.py`)

The inner graph inherits `BaseGraph` (fully custom linear topology). The five
pipeline steps map 1:1 to inner nodes. **Every inner domain node declares
`required_trust_level = TrustLevel.ANONYMOUS`** — the caller's
`InvocationContext` is forwarded into the subgraph unchanged, so the single
external trust gate stays on the backbone `pre_process`.

| Inner node | Step | Responsibility | Output | Trust |
|------|------|---------------|--------|-------|
| validate_input | 1 ValidateInput | empty/non-request guard; instruction-override refusal; deterministic (regex) flag-and-redact of email/token-like strings before logging | validated_input, app_hint, record_hint, redaction_flags | ANONYMOUS |
| classify_intent | 2 ClassifyIntent | deterministic keyword classification -> lookup_record / create_record / update_record; low-confidence -> lookup_record (read-only default — never a write) | intent | ANONYMOUS |
| infer_kintone_fields | 3 InferKintoneFields | extract kintone app id / record number via regex/hint (never invented, never LLM-derived); extract record title / custom record fields via regex baseline, optionally enhanced by an Azure OpenAI call that degrades silently to the baseline on any failure (Step 8e); assemble the kintone REST API request body per intent | record_title, app_id, kintone_payload | ANONYMOUS |
| call_kintone_api | 4 CallKintoneApi | GET /record.json (lookup) / POST /record.json (create) / PUT /record.json (update) via `KintoneClient`; token via ctx.secrets; 4xx/5xx -> status=error | record_id, record_ref, app_id, record_title | ANONYMOUS |
| confirm | 5 Confirm | format intent + record id + reference into a human-readable confirmation | confirmation, result | ANONYMOUS |

### Data Flow

```
Outer:  START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
                                              | (RETRY, max 3) ^
Inner (inside main / KintoneWorkflowGraphNode):
        START -> validate_input -> classify_intent -> infer_kintone_fields
              -> call_kintone_api -> confirm -> END
```

The instruction text travels as a JSON string: `pre_process` serializes
`{"text": ...}` into `validated_input`,
`KintoneWorkflowGraphNode.extract_input()` hands that JSON to the subgraph, and
the first inner node (`validate_input`) parses it back.

## Caller-data contract

`POST /invoke` accepts an `input_context` mapping alongside the free-text
instruction. `PreProcessNode` owns that contract: it is the only place caller
fields are interpreted, and nothing reaches the workflow that has not passed it.

| Caller field (aliases) | Accepted value | Renders into |
|---|---|---|
| `app_id` / `app` / `app_hint` | numeric kintone app id, 1–9 digits, 1..999999999 | `kintone://app/<app>/records/<id>` |
| `record_id` / `record_no` / `record_hint` | numeric record number, same bounds | `record_id`, the same reference, the confirmation |

Rules the node enforces:

- **Type before value.** A string or an integer is accepted; a float, boolean,
  mapping or list is refused outright and never coerced. `str(float("nan"))`
  is `"nan"` and `str(1e9)` is `"1000000000.0"`, so a coercing reader would let
  a non-finite or unbounded value name the target of a write. Bare
  `NaN`/`Infinity` really do arrive on request bodies — Python's JSON decoder
  accepts them — and are refused on arrival.
- **Bounded and inert.** Accepted values are re-rendered as `str(int(value))`,
  so whatever spelling arrived, what reaches the output is a canonical digit
  string. There is no free-text caller field: everything a caller can put into
  the response is an identifier.
- **Fail closed, quietly.** A breach names the FIELD and never echoes the
  value; an unrecognised field name is masked rather than echoed. An *absent*
  hint is not a breach — the workflow falls back to an id named in the
  instruction text, and an unresolvable target is a clean error, never a guess.
- **Envelope caps.** The entry point caps the mapping at 16 keys and 256 KB
  before the graph is entered.

### Caller context across the graph boundary

`GraphNode.execute()` invokes the inner graph as
`subgraph.invoke(user_input, session_id=..., ctx=...)` and does **not** forward
`input_context`, so an inner-node read of `state["input_context"]` would always
see `{}` through the nested graph. `src/graph/context_bridge.py` carries it
across the sanctioned hooks: `extract_input()` stashes the validated contract
immediately before the invoke, and the inner graph's `_extra_initial_state()`
seeds it into the inner state. Only the validated contract crosses — never the
raw request body.

The alternative, carrying the fields inside the `validated_input` JSON, is not
usable: the framework masks that field at every node boundary, so a caller's
target identifier could be rewritten between hops.

### State Definition (`src/schemas/state.py`)

All domain fields are declared `NotRequired[...]` — fields are absent until
their producer node writes them. Dict/list payloads are stored as JSON strings
(`Optional[str]`) via the module helpers `to_json` / `from_json`, used by every
producer and consumer.

| Field | Type | Purpose | Producer |
|-------|------|---------|----------|
| app_hint | NotRequired[str] | caller-supplied kintone app id; never inferred | pre_process / validate_input |
| record_hint | NotRequired[str] | caller-supplied record number; never inferred | pre_process / validate_input |
| app_id | NotRequired[str] | resolved kintone app id | infer_kintone_fields |
| caller_fields | NotRequired[Optional[str]] | JSON — the validated caller contract, read back at the graph boundary | pre_process |
| redaction_flags | NotRequired[Optional[str]] | JSON list of pattern categories redacted before logging | validate_input |
| record_title | NotRequired[str] | record title / display label | infer_kintone_fields / call_kintone_api |
| kintone_payload | NotRequired[Optional[str]] | JSON — assembled kintone REST API request body (stored as a JSON string via `to_json`/`from_json`) | infer_kintone_fields |
| kintone_config | NotRequired[Optional[str]] | JSON — the `kintone:` section of `config/config.yaml`, forwarded by `_parent_config()` and injected via the inner graph's `_extra_initial_state()` | inner graph |
| record_id | NotRequired[str] | record number / record id returned by kintone | call_kintone_api |
| record_ref | NotRequired[str] | record reference (`kintone://app/<app>/records/<id>`) | call_kintone_api |
| confirmation | NotRequired[str] | human-readable confirmation | confirm |

`intent`, `result`, `validated_input`, `formatted_output` and `input_context`
are inherited from `AgentState` and are **not** re-declared.

**State constraints (mandatory, satisfied):**

- Flat TypedDict only (primitives + JSON-serializable) — no Pydantic/dataclass.
- No JWT / API keys / credentials in State — the kintone token is accessed via `ctx.secrets`.
- `InvocationContext` read via `InvocationContext.from_state(state)`, never stored in State.

## Configuration

Two files, two jobs:

| File | Contents | Read by |
|---|---|---|
| `config/agent.yaml` | the static registry manifest — flat, root-level keys only (id, name, namespace, category, entry-point class, `required_trust_level`, `requires`) | the platform registry at discovery time |
| `config/config.yaml` | runtime parameters — `max_retry`, `timeout_s`, and the `kintone:` integration section | the graph, as its `config=` mapping |

Nodes take **no constructor arguments** (SDK v1 nodes are no-arg; configuration
never rides on node instances). `load_runtime_config()` in `src/graph/graph.py`
reads `config/config.yaml`; the standalone entry point constructs the agent with
it, exactly as the registry does, so the declared values are live rather than
silently absent. `KintoneWorkflowGraphNode._parent_config()` forwards the
`kintone:` section and the runtime values (as the `agent` section) to the inner
graph under `config["configurable"]`. The inner graph's
`_extra_initial_state()` injects the `kintone` section into State as a JSON
string (`kintone_config`), where `CallKintoneApiNode.execute(state,
config=None)` reads it (an explicit `config["configurable"]["kintone"]`
override is also honoured for direct invocation).

`timeout_seconds` from the pre-migration manifest is `timeout_s` here — the
name the framework's config validator reads.

## Security Design

- **Trust gate** — the single external trust gate is on the outer backbone:
  `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`. Every inner
  domain node — **including the write-capable `CallKintoneApiNode`** — declares
  `TrustLevel.ANONYMOUS`. `GraphNode.execute()` forwards the caller's
  `InvocationContext` into the subgraph **unchanged** (no elevation), and
  `VERIFIED_EXTERNAL (1) < INTERNAL (2)`, so declaring an inner node `INTERNAL`
  would deny a legitimate external caller before the call runs — the boundary
  is therefore enforced exactly once, at `pre_process`. The agent-level default
  trust `VERIFIED_EXTERNAL` is declared in `config/agent.yaml`.
  `src/api/server.py` enforces the standalone entry-point Bearer-token auth
  boundary (`INVOKE_AUTH_TOKEN` -> VERIFIED_EXTERNAL elevation).
- **Instruction-override screen (template-owned)** — `src/services/security.py`
  refuses directives aimed at the model: chat-template control tokens
  (`<|im_start|>`, `[INST]`, `<<SYS>>`), instruction-override phrases,
  system-prompt exfiltration, model-role reassignment and privileged-mode
  role-play. It runs on the instruction text **and** depth-first over every
  decoded string in `input_context`, keys included, and it screens each string
  both raw and after the markup strip — the raw pass catches control tokens the
  strip would silently remove, the stripped pass catches a directive spliced
  with markup (`ig<b>nore`) that would re-assemble downstream. It is enforced
  inside `execute()`, so calling the node directly still refuses: the guarantee
  does not depend on a platform gate being present or configured on. Every
  alternative is anchored on a full directive phrase, so ordinary app-record
  prose ("please ignore my previous request", "update the record and show it",
  "override approved by the app administrator") is unaffected. `ValidateInputNode`
  re-applies the same screen to the text it actually receives.
- **Input flag-and-redact** — `ValidateInputNode.execute()` runs a deterministic
  (regex, not model-based) scan for email addresses and access-token-like
  strings (`eyJ...`, `secret_...`, `sk-...`) and redacts them before any
  logging. An app-record request legitimately names business entities, so this
  is flag-and-redact for safe logging, not a hard reject; the framework's own
  input mask additionally masks emails/phones/names in
  `user_input`/`validated_input`.
- **Secrets** — the integration token is read via
  `ctx.secrets.get("KINTONE_API_TOKEN")` (`InvocationContext.from_state(state)`),
  never `os.environ`, never stored in State. It is read with `.get()` rather
  than `.require()`, and is therefore **not** declared in
  `config/agent.yaml requires.secrets`: `requires` is a compile-time contract,
  so declaring a key that a deployment has not provisioned would make the agent
  fail to start — including in its default network-free configuration. A missing
  token is tolerated only while the stub transport is active (no live call is
  made); with a live transport injected, a missing token is a hard
  `status=error`.
- **Output gate** — the domain output gate is the **module-level**
  `_security_gate_output()` in `src/nodes/post_process_node.py`, called from
  `PostProcessNode.execute()`. This template renders no monetary aggregates —
  its caller-facing output is a record identifier, a `kintone://` reference, a
  title and a confirmation sentence — so the invariant it enforces is:
  a SUCCESS response always carries record evidence (`record_id`/`record_ref`),
  and no credential material leaves the agent. The scan walks the WHOLE output
  structure, keys included: `kintone_payload` is a nested mapping carrying
  caller-derived text, so a top-level-only scan would step past a credential in
  a record field. It runs on the error shape as well as the success shape — an
  error message is caller-facing output too.
  **On EVERY error return the node clears every output-bearing state field**
  (`formatted_output`, `result`, `confirmation`, `kintone_payload`,
  `record_title`, `record_ref`, `record_id`, `app_id`, `intent`) and returns an
  error naming the location, never the value. Returning an error status without
  clearing would not be containment: the response envelope falls back to
  `state["result"]` even on an error status, so the blocked content would still
  ship inside the error envelope. For the same reason the gate recognises every
  credential family the platform's own egress scan does — a family it missed
  would be raised past the clearing instead of contained by it.
  No node defines `_extra_security_gate_input/_output` instance methods
  (the framework hooks are final / auto-wrapped — domain checks live inline or
  in module-level helpers).
- **Error-path containment (every error return, not just a gate violation)** —
  both error returns of `PostProcessNode.execute()` — a gate violation and a
  pre-existing inner-workflow error — go through the one module-level
  `_contain()` helper: it spreads the same cleared set and replaces
  `formatted_output` with a **closed-set** envelope, `{"reason": <code>}` and
  nothing else, the code being one of `ERROR_REASONS`
  (`kintone_workflow_failed` / `output_withheld_by_gate`). Nothing is read out
  of state into it — not the record, not `error_log`. `record_id`/`record_ref`
  are this agent's **write evidence** — the gate refuses a SUCCESS that lacks
  them — so returning them under an ERROR status would tell a caller being
  informed of failure that a kintone record was nonetheless touched, and which
  one. Omitting a field from one envelope is not clearing it: the clearing is
  what stops a checkpoint or a downstream reader recovering it. The envelope is
  deliberately **truthy** — the response builder reads `formatted_output`
  falling back to `result` with no status check, so an empty/falsy replacement
  would activate that same fallback.
- **`error_log` is internal, and still closed-set** — the caller never receives
  it: it is not projected under any key of the envelope, and the inner entries
  are not re-emitted (the state reducer appends, so that would duplicate every
  line). Gate violations are written to it naming the offending PATH — fixed
  keys and indices; a credential-shaped mapping key is written as `<withheld>`,
  never quoted — and the audit event carries the count. Every reason a node
  writes still carries closed-set labels only — a fixed phrase, the HTTP status,
  the exception type — never an interpolated record id, app id, record title,
  intent value, exception message or upstream response body: the audit trail
  and any checkpoint read the log, and the framework's own egress scan raises
  on credential-shaped text anywhere in a node result, which would replace the
  contained result with a bare error. An upstream error body is unbounded
  third-party text: it is not echoed anywhere.
- **Audit** — every node's `execute()` emits exactly one positional
  `emit_trace_event("<node>_complete", {small non-PII payload}, state)` on its
  SUCCESS path (intent / presence signals only — never request text, record
  content, or credentials), plus a refusal event on each fail-closed path.
  `__call__()` is never overridden. Event names (documented for operations):

  | Node | Event |
  |------|-------|
  | pre_process | `pre_process_complete`, `pre_process_validation_failed` |
  | validate_input | `validate_input_complete`, `validate_input_refused` |
  | classify_intent | `classify_intent_complete` |
  | infer_kintone_fields | `infer_kintone_fields_complete`, `kintone_field_llm_degraded` |
  | call_kintone_api | `call_kintone_api_complete` |
  | confirm | `confirm_complete` |
  | post_process | `post_process_complete`, `post_process_blocked`, `post_process_error_contained` (closed-set reason code + error COUNT only) |

## Implementation note — optional LLM enhancement (Step 8e)

Intent classification (`ClassifyIntentNode`) stays a pure keyword heuristic —
no change. Field inference (`InferKintoneFieldsNode`) keeps its regex /
line-structure extraction as the baseline, **plus** an optional Azure OpenAI
enhancement for `record_title` and free-form record fields: the regex parser
only catches literal `"Key: value"` lines, so a request phrased in natural
language ("set the priority to high and mark it done") falls through to an
LLM call (`src/services/service.py::extract_record_fields_via_llm`, resolved
via `src/services/llm_resolver.py::resolve_llm`). The client is built fresh
per invocation from `ctx.secrets` inside `execute()` — never cached, never
built at construction — and any failure (missing secret, API error,
malformed/wrong-shape JSON response) degrades silently back to the regex
result; this node never raises or sets `status=ERROR` because of the LLM
step. **`app_id` / `record_no` are never LLM-derived, in every case** — they
stay resolved only from an explicit text mention or the caller-supplied hint,
per this node's own risk-mitigation note above (an id must never be
invented).

The manifest declares `generation_mode: "llm"` and `requires.extras:
["openai"]`; `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_ENDPOINT` /
`AZURE_OPENAI_DEPLOYMENT` are deliberately **not** under `requires.secrets`
(mirrors the `ANTHROPIC_API_KEY` precedent elsewhere in this scaffold) —
declaring them there would 503 a registry-based compile in any environment
that hasn't provisioned the Azure key, contradicting the graceful-degrade
contract. The template still runs and is fully testable without a model
backend: every LLM-path test uses an injected test-double client, never a
real network call.

## Limitation — kintone client (documented)

`src/services/kintone_client.py` is an injectable-transport client
(`KintoneApiError`, per-call token, no framework imports) that ships a
**deterministic, network-free stub** as its default transport: it returns the
documented kintone REST API response shapes (a `record` object for lookups; the
`{"id", "revision"}` receipt shape with a synthetic record-id echo for
create/update, derived from the request) so the pipeline is runnable and
testable without a live kintone tenant or the `requests` package. It does
**not** perform a live kintone call — the template never fakes one. To go live,
inject real `post`/`put`/`get` transports at construction and set the tenant
`base_url` (`https://<subdomain>.cybozu.com/k/v1`) under `kintone:` in
`config/config.yaml`; the method contracts and payload shapes are already
kintone REST API exact (`GET/POST/PUT /record.json`), so no business-logic
change is required. (The stub also runs without a live credential — see
Secrets above; a live transport requires `KINTONE_API_TOKEN`.)

## Framework Utilization

### Shared components used

- [x] `InvocationContext` — read in `CallKintoneApiNode` via `InvocationContext.from_state(state)` (secrets + trust)
- [x] Trust gate — single external gate `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; inner domain nodes (incl. `CallKintoneApiNode`) declare `TrustLevel.ANONYMOUS` (caller `InvocationContext` forwarded unchanged into the subgraph)
- [x] Secrets — `ctx.secrets.get("KINTONE_API_TOKEN")`; entry-point `bound_secrets` / `secrets_factory` / `provision_secrets` in `src/api/server.py`
- [x] `emit_trace_event()` — one positional call per node on the SUCCESS path; framework lifecycle events (node_start/node_complete/node_error) are not re-emitted

### Composition pattern

- **Pattern**: GraphNode (subgraph) — Cat 2 outer/inner split.
- **Composition target**: inner `KintoneWorkflowGraph` (`BaseGraph`) via `KintoneWorkflowGraphNode.get_subgraph()`.
- **Config forwarding**: `KintoneWorkflowGraphNode._parent_config()` reads
  `config/config.yaml` and forwards `{kintone, agent(max_retry, timeout_s)}`
  under `config["configurable"]` to the subgraph.
- **Caller-context forwarding**: `src/graph/context_bridge.py` (the framework
  does not forward `input_context` into a subgraph).
- **Error propagation strategy**: `propagate` (default) — inner errors re-raised as
  `SubgraphError`; per-step `status=error` + `error_log` for API/validation failures
  (no silent pass).

### Routing

The outer backbone's conditional edge uses the framework's own `route()`; the
inner graph is linear and defines `route()` only to satisfy the `BaseGraph`
ABC. It is annotated with the inner graph's **own** `State`: a conditional-edge
path callable's annotation is read as its input schema, so a wider annotation
would project away the very fields a route reads.

## Import isolation

- [x] The template imports `framework/` and `shared/` only; no platform-SDK import anywhere
- [x] `src/services/kintone_client.py` and `src/services/security.py` have no
      framework imports (pure service layer, stdlib only)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Framework base type | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed multi-step pipeline (Cat 2), not an autonomous loop |
| Composition pattern | flat Cat 1 (MainNode) | GraphNode + inner subgraph | GraphNode + inner subgraph | Cat 2 must not be flat; 5 domain steps live in the inner graph |
| Model dependency (original) | model client in the pipeline | deterministic pipeline | deterministic | the template runs and tests without a model backend; no dead prompt/config reads. **Superseded for `InferKintoneFieldsNode` by the Step 8e optional LLM enhancement above** — the model client, when configured, is an enhancement over this same deterministic baseline, never a hard dependency; every other node stays as originally decided here |
| kintone client | live `requests` call | injectable transport + documented stub default | injectable + stub default | never fake a live call; document the limitation; go-live is a transport injection + tenant base_url, no logic change |
| Node configuration | ctor-arg dependency injection | no-arg nodes + runtime-config forwarding via `_parent_config()` -> `configurable` -> state | no-arg nodes | SDK v1 nodes are no-arg (ctor args raise TypeError at graph build) |
| Caller channel | free-text caller fields | identifiers only | identifiers only | every caller string that renders into the response is inert by construction, not by inspection |
| Caller context into the subgraph | inside the `validated_input` JSON | ContextVar bridge | ContextVar bridge | the framework masks `validated_input` at every node boundary, which can rewrite an identifier between hops |
| Write target | infer app/record id from NL freely | caller-supplied/explicit ids only; unresolved left empty | explicit only | never write to the wrong app or record; unresolved id -> status=error, not invented |
| Default intent | create_record | lookup_record | lookup_record | low-confidence classification must never default to a write |
| Output-gate violation | raise | return ERROR and clear the output-bearing fields | clear | the response envelope falls back to `state["result"]` even on an error status, so raising alone still ships the blocked content |
| Error envelope on an inner-workflow failure | echo `record_id`/`record_ref` back so the caller can correlate | record-free notice + clear the same fields | record-free | the identifiers are the WRITE EVIDENCE the success gate demands; naming them under `status=error` tells a caller being told of failure that a record was touched, and which one |
| Caller-visible error | reason code + the `error_log` lines | reason code only — `{"reason": <code>}`, code ∈ `ERROR_REASONS` | reason code only | node-authored text can embed an exception message, an identifier, a name or an upstream response body; truncation or credential-only redaction is not a closed set — the envelope carries only values this module chose |
| Error reasons | interpolate the ids and pass the upstream body through | closed-set labels (fixed phrase / HTTP status / exception type) | closed-set | `error_log` is internal (the caller sees the reason code only) but the audit trail, any checkpoint and the framework's own egress scan read it; a reason naming the record puts the evidence there by another route, and an upstream body is unbounded third-party text |
