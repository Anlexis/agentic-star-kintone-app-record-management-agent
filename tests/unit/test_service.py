# CMN-C2-276 - Unit tests: src/services/service.py (Step 8e LLM extraction) and
# src/services/llm_resolver.py. Pure functions - no framework imports beyond
# the InvocationContext read inside resolve_llm(); no real network call.

import pytest

from framework.schemas.trust_level import TrustLevel
from src.services.llm_resolver import resolve_llm
from src.services.service import LLMSynthesisError, extract_record_fields_via_llm


class _FakeLLM:
    def __init__(self, content=None, raises=None):
        self._content = content
        self._raises = raises
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        if self._raises is not None:
            raise self._raises
        return {"content": self._content}


class TestExtractRecordFieldsViaLLM:
    def test_none_llm_returns_empty_result_without_calling_anything(self):
        result = extract_record_fields_via_llm("set priority to high", "create_record", None)
        assert result == {"record_title": None, "fields": {}}

    def test_blank_text_returns_empty_result_even_with_a_configured_llm(self):
        llm = _FakeLLM(content='{"record_title": "x", "fields": {}}')
        result = extract_record_fields_via_llm("   ", "create_record", llm)
        assert result == {"record_title": None, "fields": {}}
        assert not llm.calls  # never called for blank input

    def test_well_formed_response_parses(self):
        llm = _FakeLLM(content='{"record_title": "Kickoff", "fields": {"priority": "high", "count": 3}}')
        result = extract_record_fields_via_llm("set priority to high, count 3", "create_record", llm)
        assert result["record_title"] == "Kickoff"
        assert result["fields"] == {"priority": "high", "count": "3"}  # non-str values stringified

    def test_null_title_and_empty_fields_is_a_valid_no_signal_response(self):
        llm = _FakeLLM(content='{"record_title": null, "fields": {}}')
        result = extract_record_fields_via_llm("nothing extra here", "lookup_record", llm)
        assert result == {"record_title": None, "fields": {}}

    def test_prose_and_markdown_fence_wrapped_response_parses(self):
        llm = _FakeLLM(content='Here you go:\n```json\n{"record_title": "T", "fields": {"a": "b"}}\n```\nHope that helps.')
        result = extract_record_fields_via_llm("...", "create_record", llm)
        assert result == {"record_title": "T", "fields": {"a": "b"}}

    def test_unparseable_response_raises_llm_synthesis_error(self):
        llm = _FakeLLM(content="not json at all, sorry")
        with pytest.raises(LLMSynthesisError):
            extract_record_fields_via_llm("...", "create_record", llm)

    def test_wrong_type_title_raises(self):
        llm = _FakeLLM(content='{"record_title": 123, "fields": {}}')
        with pytest.raises(LLMSynthesisError):
            extract_record_fields_via_llm("...", "create_record", llm)

    def test_fields_not_an_object_raises(self):
        llm = _FakeLLM(content='{"record_title": null, "fields": ["a", "b"]}')
        with pytest.raises(LLMSynthesisError):
            extract_record_fields_via_llm("...", "create_record", llm)

    def test_field_entry_with_unsupported_value_type_raises(self):
        llm = _FakeLLM(content='{"record_title": null, "fields": {"a": {"nested": "object"}}}')
        with pytest.raises(LLMSynthesisError):
            extract_record_fields_via_llm("...", "create_record", llm)

    def test_transport_failure_raises_llm_synthesis_error(self):
        llm = _FakeLLM(raises=RuntimeError("upstream API error"))
        with pytest.raises(LLMSynthesisError):
            extract_record_fields_via_llm("...", "create_record", llm)

    def test_long_field_name_and_value_are_truncated(self):
        long_key = "k" * 200
        long_value = "v" * 1000
        llm = _FakeLLM(content='{"record_title": null, "fields": {"%s": "%s"}}' % (long_key, long_value))
        result = extract_record_fields_via_llm("...", "create_record", llm)
        (key, value), = result["fields"].items()
        assert len(key) == 100
        assert len(value) == 500


class TestResolveLLM:
    def test_constructor_injected_llm_is_returned_unchanged(self):
        sentinel = object()
        assert resolve_llm(sentinel, {}) is sentinel

    def test_bare_state_with_no_lifecycle_fields_returns_none_not_raise(self):
        # No session_id/thread_id/trace_id - InvocationContext.from_state()
        # indexes these and would KeyError; resolve_llm() must swallow it.
        assert resolve_llm(None, {"correlation_id": "x"}) is None

    def test_full_state_with_no_bound_secret_returns_none(self):
        state = {
            "correlation_id": "c",
            "session_id": "s",
            "thread_id": "t",
            "trace_id": "tr",
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
        }
        assert resolve_llm(None, state) is None
