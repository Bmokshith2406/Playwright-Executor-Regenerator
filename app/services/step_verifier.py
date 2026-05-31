# app/services/step_verifier.py

from typing import Optional
import logging

from app.core.llm_executor import LLMExecutor
from app.core.llm_json import generate_json

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

        history_block = "N/A"

        if failure_history:
            formatted_history = []
            for idx, code in enumerate(failure_history, start=1):
                formatted_history.append(
                    f"Attempt {idx}:\n{code}"
                )
            history_block = "\n\n".join(formatted_history)

        prompt = f"""
        You are a Playwright Python STEP VERIFIER.

        Your task is to decide whether the GENERATED CODE
        correctly satisfies the given INTENT.

        IMPORTANT CONTEXT:
        - The generated code MAY include RUNTIME FALLBACK logic
        (e.g., accepting cookies, dismissing dialogs, closing popups).
        - Runtime fallback code exists ONLY to unblock execution.
        - Runtime fallback code does NOT need to satisfy intent.
        - Do NOT penalize correct fallback logic.

        RUNTIME CONTEXT:
        - This step has FAILED in previous attempts.
        - Error message:
        {error_message or "N/A"}

        - Previous failed attempts:
        {history_block}

        CRITICAL RULE (LOCATOR REUSE):
        If the generated code reuses the same locator strategy,
        same visible text, same role, or same selector pattern
        as ANY previous failed attempt that resulted in
        "element not found" or visibility failure,
        you MUST mark it as "incorrect".
        Minor formatting changes do NOT count as different.

        DATA INTEGRITY RULE (NEW — DYNAMIC, NOT HARDCODED):
        The verifier MUST ensure that **runtime data literals** are NOT changed
        by the generated code compared to the authoritative reference(s).

        Definitions:
        - "Runtime data literals" are string, numeric, or boolean literals
          that materially affect runtime behavior, including but not limited to:
          - arguments passed to user-input functions (e.g., .fill('john'), .type("abc"))
          - values assigned to variables used as inputs (e.g., username = 'john')
          - values used in assertions/expectations (e.g., expect(el).to_have_text("Done"))
          - navigation targets (e.g., page.goto("https://...")), select option values,
            cookies/headers literal values, and any literal used as expected content.
        - "Locators / selectors" (e.g., "input[name='usernamee']", CSS/XPath) are NOT
          considered runtime data literals for this rule and may be changed to fix
          selection issues.

        How to enforce (dynamic algorithm to follow — do NOT hardcode names/values):
        1. Extract the authoritative set of runtime literals:
           - If a MATCHED_SCRIPT is provided, extract literals and their semantic role
             from MATCHED_SCRIPT (preferred authoritative source).
           - Otherwise, if failure_history exists, extract literals from the most recent
             relevant failed attempt.
           - If neither exists, use the literals present in the GENERATED_CODE as the
             only available set (still enforce consistency between semantic roles if
             multiple literals appear).
        2. For each authoritative runtime literal, locate the corresponding usage in
           GENERATED_CODE and ensure the literal value is exactly preserved (semantic
           equality). Allow only trivial formatting differences:
           - single vs double quotes are OK,
           - whitespace differences are OK,
           - but any character/content change (e.g., 'john' -> 'jon' or 100 -> 200)
             is considered a substantive modification and MUST be marked "incorrect".
        3. Specifically verify matching by *semantic role*, not only text:
           - If MATCHED_SCRIPT passes 'john' to .fill() for the username, the
             GENERATED_CODE must pass the same literal to the same semantic role (an
             assignment used as the fill argument is acceptable if the assigned value
             is identical).
           - If the same literal appears in multiple roles (e.g., both an assert and a fill),
             ensure each role's literal is preserved.
        4. If there is any ambiguity whether a literal is a "label" vs "runtime value",
           treat it as runtime data unless the reference script clearly uses it as
           non-runtime label; explain this ambiguity explicitly in the "reason".
        5. Exceptions:
           - Changing locators/selectors to fix selection is allowed.
           - Changing only formatting, spacing, or quoting is allowed.
           - Any other change to runtime literals is NOT allowed and should trigger
             "incorrect" verdict per this Data Integrity Rule.

        GUIDELINES:
        - Judge realistically: could this code reasonably succeed on the page?
        - Extra or redundant actions are acceptable if they do not change behavior.
        - Do not mark incorrect just because fallback logic exists.
        - A locator does NOT need to match the reference exactly if it reliably targets the same element.
        - If focus is explicitly established before typing, using ':focus' or keyboard typing is acceptable.
        - If a runtime literal is replaced by a variable reference, ensure the variable's value
          is identical to the authoritative literal (explain how you inferred that).

        Verification mode: {verification_mode}

        How to judge:
        - INTENT_ONLY → use only the intent, error, generated code and history block.
        - INTENT_PLUS_REFERENCE → consider intent, reference script, and generated code.
        - Mention concrete mismatches or risks if they exist.
        - If the reference script is missing, explicitly state that.
        - Be precise and technical. Avoid vague statements.

        Respond ONLY in JSON.

        Output format:
        {{
        "verdict": "correct" | "incorrect",
        "reason": "clear, technical explanation (include which literal changed, where, and why that is incorrect)"
        }}

        INTENT:
        {intent}

        REFERENCE SCRIPT:
        {matched_script or "N/A"}

        GENERATED CODE:
        {generated_code}
        """.strip()

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