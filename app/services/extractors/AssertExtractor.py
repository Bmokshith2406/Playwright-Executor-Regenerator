from typing import Optional
import re
import logging

from app.core.llm_executor import LLMExecutor
from app.models.extraction import ExtractedAssertion, ExtractedLocator
from app.models.cir import AssertionType, LocatorStrategy, StepWait
from app.core.dom_pruner import DomPruner
from app.core.prompts import build_assert_extractor_prompt
from app.services.extractors.BaseExtractor import BaseExtractor

logger = logging.getLogger("assert_extractor")


class AssertActionExtractor(BaseExtractor):
    """
    Assertion evidence extractor for STEP REPAIR.
    """

    async def extract(
        self,
        *,
        step_intent: str,
        original_code: str,
        error_message: str,
        dom_snapshot: Optional[str] = None,
        page_url: Optional[str] = None,
        error_image_bytes: Optional[bytes] = None,
    ) -> Optional[ExtractedAssertion]:

        logger.debug("ASSERT EXTRACT | intent=%r", step_intent)

        intent_text = step_intent.lower()

        # URL-based assertion (deterministic, non-LLM)
        if page_url and "url" in intent_text:
            fragment = self._url_fragment(page_url)
            if fragment:
                logger.debug(
                    "ASSERT EXTRACT | url fragment detected: %s",
                    fragment,
                )
                return ExtractedAssertion(
                    type=AssertionType.url_contains,
                    expected=fragment,
                    locator=None,
                    wait=StepWait(),
                )

        self._last_step_intent = step_intent or ""
        self._last_original_code = original_code or ""
        self._last_dom_snapshot = dom_snapshot or ""

        llm_hint = await self._ask_llm(
            step_intent=step_intent,
            original_code=original_code,
            error_message=error_message,
            dom_snapshot=dom_snapshot,
            error_image_bytes=error_image_bytes,
        )

        if not llm_hint:
            logger.debug("ASSERT EXTRACT | LLM returned none")
            return None

        assertion = self._normalize_llm_hint(llm_hint)

        if not assertion:
            logger.warning(
                "ASSERT EXTRACT | discarded LLM hint: %r",
                llm_hint,
            )

        return assertion

    async def _ask_llm(
        self,
        *,
        step_intent: str,
        original_code: str,
        error_message: str,
        dom_snapshot: Optional[str],
        error_image_bytes: Optional[bytes],
    ) -> Optional[str]:

        keyword = self._extract_quoted(step_intent)
        if not keyword:
            keyword = self._extract_quoted(original_code)

        pruned_dom = DomPruner.prune(dom_snapshot, keyword)
        self._last_dom_snapshot = pruned_dom or ""

        prompt = build_assert_extractor_prompt(
            step_intent=step_intent,
            original_code=original_code,
            error_message=error_message,
            dom_snapshot=pruned_dom,
        )

        executor = LLMExecutor.get_instance()

        if error_image_bytes:
            return await executor.run_multimodal_extractor(
                prompt=prompt,
                image_bytes=error_image_bytes,
            )

        return await executor.run_extractor(prompt)

    def _normalize_llm_hint(
        self,
        text: str,
    ) -> Optional[ExtractedAssertion]:

        raw = text.strip()
        lowered = raw.lower()

        if lowered == "none":
            return None

        if lowered.startswith("url_contains:"):
            value = raw.split(":", 1)[1].strip()
            if not value:
                return None

            return ExtractedAssertion(
                type=AssertionType.url_contains,
                expected=value,
                locator=None,
                wait=StepWait(),
            )

        if lowered == "element_visible":
            return ExtractedAssertion(
                type=AssertionType.element_is_visible,
                expected=None,
                locator=None,
                wait=StepWait(),
            )

        if lowered.startswith("element_visible:"):
            hint_raw = raw.split(":", 1)[1].strip()
            hint_lower = hint_raw.lower()

            if hint_lower.startswith(("role(", "tag(")):
                logger.warning(
                    "ASSERT EXTRACT | unsupported hint variant: %s",
                    hint_raw,
                )
                return None

            value = self._extract_quoted(hint_raw)
            if not value:
                return None

            return ExtractedAssertion(
                type=AssertionType.element_is_visible,
                expected=None,
                locator=ExtractedLocator(
                    strategy=LocatorStrategy.text,
                    value=value,
                ),
                wait=StepWait(),
            )

        return None

    def _url_fragment(self, url: str) -> Optional[str]:
        try:
            path = re.sub(r"https?://[^/]+", "", url)
            segments = path.split("?", 1)[0].strip("/").split("/")
            return segments[-1] if segments and segments[-1] else None
        except Exception:
            return None
