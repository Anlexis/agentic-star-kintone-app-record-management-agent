# Test Specification - CMN-C2-276 Kintone App Record Agent

## Test Strategy

- Test types: Unit (per node + service + config + inner graph) / Proof-of-Boundary
  (end-to-end through the real HTTP entry point, full outer-graph invoke, import
  isolation, state safety, server boot, HITL stub).
- Location: `tests/unit/`, `tests/proof_of_boundary/` (`tests/integration/` is an
  empty package; end-to-end coverage lives in the proof-of-boundary suite, which
  drives the real compiled graph).
- The kintone call is exercised through the deterministic, network-free stub
  transport (the default) and through injected fake transports; no live kintone
  call is ever made.
- **Trust-gate routing canon**: every per-node unit test invokes the node as
  `node(state)` — `BaseNode.__call__` routes the full security pipeline (trust
  gate -> PII mask -> `execute()` -> credential scan) — never bare
  `node.execute(state)`. State builders set `caller_trust_level =
  TrustLevel.VERIFIED_EXTERNAL.value` for PreProcessNode (the single external gate)
  and `TrustLevel.ANONYMOUS.value` for every other node.
  Two documented exceptions, both deliberate:
  `CallKintoneApiNode.execute(state, config=...)` (a 2nd argument `__call__`
  cannot forward), and the **refusal tests**, which call `execute()` directly so
  that the guarantee is shown to hold with no framework wrapper in front of it.
  The trust-rejection test asserts on the RETURNED error dict (`status ==
  AgentStatus.ERROR.value`, "trust gate denied" in `error_log`, execute-only keys
  absent) — `__call__` never raises for a trust denial.
- Assertion contract: the invoke surface is `result["output"]` / `status` /
  `trace_id` / `correlation_id` / `node_history` (never `formatted_output` at the
  invoke surface); status is compared to `AgentStatus.SUCCESS`/`.value`
  (lowercase `success`/`error`); the outer graph is called as
  `invoke(user_input=..., ctx=..., input_context=...)`; identifiers may be masked
  (`[MASKED]`) in text fields, so record evidence in prose is asserted by
  presence; audit spies assert on `call.args[1]` (the event payload), never the
  whole-call repr.
- Framework pipeline behaviours the suite encodes: `__call__` short-circuits on an
  incoming errored state (`execute()` is skipped; error status/error_log pass
  through); the framework input mask rewrites Title-Case bigrams (across
  newlines), emails, and digit groups in `user_input`/`validated_input` to
  `[MASKED]` before `execute()` sees the text — positive payloads are PII-free,
  intentional-PII tests assert the `[MASKED]` path.
- Domain audit events are muted per module via an autouse fixture patching
  `src.nodes.<mod>.emit_trace_event` (never a `sys.modules` stub of `shared.*`).

## Unit Tests (`tests/unit/`)

| TC-ID | Test file | Focus | Expected |
|-------|-----------|-------|----------|
| U-01 | test_trust_gate.py | trust boundary: ANONYMOUS caller on the VERIFIED_EXTERNAL pre_process gate; inner nodes ANONYMOUS; trust-posture declarations; denial emits no domain audit event; caller hints survive the gate | denial RETURNS an error dict ("trust gate denied" in error_log, execute-only keys absent, no domain audit event); VERIFIED_EXTERNAL passes with the caller contract on `caller_fields`; every inner node declares ANONYMOUS |
| U-02 | test_pre_process_node.py | caller-data contract: alias priority (app_id > app > app_hint, record_id > record_no > record_hint); string/int accepted and normalised to `str(int(...))`; markup strip; absent contract degrades; audit payload; **bounds matrix** (bool / float / NaN / ±Infinity / "NaN" / "Infinity" / 0 / -1 / 10 digits / "17.0" / "1e3" / non-numeric / mapping / list / empty) per field; **instruction-override screen** (control tokens, override phrases, exfiltration, role reassignment, markup-spliced) vs ordinary domain wording; nested / key-position / escaped payloads | every out-of-contract value fails CLOSED with no `validated_input` and no `caller_fields`; refusal names the field, never the value; hostile field names masked; ordinary app-record prose unaffected |
| U-03 | test_validate_input_node.py | empty/short guard; JSON-shaped input; caller contract read from `input_context`; instruction-override refusal (defence in depth); framework `[MASKED]` path for emails; node-level token flag-and-redact (`secret_*`) | email -> `[MASKED]` before execute; token -> `[REDACTED]` + `redaction_flags=["token"]` (JSON string); empty/short/override -> error; audit payload carries flags only |
| U-04 | test_classify_intent_node.py | intent = lookup_record / create_record / update_record (keyword, writes-first priority, read-only default) | correct intent per keyword; no-signal defaults to lookup_record with a non-fatal note; empty -> error; audit emits the intent label only |
| U-05 | test_infer_kintone_fields_node.py | app id / record number resolution (text > id-shaped hint > `Key: value` field; never invented, never LLM-derived); quoted record title; `Key: value` record fields; kintone REST record body per intent (stored as a JSON string); **optional LLM enhancement (Step 8e)**: well-formed JSON override, prose/markdown-fence-wrapped response, malformed/wrong-shape response, LLM raising, no `llm=` + no secret bound, empty input never calls the LLM | lookup `{app, id}`; create `{app, record{field: {value}}}`; update `{app, id, record}`; id-like keys excluded from the record body; unresolved ids left `""`; empty input -> error; audit emits field signals + `llm_enhanced` only; every LLM failure mode degrades silently to the regex result, never `status=ERROR` |
| U-06 | test_call_kintone_api_node.py | lookup/create/update via the network-free stub; `kintone_config` state field + `execute(state, config=...)` override; API error / unresolved app id / unresolved record number / unknown intent / missing payload; **closed-set error reasons** (no-match reason omits the record and app ids; API-error reason carries the HTTP status not the upstream body; transport-failure reason carries the exception type not the error string/URL; clean-call control); secret posture (a live transport refuses to run unauthenticated; token read via `ctx.secrets`, never env/state) | record_id/record_ref (`kintone://app/<app>/records/<id>`) on success; 403 surfaces in error_log; live+no-secret -> error "unauthenticated"; live+bound secret -> token passed to the client; audit emits presence signals with `stub_transport=True`; every error_log entry carries a closed-set label only (error_log is internal — the caller receives a reason code only — but the audit trail and the framework egress scan read it) |
| U-07 | test_confirm_node.py | human-readable confirmation per intent verb; ref/id formatting; title fallback | "Retrieved/Created/Updated kintone record ... ref=... id=..."; missing evidence -> error; audit emits intent + ref presence only |
| U-08 | test_post_process_node.py | `formatted_output` shaping (payload round-trip); errored state passes through `__call__` un-masked (short-circuit); the record-evidence gate; **containment** (every output-bearing field cleared on a violation, blocked value absent from the returned delta, nested value and nested KEY caught, a credential-shaped inner error_log line neither projected nor re-emitted); **existing-ERROR path containment** (envelope present AND truthy; no record_id/record_ref/app_id/record_title in the shipped envelope; every output-bearing field cleared in the delta; error status still reported; error_log not projected under any key and not re-emitted; clean-path control so the containment assertions cannot pass vacuously); every credential family recognised | success shape with parsed `kintone_payload`; error status/error_log preserved, no success shape fabricated; SUCCESS without record_id/record_ref blocked; EVERY error return clears `formatted_output`/`result`/`confirmation`/`kintone_payload`/`record_title`/`record_ref`/`record_id`/`app_id`/`intent` and replaces the envelope with the closed-set `{"reason": <code>}` and nothing else |
| U-09 | test_kintone_client.py | kintone REST API record client: get/add/update record via `/record.json`; `X-Cybozu-API-Token` header; `KintoneApiError` on non-2xx (message + errors extraction); stub shapes (`record` object for lookup / `{id, revision}` receipt with id echo, `_stub` marker); `uses_stub_transport` | correct URLs/headers/bodies; 400 raises with the message; deterministic stub shapes; an injected transport disables the stub flag |
| U-10 | test_config.py | both config files: flat manifest (no `agent:` block), dotted entry-point class, trust level, compile-time `requires`, `generation_mode`; runtime values read through the loader the code uses and forwarded to the inner graph | manifest keys at root; `requires.secrets == []` (the three `AZURE_OPENAI_*` keys deliberately excluded, mirroring `KINTONE_API_TOKEN`/`ANTHROPIC_API_KEY`) and `requires.extras == ["openai"]`; `generation_mode == "llm"`; `max_retry`/`timeout_s`/`kintone.base_url` load from `config/config.yaml` and arrive under `config["configurable"]` |
| U-11 | test_error_envelope_closed_set.py | **closed-set ERROR envelope** over every non-success path of post_process (existing ERROR; existing ERROR with credential-shaped log text; gate: missing evidence / nested credential value / nested credential KEY), through `execute()` and — where the framework wrapper allows — `node(state)`; a sentinel (a name, an email, a token-shaped fragment) seeded into `error_log` AND every output-bearing field; the same delta through `KintoneAppRecordAgent.get_output()`; the credential-shaped mapping key withheld from the violation label; the pre_process refusal line over the bad-value matrix × every contract alias; the unknown-intent reason | envelope is `{"reason": <code>}` with code ∈ `ERROR_REASONS`, truthy, every output-bearing field cleared; the sentinel appears nowhere in the returned mapping (keys and values walked); `error_log` not projected under any key and not re-emitted; `<withheld>` in the label, never the key; invoke body `output == {"reason": …}` with no `error_log` key; refusal = contract field + code ∈ `CALLER_FIELD_CODES`, value never echoed; the intent value is not echoed |
| U-11 | test_domain_workflow_graph.py | inner `KintoneWorkflowGraph`: identity, `_extra_initial_state()` kintone_config JSON injection, caller-contract seeding from the context bridge, `route()` error short-circuit, `get_output` contract, compile, direct inner invoke on the stub | name/state_schema correct; config forwarded as a JSON string; the stashed caller contract arrives as the inner `input_context`; error -> END; the inner invoke runs validate -> classify -> infer -> call -> confirm to SUCCESS with record evidence |
| U-12 | test_framework_compliance_tc06_tc07.py | the framework's final input/output gate methods cannot be overridden by a domain node | overriding either raises at class-definition time |
| U-13 | test_service.py | `extract_record_fields_via_llm()` (Step 8e): well-formed / prose-wrapped / malformed / wrong-shape / transport-failure responses, blank input never calls the LLM, long field name/value truncation; `resolve_llm()`: constructor-injected test double returned unchanged, bare state with no lifecycle fields, full state with no bound secret | valid responses parse and normalize; every invalid shape raises `LLMSynthesisError` (never returned as data); `resolve_llm()` never raises — returns `None` on any failure |

## Proof-of-Boundary Tests (`tests/proof_of_boundary/`)

| PB-ID | Boundary | Test | Expected |
|-------|----------|------|----------|
| PB-4 | Import isolation | test_import_isolation.py | AST scan of `src/`: no platform-SDK imports |
| PB-2/PB-5 | State serialization | test_state_safety.py | `state.py`: no Pydantic/credential fields |
| PB-6 | Backbone invoke-order + external-trust | test_pb_invoke_order.py | `_VALID_PAYLOAD` byte-equal to `deploy/invoke_payload.json` "input" (asserted); a VERIFIED_EXTERNAL caller yields `status=success` with `node_history == [InitializeNode, PreProcessNode, KintoneWorkflowGraphNode, PostProcessNode, FinalizeNode]` and record evidence + confirmation in `result["output"]`; an ANONYMOUS caller is denied at pre_process (error, no post_process, no output); blank input -> error, not a crash |
| PB-E2E | The public HTTP path | test_invoke_e2e.py | Through the real ASGI app: Bearer auth (401 without/with a wrong token); lookup, create and update paths return real record evidence; **a caller-supplied app/record id with no id in the text resolves the target, and the identical request without it cannot** (the bridge is load-bearing); the output tracks the input rather than a constant; declared runtime config reaches the running agent; the bounds matrix and the non-finite literals are refused over the wire; oversized / too-many-key envelopes are 413; control-token and caller-channel attacks refused while ordinary wording succeeds; an under-trusted caller denied; **containment** — a credential returned by the external system leaves an envelope with no released text, no traceback and no source paths; identifiers cross verbatim; SUCCESS always carries record evidence |
| PB-7 | HITL interrupt propagation *(conditional)* | test_pb7_hitl_interrupt_propagation.py | **Auto-waived — non-HITL** (`config/config.yaml` declares no `hitl.enabled: true`): module-level skipif; the stub bodies are real AssertionErrors, so enabling HITL without implementing PB-7 fails loudly |
| PB (boot) | Server entry point | test_server_boot.py | importing `src.api.server` does not raise (construct + compile + provision_secrets at import); the agent constructs and compiles via the supported path; `/invoke` + `/health` routes exposed |

> PB-1 (audit emission) is covered inside the unit suite via the emit-spy tests
> (pre_process / validate / classify / infer / call / confirm nodes assert on the
> event payload, `call.args[1]`). PB-3 (a live external service) is exercised at
> first invoke against a real tenant, not in this suite — the shipped transport
> is the documented network-free stub.

## Test Execution Summary

- Total tests: 465 collected, 3 skipped (PB-7 A/B — auto-waived, non-HITL; PB-5 — auto-waived, checkpointing disabled)
- Pass: 462 / Fail: 0
- Framework: `agenticstar-agentcore==1.0.3`
