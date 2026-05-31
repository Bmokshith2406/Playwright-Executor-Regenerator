from typing import Optional
import logging

from app.core.llm_executor import LLMExecutor
from app.models.extraction import ExtractedLocator
from app.models.cir import LocatorStrategy
from app.core.dom_pruner import DomPruner
from app.services.extractors.BaseExtractor import BaseExtractor

logger = logging.getLogger("click_extractor")


class ClickActionExtractor(BaseExtractor):
    """
    CLICK locator evidence extractor for STEP REPAIR.
    """

    async def extract(
        self,
        *,
        step_intent: str,
        original_code: str,
        error_message: str,
        dom_snapshot: Optional[str],
        page_url: Optional[str],
        error_image_bytes: Optional[bytes] = None,
    ) -> Optional[ExtractedLocator]:

        logger.info(
            "CLICK EXTRACT | intent=%r | error=%r",
            step_intent,
            error_message,
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
            logger.debug("CLICK EXTRACT | LLM returned none")
            return None

        locator = self._normalize_llm_hint(llm_hint)

        if not locator:
            logger.warning(
                "CLICK EXTRACT | discarded LLM hint: %r",
                llm_hint,
            )

        return locator

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

        prompt = f"""Analyze FAILED Playwright step.
Identify CLICK action visible text (preserve casing/spacing exactly).
No CSS/XPath. No hallucinated/invented values.

Reply ONLY one of:
- none
- click:text("<EXACT visible text>")

Intent: {step_intent}
Code: {original_code}
Error: {error_message}
DOM: {pruned_dom or "N/A"}"""

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
    ) -> Optional[ExtractedLocator]:

        if not isinstance(text, str):
            return None

        raw = text.strip()
        lowered = raw.lower()

        if lowered == "none":
            return None

        if not lowered.startswith("click:"):
            return None

        hint_raw = raw.split(":", 1)[1].strip()
        hint_lower = hint_raw.lower()

        if hint_lower.startswith(("role(", "tag(")):
            logger.warning(
                "CLICK EXTRACT | unsupported hint variant: %s",
                hint_raw,
            )
            return None

        if hint_lower.startswith("text("):
            value = self._extract_quoted(hint_raw)
            if not value:
                return None

            if not self._literal_exists_in_sources(value):
                logger.warning(
                    "CLICK EXTRACT | rejected invented literal: %r",
                    value,
                )
                return None

            return ExtractedLocator(
                strategy=LocatorStrategy.text,
                value=value,
            )

        return None
