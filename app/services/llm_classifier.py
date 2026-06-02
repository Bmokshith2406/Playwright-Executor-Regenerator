# app/services/llm_classifier.py

from typing import Optional
import re

from app.core.llm_executor import LLMExecutor
from app.core.prompts import build_action_classifier_prompt
from app.models.cir import ActionType


class LLMActionClassifier:
    """
    Fully LLM-based action classifier.

    The LLM is the semantic authority, but we add
    deterministic safety guards to prevent false ASSERTS.

    Screenshot (if present) is used ONLY to detect
    runtime dialogs, popups, modals, or blocking overlays.
    """

    ALLOWED_LABELS = {
        "navigate": ActionType.navigate,
        "click": ActionType.click,
        "type": ActionType.type,
        "select": ActionType.select,
        "assert": ActionType.assert_action,

        # Runtime interruption
        "dialog": ActionType.handle_dialog,
        "popup": ActionType.handle_dialog,
        "modal": ActionType.handle_dialog,
    }

    ASSERT_PATTERNS = [
        r"\bverify\b",
        r"\bcheck\b",
        r"\bensure\b",
        r"\bconfirm\b",
        r"\bvalidate\b",
        r"\bassert\b",
        r"\bshould\b",
        r"\bmust\b",
        r"\bexpect\b",
    ]

    CLICK_PATTERNS = [
        r"\bclick\b",
        r"\bpress\b",
        r"\btap\b",
        r"\bsubmit\b",
        r"\bopen\b",
    ]

    TYPE_PATTERNS = [
        r"\btype\b",
        r"\benter\b",
        r"\bfill\b",
        r"\binput\b",
    ]

    SELECT_PATTERNS = [
        r"\bselect\b",
        r"\bchoose\b",
        r"\bpick\b",
    ]

    NAVIGATE_PATTERNS = [
        r"\bnavigate\b",
        r"\bgo to\b",
        r"\bopen url\b",
        r"\bvisit\b",
        r"\bload\b",
    ]

    def __init__(self):
        self.llm = LLMExecutor.get_instance()

    def _matches(self, patterns, text: str) -> bool:
        text = text.lower()
        return any(re.search(p, text) for p in patterns)

    def _looks_like_assert(self, intent: str) -> bool:
        return self._matches(self.ASSERT_PATTERNS, intent)

    async def classify(
        self,
        *,
        step_intent: str,
        original_code: str,
        error_type: Optional[str],
        error_image_bytes: Optional[bytes] = None,
    ) -> ActionType:
        """
        Classify the failed step into a SINGLE ActionType.
        """

        intent_lower = step_intent.lower().strip()

        # ---------------------------
        # HARD GUARDS (NO LLM)
        # ---------------------------
        if self._matches(self.CLICK_PATTERNS, intent_lower):
            return ActionType.click

        if self._matches(self.TYPE_PATTERNS, intent_lower):
            return ActionType.type

        if self._matches(self.SELECT_PATTERNS, intent_lower):
            return ActionType.select

        if self._matches(self.NAVIGATE_PATTERNS, intent_lower):
            return ActionType.navigate

        if self._matches(self.ASSERT_PATTERNS, intent_lower):
            return ActionType.assert_action

        # ---------------------------
        # LLM FALLBACK (AMBIGUOUS)
        # ---------------------------

        prompt = build_action_classifier_prompt(
            step_intent=step_intent,
            original_code=original_code,
            error_type=error_type,
        )

        try:
            if error_image_bytes:
                result = await self.llm.run_multimodal_classifier(
                    prompt=prompt,
                    image_bytes=error_image_bytes,
                )
            else:
                result = await self.llm.run_classifier(prompt)

        except Exception:
            return ActionType.click  # SAFE DEFAULT

        if not result:
            return ActionType.click  # SAFE DEFAULT

        label = re.sub(
            r"[^a-z]",
            "",
            result.lower().strip().split()[0],
        )

        action = self.ALLOWED_LABELS.get(label, ActionType.click)

        # ---------------------------
        # ASSERT SAFETY DOWNGRADE
        # ---------------------------
        if action == ActionType.assert_action:
            if not self._looks_like_assert(step_intent):
                return ActionType.click

        return action
