# app/services/step_verifier.py

from typing import Optional
import logging

from app.core.llm_executor import LLMExecutor
from app.core.prompts import build_step_verifier_prompt

logger = logging.getLogger("step_verifier")


class StepVerificationResult(dict):
    """
    Dict subclass that supports both dict-style access and property access for .passed.
    """
    @property
    def passed(self) -> bool:
        return self.get("verdict") == "correct"


class StepVerifier:
    """
    PURE LLM-ONLY STEP VERIFIER

    Properties:
    - ZERO deterministic semantic checks
    - ZERO regex / static guards on meaning
    - LLM is the ONLY decision maker
    - Mandatory explanation for every verdict
    - Structural validation ONLY:
      - Ensures verdict format correctness
      - Ensures explanation presence
    """

    VERIFIER_VERSION = "LLM_ONLY_1.3"

    def __init__(self, llm: Optional[LLMExecutor] = None):
        self.llm = llm or LLMExecutor.get_instance()
        logger.warning("StepVerifier initialized | mode=LLM_ONLY")

    # ==================================================
    # PUBLIC ENTRY
    # ==================================================

    async def verify(
        self,
        generated_code: Optional[str] = None,
        intent: Optional[str] = None,
        matched_script: Optional[str] = None,
        *,
        error_message: Optional[str] = None,
        failure_history: Optional[list[str]] = None,
        **kwargs,
    ) -> StepVerificationResult:

        # Handle kwargs mappings for keyword-only parameters or aliases
        if "generated_code" in kwargs:
            generated_code = kwargs["generated_code"]
        if "intent" in kwargs:
            intent = kwargs["intent"]
        if "matched_script" in kwargs:
            matched_script = kwargs["matched_script"]
        if "error_message" in kwargs:
            error_message = kwargs["error_message"]
        if "failure_history" in kwargs:
            failure_history = kwargs["failure_history"]

        # Make sure they are not None
        intent = intent or ""
        generated_code = generated_code or ""

        verification_mode = (
            "INTENT_ONLY"
            if not matched_script or not matched_script.strip()
            else "INTENT_PLUS_REFERENCE"
        )

        logger.warning(
            "STEP VERIFIER START | mode=%s | version=%s",
            verification_mode,
            self.VERIFIER_VERSION,
        )

        prompt = build_step_verifier_prompt(
            verification_mode=verification_mode,
            intent=intent,
            matched_script=matched_script,
            generated_code=generated_code,
            error_message=error_message or "N/A",
            failure_history=failure_history,
        )

        import json
        from app.core.llm_json import _extract_json_block

        try:
            raw = await self.llm.run_verifier(prompt)
            logger.debug(
                "STEP VERIFIER RAW LLM RESPONSE | %r",
                raw,
            )
            if not raw:
                result = None
            else:
                json_text = _extract_json_block(raw, 5000)
                if not json_text:
                    raw_stripped = raw.strip().upper()
                    if raw_stripped in ("PASS", "CORRECT"):
                        result = {"verdict": "correct", "reason": "verified by raw pass response"}
                    elif raw_stripped.startswith("PASS:"):
                        result = {"verdict": "correct", "reason": raw[5:].strip()}
                    elif raw_stripped.startswith("FAIL:"):
                        result = {"verdict": "incorrect", "reason": raw[5:].strip()}
                    else:
                        result = {"verdict": "incorrect", "reason": raw.strip()}
                else:
                    result = json.loads(json_text)

        except Exception:
            logger.exception("LLM verification failed")
            return self._failure_response(
                reason="LLM verification failed",
                verification_mode=verification_mode,
            )

        if not isinstance(result, dict):
            logger.warning(
                "STEP VERIFIER INVALID RESPONSE TYPE | value=%r",
                result,
            )
            return self._failure_response(
                reason="invalid LLM response format",
                verification_mode=verification_mode,
            )

        verdict = result.get("verdict")
        reason = result.get("reason")

        if (
            verdict not in {"correct", "incorrect"}
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            logger.warning(
                "STEP VERIFIER MALFORMED RESPONSE | verdict=%r | reason=%r",
                verdict,
                reason,
            )
            return self._failure_response(
                reason="LLM response missing valid verdict or reason",
                verification_mode=verification_mode,
            )

        return StepVerificationResult({
            "verdict": verdict,
            "reason": reason.strip(),
            "verification_mode": verification_mode,
            "verifier_version": self.VERIFIER_VERSION,
        })

    # ==================================================
    # FAILURE RESPONSE (CENTRALIZED)
    # ==================================================

    def _failure_response(
        self,
        *,
        reason: str,
        verification_mode: str,
    ) -> StepVerificationResult:
        return StepVerificationResult({
            "verdict": "incorrect",
            "reason": reason,
            "verification_mode": verification_mode,
            "verifier_version": self.VERIFIER_VERSION,
        })

    # ==================================================
    # BACKWARD COMPAT
    # ==================================================

    async def verify_atomic(
        self,
        *,
        intent: str,
        generated_code: str,
        matched_script: Optional[str],
        error_message: Optional[str] = None,
        previous_failed_code: Optional[str] = None,
    ) -> dict:

        history = [previous_failed_code] if previous_failed_code else None

        return await self.verify(
            intent=intent,
            generated_code=generated_code,
            matched_script=matched_script,
            error_message=error_message,
            failure_history=history,
        )
