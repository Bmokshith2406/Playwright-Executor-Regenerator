from __future__ import annotations

import json
import logging
import re
import textwrap
from typing import Optional, Dict, Any

from app.core.llm_executor import LLMExecutor
from app.core.dom_pruner import DomPruner


__all__ = ["RepairExplanationService"]


logger = logging.getLogger("llm.repair.explainer")


class RepairExplanationService:
    """Generates a human-readable explanation of why an automated Playwright repair worked.

    The class maintains the original public API and flow:
      - _build_prompt builds a textual prompt
      - generate_explanation calls the LLM (multimodal if an image was provided)
      - _safe_parse_json attempts to extract a single JSON object from the model output

    Improvements made (non-exhaustive):
      - better input validation and explicit constructor parameters
      - safer logging (truncate large LLM outputs at debug level)
      - robust JSON extraction using a regex that finds the first JSON object or array
      - preservation of return types and error behaviour (returns None on failure)
    """

    DEFAULT_DOM_SNIPPET_MAX_CHARS: int = 1200

    FAILURE_TYPE_ENUM = (
        "LOCATOR_CHANGE",
        "ELEMENT_NOT_VISIBLE",
        "DOM_CHANGE",
        "TIMING_ISSUE",
        "ASSERTION_CHANGE",
        "UNKNOWN",
    )

    def __init__(
        self,
        llm: Optional[LLMExecutor] = None,
        dom_snippet_max_chars: Optional[int] = None,
    ) -> None:
        """Initialize the service.

        Args:
            llm: Optional LLMExecutor instance. If omitted, the singleton is used.
            dom_snippet_max_chars: Max chars to include from the pruned DOM in the prompt.
        """
        self.llm = llm or LLMExecutor.get_instance()
        if dom_snippet_max_chars is None:
            self.DOM_SNIPPET_MAX_CHARS = self.DEFAULT_DOM_SNIPPET_MAX_CHARS
        else:
            self.DOM_SNIPPET_MAX_CHARS = int(dom_snippet_max_chars)

    # ==================================================
    # PUBLIC API
    # ==================================================

    async def generate_explanation(
        self,
        *,
        step_id: str,
        step_intent: str,
        original_code: str,
        repaired_code: str,
        error_text: str,
        dom_snapshot: Optional[str] = None,
        error_image_bytes: Optional[bytes] = None,
    ) -> Optional[Dict[str, Any]]:
        """Generate a JSON explanation of why a repair fixed a Playwright step.

        Returns:
            A dict if parsing succeeds, otherwise None.
        """
        # Input validation - keep this light so behaviour matches previous implementation
        step_id = (step_id or "").strip()
        step_intent = (step_intent or "").strip()

        prompt = self._build_prompt(
            step_id=step_id,
            step_intent=step_intent,
            original_code=original_code or "",
            repaired_code=repaired_code or "",
            error_text=error_text or "",
            dom_snapshot=dom_snapshot,
        )

        try:
            if error_image_bytes:
                raw = await self.llm.run_multimodal_modifier(
                    prompt=prompt,
                    image_bytes=error_image_bytes,
                )
            else:
                # keep same method name used previously (run_modifier)
                raw = await self.llm.run_modifier(prompt)
        except Exception:
            # Preserve behaviour of returning None on LLM failure, but record exception details
            logger.exception("EXPLANATION LLM FAILURE for step_id=%s", step_id)
            return None

        if not raw:
            # Nothing to parse
            logger.debug("LLM returned empty response for step_id=%s", step_id)
            return None

        # Avoid logging arbitrarily large strings at info level in production
        truncated = raw if len(raw) <= 2000 else raw[:2000] + "...<truncated>"
        logger.debug("REPAIR EXPLANATION RAW OUTPUT (truncated) = %s", truncated)

        parsed = self._safe_parse_json(raw)

        if not parsed:
            logger.error("EXPLANATION JSON PARSE FAILED for step_id=%s", step_id)
            return None

        # Optional additional validation: ensure required keys exist (do not alter behaviour on failure)
        required_keys = {
            "failure_reason",
            "repair_action",
            "why_previous_failed",
            "why_repair_passed",
            "failure_type",
            "summary",
        }

        if not required_keys.issubset(parsed.keys()):
            logger.warning(
                "Parsed JSON for step_id=%s is missing expected keys: missing=%s",
                step_id,
                required_keys.difference(parsed.keys()),
            )

        # Ensure failure_type is one of allowed values; otherwise leave as-is (don't change structure)
        failure_type = parsed.get("failure_type")
        if failure_type and failure_type not in self.FAILURE_TYPE_ENUM:
            logger.debug(
                "Parsed failure_type '%s' not in known enum for step_id=%s",
                failure_type,
                step_id,
            )

        return parsed

    # ==================================================
    # PROMPT
    # ==================================================

    def _build_prompt(
        self,
        *,
        step_id: str,
        step_intent: str,
        original_code: str,
        repaired_code: str,
        error_text: str,
        dom_snapshot: Optional[str],
    ) -> str:
        """Construct the prompt sent to the LLM.

        This keeps the same schema and content as the original, while making
        the prompt string creation clearer and safer to read in code.
        """
        pruned_dom = DomPruner.prune(dom_snapshot, None) if dom_snapshot else None
        dom_block = (
            (pruned_dom[: self.DOM_SNIPPET_MAX_CHARS] if pruned_dom else "N/A")
        )

        # Use textwrap.dedent to avoid accidental leading spaces
        prompt = textwrap.dedent(f"""
        You are an expert Playwright test debugging analyst.

        A Playwright automation step failed and was automatically repaired.

        Your job is to explain WHY the failure happened and WHY the repair fixed it.

        Return ONLY valid JSON.

        Schema:

        {{
          "failure_reason": "Short explanation of why the original step failed",
          "repair_action": "What change the repair introduced",
          "why_previous_failed": "Detailed reasoning of the failure",
          "why_repair_passed": "Detailed reasoning why the repaired code works",
          "failure_type": "One of: LOCATOR_CHANGE, ELEMENT_NOT_VISIBLE, DOM_CHANGE, TIMING_ISSUE, ASSERTION_CHANGE, UNKNOWN",
          "summary": "One concise sentence summarizing the repair"
        }}

        Step ID:
        {step_id}

        Step intent:
        {step_intent}

        Original code:
        {original_code}

        Repaired code:
        {repaired_code}

        Error logs:
        {error_text}

        DOM snapshot (pruned):
        {dom_block}

        Return ONLY JSON.
        """)

        return prompt.strip()

    # ==================================================
    # UTILITIES
    # ==================================================

    def _safe_parse_json(self, text: str) -> Optional[Dict[str, Any]]:
        """Robustly parse a JSON object from free-form LLM output.

        Strategy:
          1. Strip common markdown fences and surrounding text.
          2. Use a regex to find the first JSON object or array substring.
          3. Attempt to json.loads it. If that fails, return None.

        This is intentionally conservative: we attempt to find the *first* JSON-like
        substring rather than eagerly concatenating multiple matches.
        """
        if not text:
            return None

        s = text.strip()

        # Remove surrounding ``` fences and an optional leading language marker
        if s.startswith("```") and s.endswith("```"):
            # Drop the outer fences
            s = s[3:-3].strip()
            # If the fence had a language like ```json, drop it from the start
            s = re.sub(r"^\s*json\s*", "", s, flags=re.IGNORECASE)

        # Look for the first balanced JSON object or array using regex.
        # This regex finds a substring that starts with { or [, and ends with the corresponding } or ].
        # We keep it simple — find the longest match starting from the first brace/bracket.
        match = re.search(r"([\{\[][\s\S]*[\}\]])", s)
        if not match:
            # No JSON-like substring found; fall back to trying a direct json.loads
            try:
                return json.loads(s)
            except Exception:
                logger.debug("No JSON-like substring found and direct json.loads failed")
                return None

        candidate = match.group(1)

        # Attempt to load the candidate. If it fails, try to perform minor fixes
        # (e.g., remove trailing commas) but do not attempt heavy-handed corrections.
        try:
            return json.loads(candidate)
        except Exception:
            # Try to remove trailing commas before closing braces/brackets, a
            # common LLM formatting error.
            sanitized = re.sub(r",\s*(\}|\])", r"\1", candidate)
            try:
                return json.loads(sanitized)
            except Exception:
                logger.debug("Candidate JSON parse failed after sanitization")
                return None


