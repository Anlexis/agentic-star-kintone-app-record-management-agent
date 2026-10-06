"""AgentCore Platform v1.0 - CMN-C2-276 domain service layer.

LLM-based record_title + custom record-field extraction (optional enhancement
over InferKintoneFieldsNode's deterministic regex baseline, Step 8e). No
agenticstar imports, no credentials, no side effects beyond the injected llm
collaborator.

Deliberately out of scope here: app_id / record_no. Those stay
regex/hint-derived only, in every case - see
src/nodes/infer_kintone_fields_node.py's own risk-mitigation note (an id must
never be invented; an unresolved one is left empty).
"""

from __future__ import annotations

from typing import Any

from shared.utils.llm_json import extract_json_object

_EMPTY_RESULT: dict[str, Any] = {"record_title": None, "fields": {}}
_MAX_FIELD_NAME = 100
_MAX_FIELD_VALUE = 500
_MAX_TITLE = 100


class LLMSynthesisError(Exception):
    """Raised when a *configured* LLM call fails, or returns an invalid/
    unparseable response. Callers (src/nodes/infer_kintone_fields_node.py)
    catch this and keep the regex-derived baseline unchanged - the LLM is an
    optional enhancement over an already-valid result (Step 8e), so neither a
    missing secret nor a live API/parse failure may surface as a node ERROR.
    """


def extract_record_fields_via_llm(text: str, intent: str, llm: Any) -> dict[str, Any]:
    """LLM-extract record_title + free-form "field: value" record attributes.

    Returns ``{"record_title": None, "fields": {}}`` (a pure no-op signal, the
    caller keeps its own baseline) when ``llm`` is None or ``text`` is blank -
    the LLM is never called for empty input. Raises LLMSynthesisError when
    ``llm`` is configured but the call fails, the response is not valid JSON,
    or the parsed shape does not match the documented contract.
    """
    if llm is None or not text.strip():
        return dict(_EMPTY_RESULT)

    prompt = (
        "Extract the kintone record title and any record attributes "
        '("field: value" style information) mentioned in the following '
        "business request. The request may phrase attributes in natural "
        'language rather than literal "Key: value" lines (e.g. "set the '
        'priority to high and mark it done" -> {"priority": "high", '
        '"status": "done"}).\n\n'
        "Respond with a JSON object of exactly this shape:\n"
        '{"record_title": <string or null>, "fields": {<field name>: '
        "<string value>, ...}}\n\n"
        "Do not include the app id or record number/id - those are resolved "
        "separately. Do not include any other text.\n\n"
        f"Intent: {intent}\n"
        f"Request: {text}"
    )
    try:
        raw = llm.complete([{"role": "user", "content": prompt}])
    except Exception as e:  # narrow provider/transport failure into a domain error
        raise LLMSynthesisError(f"LLM call failed: {e}") from e

    content = raw.get("content", "") if isinstance(raw, dict) else raw if isinstance(raw, str) else ""
    parsed = extract_json_object(content)
    if not parsed:
        raise LLMSynthesisError("LLM response was not valid/parseable JSON")
    return _validate_shape(parsed)


def _validate_shape(parsed: dict[str, Any]) -> dict[str, Any]:
    """Validate + normalize the parsed LLM response. Raises LLMSynthesisError
    on any shape violation - never surfaces an unvalidated response."""
    title = parsed.get("record_title")
    if title is not None and not isinstance(title, str):
        raise LLMSynthesisError(f"record_title was not a string or null: {title!r}")

    fields = parsed.get("fields", {})
    if not isinstance(fields, dict):
        raise LLMSynthesisError(f"fields was not a JSON object: {fields!r}")

    clean_fields: dict[str, str] = {}
    for key, value in fields.items():
        if not isinstance(key, str) or not isinstance(value, (str, int, float, bool)):
            raise LLMSynthesisError(f"fields entry has an unsupported key/value: {key!r}={value!r}")
        clean_key = key.strip()[:_MAX_FIELD_NAME]
        if clean_key:
            clean_fields[clean_key] = str(value).strip()[:_MAX_FIELD_VALUE]

    return {
        "record_title": title.strip()[:_MAX_TITLE] if title else None,
        "fields": clean_fields,
    }
