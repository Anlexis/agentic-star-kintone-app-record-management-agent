"""AgentCore Platform v1.0 - inner workflow Step 3: InferKintoneFields.

Extracts the kintone app id, record number, record title, and "Key: value"
record fields from the (redacted) request and assembles a validated kintone
REST API request body for the classified intent. The app id and record number
are taken only from an explicit mention in the text or the caller-supplied
app_hint / record_hint - an unresolved id is left empty rather than invented
(risk mitigation: never touch the wrong app or record; the executor surfaces
the miss as status=error). This id resolution is deterministic in every case
- never LLM-derived.

record_title + custom record fields have an OPTIONAL LLM enhancement over the
regex baseline (Step 8e): the regex parser only catches literal "Key: value"
lines, so a request that phrases attributes in natural language ("set the
priority to high and mark it done") falls through to the LLM step. Any
resolution/call/parse failure - missing secret, API error, malformed response
- degrades silently to the regex-derived value; this node never raises or
sets status=ERROR because of the LLM step (src/services/service.py).
"""

import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json
from src.services.llm_resolver import resolve_llm
from src.services.service import LLMSynthesisError, extract_record_fields_via_llm

# A kintone app id / record number: a short numeric identifier.
_ID_SHAPE_RE = re.compile(r"^\d{1,9}$")
# Explicit app-id mention in the request text, EN or JA
# ("app 17" / "kintone app id: 17" / "アプリ 17").
_APP_IN_TEXT_RE = re.compile(
    r"\bapp\s*(?:id|no\.?|number)?\s*[:#]?\s*(\d{1,9})" r"|(?:アプリ)(?:ID|番号)?\s*[:：#]?\s*(\d{1,9})",
    re.IGNORECASE,
)
# Explicit record-number mention ("record 101" / "record no: 101" / "レコード番号 101").
_RECORD_IN_TEXT_RE = re.compile(
    r"\brecord\s*(?:id|no\.?|number)?\s*[:#]?\s*(\d{1,9})" r"|(?:レコード)(?:ID|番号)?\s*[:：#]?\s*(\d{1,9})",
    re.IGNORECASE,
)
# Quoted record title: titled "Foo" / named "Foo" / called "Foo". Curly quotes as
# \u escapes so the source stays pure ASCII (push-safe).
_TITLE_QUOTED_RE = re.compile(r'(?:titled|named|called|for)\s+["“]([^"”\n]+)["”]', re.IGNORECASE)
# "Key: value" record-field lines (ASCII or full-width colon). CJK ranges:
# hiragana/katakana + CJK unified ideographs, as \u escapes (push-safe).
_KV_RE = re.compile(r"^\s*([A-Za-z぀-ヿ一-鿿][\w \-぀-ヿ一-鿿]{0,40})[:：]\s*(.+?)\s*$")
# Keys that are the app / record id / title themselves, not custom record fields.
_APP_KEYS = ("app", "app id", "kintone app")
_RECORD_KEYS = ("record", "record id", "record no", "record number", "id")
_TITLE_KEYS = ("title", "record title", "name", "subject")


class InferKintoneFieldsNode(FunctionNode):
    """Extract entities and assemble the kintone REST API request body."""

    # Inner domain node - derives fields from already-validated text; the
    # external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def __init__(self, llm: Any = None) -> None:
        # llm= is a test-double seam only; production wiring (register_nodes()
        # in src/graph/domain_workflow_graph.py) never passes one - the real
        # client is resolved fresh per invocation in execute() below.
        super().__init__()
        self._llm = llm

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        text = state.get("validated_input", "") or ""
        intent = state.get("intent", "lookup_record") or "lookup_record"
        app_hint = state.get("app_hint", "") or ""
        record_hint = state.get("record_hint", "") or ""

        if not text.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InferKintoneFieldsNode: missing validated_input"],
            }

        fields = self._parse_fields(text)
        app_id = self._resolve_id(_APP_IN_TEXT_RE, text, app_hint, fields, _APP_KEYS)
        record_no = self._resolve_id(_RECORD_IN_TEXT_RE, text, record_hint, fields, _RECORD_KEYS)
        record_title = self._resolve_title(text, fields)

        # Optional LLM enhancement (Step 8e) - only for record_title/fields;
        # app_id/record_no above are already final and never touched here.
        llm_enhanced = False
        llm = resolve_llm(self._llm, state)
        try:
            llm_result = extract_record_fields_via_llm(text, intent, llm)
        except LLMSynthesisError:
            emit_trace_event(
                "kintone_field_llm_degraded",
                {"intent": intent},
                state,
            )
            llm_result = {"record_title": None, "fields": {}}
        if llm_result.get("record_title"):
            record_title = llm_result["record_title"]
            llm_enhanced = True
        if llm_result.get("fields"):
            fields = list(llm_result["fields"].items())
            llm_enhanced = True

        payload: dict[str, Any]
        if intent == "create_record":
            payload = {"app": app_id, "record": self._build_record_fields(record_title, fields)}
        elif intent == "update_record":
            payload = {
                "app": app_id,
                "id": record_no,
                "record": self._build_record_fields(record_title, fields),
            }
        else:  # lookup_record (read-only default)
            payload = {"app": app_id, "id": record_no}

        # Audit the assembled payload shape - field signals only, not content.
        emit_trace_event(
            "infer_kintone_fields_complete",
            {
                "intent": intent,
                "has_app_id": bool(app_id),
                "has_record_no": bool(record_no),
                "n_fields": len(fields),
                "llm_enhanced": llm_enhanced,
            },
            state,
        )

        return {
            "app_id": app_id,
            "record_title": record_title,
            "kintone_payload": to_json(payload),
            "status": AgentStatus.SUCCESS.value,
        }

    # -- extraction -----------------------------------------------------------

    def _resolve_id(
        self, text_re: "re.Pattern[str]", text: str, hint: str, fields: "list[tuple[str, str]]", keys: "tuple[str, ...]"
    ) -> str:
        """Explicit id only: text mention > id-shaped hint > 'Key:' field. Never invented."""
        m = text_re.search(text)
        if m:
            return m.group(1) or m.group(2) or ""
        hint = hint.strip()
        if hint and _ID_SHAPE_RE.match(hint):
            return hint
        for key, value in fields:
            if key.strip().lower() in keys and _ID_SHAPE_RE.match(value.strip()):
                return value.strip()
        return ""  # unresolved - left empty, never invented

    def _resolve_title(self, text: str, fields: "list[tuple[str, str]]") -> str:
        m = _TITLE_QUOTED_RE.search(text)
        if m:
            return m.group(1).strip()[:100]
        for key, value in fields:
            if key.strip().lower() in _TITLE_KEYS:
                return value.strip()[:100]
        return ""

    def _parse_fields(self, text: str) -> "list[tuple[str, str]]":
        """Return the [(key, value), ...] record fields parsed from the request lines."""
        fields: list[tuple[str, str]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = _KV_RE.match(stripped)
            if m:
                fields.append((m.group(1).strip(), m.group(2).strip()))
        return fields

    # -- payload assembly (kintone REST API record shape) -----------------------

    def _build_record_fields(self, record_title: str, fields: "list[tuple[str, str]]") -> "dict[str, Any]":
        """kintone record body: {field_code: {"value": ...}} (documented shape)."""
        record: dict[str, Any] = {}
        if record_title:
            record["title"] = {"value": record_title}
        for key, value in fields:
            if key.strip().lower() in _APP_KEYS + _RECORD_KEYS + _TITLE_KEYS:
                continue
            record[key.strip()] = {"value": value}
        return record
