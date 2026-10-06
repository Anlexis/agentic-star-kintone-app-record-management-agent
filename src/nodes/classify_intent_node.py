"""AgentCore Platform v1.0 - inner workflow Step 2: ClassifyIntent.

Classifies the (redacted) request into one of lookup_record / create_record
/ update_record using a deterministic keyword heuristic, so the template is
testable and runnable without a language model. Low-confidence / unknown falls back to the
read-only "lookup_record" default with a note - never a write.
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_VALID_INTENTS = ("lookup_record", "create_record", "update_record")

# Deterministic keyword signals (checked in priority order, writes first so a
# "update the record then show it" style request classifies as the write).
_KEYWORDS = (
    (
        "update_record",
        (
            "update",
            "change",
            "edit",
            "correct",
            "amend",
            "revise",
            "modify",
            "set the",
            "set its",
            "mark the",
            "更新",
            "変更",
            "修正",
            "編集",
        ),
    ),
    (
        "create_record",
        (
            "create",
            "register",
            "add a record",
            "add a new",
            "new record",
            "insert",
            "open a record",
            "登録",
            "追加",
            "新規",
            "作成",
        ),
    ),
    (
        "lookup_record",
        (
            "look up",
            "lookup",
            "find",
            "show",
            "get",
            "fetch",
            "retrieve",
            "search",
            "what is",
            "summarize",
            "record for",
            "record of",
            "on file",
            "照会",
            "検索",
            "参照",
            "確認",
        ),
    ),
)


class ClassifyIntentNode(FunctionNode):
    """Classify the request into a kintone app-record operation intent."""

    # Inner domain node, read-only classification of already-redacted text -
    # the external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        text = state.get("validated_input", "") or ""
        if not text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClassifyIntentNode: missing validated_input"],
            }

        intent = self._classify_via_keywords(text)

        note: list[str] = []
        if intent not in _VALID_INTENTS:
            note = ["ClassifyIntentNode: low-confidence classification, " "defaulted to lookup_record (read-only)"]
            intent = "lookup_record"

        # Audit the classification decision - intent label only, never the text.
        emit_trace_event(
            "classify_intent_complete",
            {"intent": intent, "defaulted": bool(note)},
            state,
        )

        result: dict[str, Any] = {"intent": intent, "status": AgentStatus.SUCCESS.value}
        if note:
            result["error_log"] = note  # non-fatal note; status stays SUCCESS
        return result

    # -- classification -------------------------------------------------------

    def _classify_via_keywords(self, text: str) -> str:
        low = text.lower()
        for intent, words in _KEYWORDS:
            if any(w in low for w in words):
                return intent
        # No signal at all: fall through to the read-only default via the
        # _VALID_INTENTS guard in execute() (returns a sentinel outside the set).
        return "unknown"
