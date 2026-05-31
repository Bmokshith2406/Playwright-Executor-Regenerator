from typing import Optional, Tuple
import logging
import re

from app.core.llm_executor import LLMExecutor
from app.models.extraction import ExtractedLocator, ExtractedValue
from app.models.cir import LocatorStrategy
from app.core.dom_pruner import DomPruner
from app.services.extractors.BaseExtractor import BaseExtractor

logger = logging.getLogger("select_extractor")


class SelectActionExtractor(BaseExtractor):
    """
    SELECT action evidence extractor for STEP REPAIR.
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
    ) -> Tuple[Optional[ExtractedLocator], Optional[ExtractedValue]]:

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
            return None, None

        result = self._normalize_llm_hint(llm_hint)

        if result == (None, None):
            logger.warning(
                "SELECT EXTRACT | rejected LLM hint: %r",
                llm_hint,
            )

        return result

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

        prompt = f"""Analyze FAILED Playwright SELECT step.
Identify targeted dropdown and option text (preserve casing/spacing exactly).
No CSS/XPath. No invented values.

Reply ONLY one of:
- none
- select:text("<dropdown_text>") value("<option_text>")
- select:label("<label_text>") value("<option_text>")

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
    ) -> Tuple[Optional[ExtractedLocator], Optional[ExtractedValue]]:

        if not isinstance(text, str):
            return None, None

        raw = text.strip()
        lowered = raw.lower()

        if lowered == "none":
            return None, None

        if not lowered.startswith("select:"):
            return None, None

        value_part_match = re.search(r'value\s*\(\s*(["\']).*?\1\s*\)', raw, flags=re.DOTALL)
        if not value_part_match:
            loose = re.search(r'value\(["\']?(.*?)["\']?\)', raw)
            if not loose:
                return None, None
            option_text = loose.group(1)
        else:
            idx = raw.lower().rfind("value(")
            option_text = self._extract_quoted(raw[idx:])
            if option_text is None:
                m = re.search(r'value\(\s*["\'](.*?)["\']\s*\)', raw)
                if not m:
                    return None, None
                option_text = m.group(1)

        if option_text is None:
            return None, None

        if not self._literal_exists_in_sources(option_text):
            logger.warning(
                "SELECT EXTRACT | rejected invented option literal: %r",
                option_text,
            )
            return None, None

        value = ExtractedValue(value=option_text)

        parts = raw.rsplit("value(", 1)
        if not parts:
            return None, None
        locator_part = parts[0].strip()

        if locator_part.lower().startswith("select:text("):
            loc_text = self._extract_quoted(locator_part)
            if not loc_text:
                return None, None

            if not self._literal_exists_in_sources(loc_text):
                logger.warning(
                    "SELECT EXTRACT | rejected invented dropdown literal: %r",
                    loc_text,
                )
                return None, None

            locator = ExtractedLocator(
                strategy=LocatorStrategy.text,
                value=loc_text,
            )
            return locator, value

        if locator_part.lower().startswith("select:label("):
            loc_text = self._extract_quoted(locator_part)
            if not loc_text:
                return None, None

            if not self._literal_exists_in_sources(loc_text):
                logger.warning(
                    "SELECT EXTRACT | rejected invented dropdown literal: %r",
                    loc_text,
                )
                return None, None

            locator = ExtractedLocator(
                strategy=LocatorStrategy.label,
                value=loc_text,
            )
            return locator, value

        logger.warning(
            "SELECT EXTRACT | unsupported locator grammar: %s",
            locator_part,
        )
        return None, None
