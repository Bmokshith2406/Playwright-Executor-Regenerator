import ast
import logging
import re
from typing import Optional

from app.core.llm_executor import LLMExecutor
from app.core.dom_pruner import DomPruner

logger = logging.getLogger("llm.fallback.repair")


class LLMFallbackRepairEngine:
    """
    Second-layer fallback repair engine.
    """

    def __init__(self):
        self.llm = LLMExecutor.get_instance()
        
    # maximum total chars to include from DOM in prompts
    DOM_SNIPPET_MAX_CHARS = 1200
    # how many characters around each match to include
    DOM_SNIPPET_WINDOW = 400

    def _extract_relevant_tokens_from_code(self, code: str) -> list[str]:
        """
        Extract a conservative set of tokens from step code that are likely to
        appear in the DOM and be relevant: quoted literals, common attribute values,
        and selector attribute pairs like [name='username'].
        """
        if not code:
            return []

        tokens = []
        # quoted strings (single or double)
        for m in re.finditer(r"(['\"])(?P<v>.*?)(?<!\\)\1", code):
            v = m.group("v").strip()
            if v and len(v) <= 200:  # avoid huge strings
                tokens.append(v)

        # attributes like name='username', id="foo", placeholder="Enter"
        for m in re.finditer(r"(?:name|id|placeholder|aria-label|title|value)\s*=\s*['\"]([^'\"]+)['\"]", code, re.IGNORECASE):
            v = m.group(1).strip()
            if v and len(v) <= 200:
                tokens.append(v)

        # CSS attribute selector patterns like [name='username']
        for m in re.finditer(r"\[\s*([a-zA-Z0-9_\-]+)\s*=\s*['\"]([^'\"]+)['\"]\s*\]", code):
            v = m.group(2).strip()
            if v and len(v) <= 200:
                tokens.append(v)

        # dedupe while preserving order
        seen = set()
        ordered = []
        for t in tokens:
            if t not in seen:
                ordered.append(t)
                seen.add(t)
        return ordered

    # ==================================================
    # PUBLIC API
    # ==================================================

    async def repair(
        self,
        *,
        step_intent: str,
        current_code: str,
        error_text: str,
        error_image_bytes: Optional[bytes] = None,
        dom_snapshot: Optional[str] = None,
    ) -> Optional[str]:

        guard_result = self._deterministic_fix(
            step_intent=step_intent,
            current_code=current_code,
            error_text=error_text,
        )
        if guard_result:
            logger.info("FALLBACK_DETERMINISTIC_FIX_APPLIED")
            return guard_result

        prompt = self._build_prompt(
            step_intent=step_intent,
            current_code=current_code,
            error_text=error_text,
            dom_snapshot=dom_snapshot,
        )
        
        try:
            if error_image_bytes:
                raw = await self.llm.run_multimodal_modifier(
                    prompt=prompt,
                    image_bytes=error_image_bytes,
                )
            else:
                raw = await self.llm.run_modifier(prompt)

        except Exception as exc:
            logger.error("FALLBACK LLM HARD FAILURE | err=%s", exc)
            return None

        # 🔍 LOG RAW OUTPUT
        logger.info("========== FALLBACK RAW OUTPUT START ==========")
        logger.info(raw)
        logger.info("=========== FALLBACK RAW OUTPUT END ==========")

        if not raw:
            logger.error("FALLBACK LLM RETURNED EMPTY")
            return None

        code = self._normalize_output(raw)

        # 🔍 LOG NORMALIZED OUTPUT
        logger.info("====== FALLBACK NORMALIZED OUTPUT START ======")
        logger.info(code)
        logger.info("======= FALLBACK NORMALIZED OUTPUT END =======")

        if not code:
            logger.error("FALLBACK NORMALIZATION FAILED")
            return None

        code = self._extract_body_only(code)

        # 🔍 LOG BODY EXTRACTION
        logger.info("====== FALLBACK BODY OUTPUT START ======")
        logger.info(code)
        logger.info("======= FALLBACK BODY OUTPUT END =======")

        if not code:
            logger.error("FALLBACK LLM OUTPUT NOT BODY-ONLY")
            return None

        # 🔒 STRUCTURE ENFORCEMENT WITH AUTO-REPROMPT
        if self._violates_structure(current_code, code):
            logger.warning("FALLBACK STRUCTURE VIOLATION — AUTO-REPROMPTING")

            retry_prompt = self._build_structure_violation_prompt(
                step_intent=step_intent,
                current_code=current_code,
                error_text=error_text,
            )

            try:
                retry_raw = await self.llm.run_modifier(retry_prompt)
            except Exception as exc:
                logger.error("FALLBACK RETRY FAILED | err=%s", exc)
                return None

            if not retry_raw:
                return None

            retry_code = self._normalize_output(retry_raw)
            if not retry_code:
                return None

            retry_code = self._extract_body_only(retry_code)
            if not retry_code:
                return None

            if self._violates_structure(current_code, retry_code):
                logger.error("FALLBACK RETRY STILL VIOLATES STRUCTURE — ABORTING")
                return None

            if not self._is_valid_python_body(retry_code):
                return None

            return retry_code

        # Normal validation
        if not self._is_valid_python_body(code):
            logger.error("FALLBACK LLM RETURNED INVALID PYTHON BODY")
            return None

        return code

    # ==================================================
    # DETERMINISTIC FIXES
    # ==================================================

    def _deterministic_fix(
        self,
        *,
        step_intent: str,
        current_code: str,
        error_text: str,
    ) -> Optional[str]:

        text = (error_text or "").lower()

        if "timeout" in text:
            return self._add_visibility_wait(current_code)

        return None

    def _add_visibility_wait(self, code: str) -> Optional[str]:
        if "expect(" in code:
            return None

        lines = code.splitlines()
        for i, line in enumerate(lines):
            if ".click(" in line or ".fill(" in line:
                indent = re.match(r"\s*", line).group(0)
                locator_expr = line.strip().split(".")[0]
                wait_line = f"{indent}await expect({locator_expr}).to_be_visible()"
                return "\n".join(lines[:i] + [wait_line] + lines[i:])

        return None

    # ==================================================
    # PROMPT
    # ==================================================

    def _build_prompt(
        self,
        *,
        step_intent: str,
        current_code: str,
        error_text: str,
        dom_snapshot: Optional[str],
    ) -> str:
        # Prune DOM to only the relevant parts (or first N chars)
        keyword = None

        # Try extracting quoted literal from step intent first
        from app.core.utils import extract_quoted
        keyword = extract_quoted(step_intent)

        # Fallback to extracting from code if intent has none
        if not keyword:
            tokens = self._extract_relevant_tokens_from_code(current_code)
            keyword = tokens[0] if tokens else None

        pruned_dom = DomPruner.prune(dom_snapshot, keyword)
        dom_block = pruned_dom if pruned_dom else "N/A"

        # Updated prompt: Enforce preservation of runtime data literals dynamically.
        return f"""
    You are an expert Playwright test automation engineer.

    You MUST return ONLY the function body.
    NOT the function definition.
    NOT explanations.
    NOT markdown.
    NOT comments.
    NOT imports.


CRITICAL STRUCTURE RULES (MANDATORY):
- You MUST preserve the overall structure of the existing code.
- You MAY change locators, waits, and interaction methods.
- You MUST keep the same variable flow and shape (e.g., locator → target → wait → action).
- Do NOT collapse multiple steps into one unless unavoidable.
- Do NOT introduce a completely different style unless the current one is invalid.
- If the current code uses intermediate variables, KEEP them.
- If the current code uses explicit waits, KEEP them (but you may modify them).
- NEVER change any USERNAME, PASSWORD, credential variables, or hardcoded authentication values if explicitly present in the code or step intent.

DATA INTEGRITY RULE (MANDATORY — DYNAMIC, NOT HARDCODED):
- DO NOT change any runtime data literals. "Runtime data literals" include string, numeric, or boolean literals that materially affect runtime behavior, such as:
  * arguments passed to user-input functions (e.g., .fill('john'), .type(\"abc\"))
  * values assigned to variables used as inputs (e.g., username = 'john')
  * expected values used in assertions (e.g., expect(el).to_have_text(\"Done\"))
  * navigation URLs passed to page.goto("https://...")
  * option values passed to select/choose, cookie/header literal values, or JSON payload literals
- "Locators / selectors" (e.g., "input[name='usernamee']", CSS/XPath) ARE NOT runtime data literals and MAY be changed to fix selection issues.
- Allowed minor edits to literals: changing single ↔ double quotes, whitespace differences. Any content change (character, digit, or word) is prohibited.

HOW TO COMPLY (dynamic algorithm — implement by extraction/comparison):
1. Prefer authoritative source for literals:
   - If the current code contains literals used at runtime, treat those as authoritative.
   - If the step_intent explicitly provides values, treat those as authoritative.
   - If there is an earlier failed attempt and it contains literals, prefer the most recent authoritative attempt.
2. Extract runtime literals and their *semantic roles* (e.g., fill-argument for username, value for expect assertion, page.goto target).
3. In your repaired code, preserve the exact literal content for every authoritative runtime literal when used in the same semantic role.
   - Replacing a literal with a variable is allowed only if that variable is assigned the identical literal value within the same function body.
4. If preserving the literal exactly would prevent a working repair (e.g., a locator must change), prioritize changing locators/waits rather than changing the literal content.
5. If a literal appears in multiple roles, preserve it in all roles.
6. If you are uncertain whether a particular token is a runtime literal or a non-runtime label, treat it as a runtime literal and preserve it (explainability is not allowed in the response body — but the repair must keep the literal).

OTHER RULES:
- Preserve original intent
- Preserve the structural pattern of the code
- Fix only what is broken
- Use Playwright best practices
- Prefer role/text/label-based locators
- Add or adjust waits only if needed
- Handle dialogs or overlays if blocking
- NEVER add assertions unless explicitly asked
- NEVER remove variables unless required for correctness
- DONT TOUCH ACTUAL VALUES IN THE CODE

Step intent:
{step_intent}

Current step code:
{current_code}

Error logs:
{error_text}

DOM snapshot (pruned to relevant snippets if available):
{dom_block}

Return ONLY valid Python body code.
""".strip()

    def _build_structure_violation_prompt(
        self,
        *,
        step_intent: str,
        current_code: str,
        error_text: str,
    ) -> str:
        # Retry prompt: insist on preserved structure and preserved runtime literals.
        return f"""
Your previous output VIOLATED the required code structure or changed runtime data.

You MUST rewrite the code while STRICTLY preserving its structure AND preserving all runtime data literals.

STRUCTURE RULES (MANDATORY):
- Keep the same number of steps
- Keep intermediate variables (e.g., locator, target)
- Keep the same flow shape
- Do NOT collapse into one-liners
- Only fix what is broken

DATA INTEGRITY RULE (MANDATORY):
- Do NOT change any runtime data literals (string/number/boolean values that affect behavior).
- You MAY change locators, waits, and interaction techniques, but the actual runtime literal values must remain identical (allowed minor changes: quotes, whitespace).
- If you replace a literal with a variable, ensure the variable is assigned the exact same literal value in the function body.

Step intent:
{step_intent}

Current step code (structure to preserve):
{current_code}

Error logs:
{error_text}

Return ONLY the corrected Python function body.
""".strip()

    # ==================================================
    # OUTPUT NORMALIZATION
    # ==================================================

    def _normalize_output(self, raw: str) -> Optional[str]:
        """
        Strip fences, leading explanatory lines, and collapse excessive blank lines.
        Be conservative about removing content.
        """
        if not raw:
            return None

        text = raw.strip()

        # Remove code fences (``` or ```python) and any leading/trailing fence markers
        # Use DOTALL to be robust if LLM included fenced blocks with interior newlines.
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)

        # Remove common "assistant prose" prefixes that precede code
        text = re.sub(r"(?im)^(here is(?: the)? output|below(?: is)?|sure[:,\s]*|output[:]?\s*|answer[:]?\s*|this is the code[:\s-]*)\s*", "", text)

        # Remove lines that are obviously commentary or short English sentences at the top
        lines = text.splitlines()
        clean_lines = []
        skip_prefix_block = True
        for line in lines:
            stripped = line.strip()
            # stop skipping when we see a likely code line (starts with await, locator, indent, or def/async)
            if skip_prefix_block and re.match(r"^(await\b|locator\b|page\.|def\b|async\b|import\b|from\b|await\s+page\.|locator\s*=)", stripped):
                skip_prefix_block = False
            if skip_prefix_block:
                # if line is very short english prose, skip it; otherwise keep (be conservative)
                if len(stripped) < 60 and re.match(r"^[A-Za-z0-9 ,.'\"-]{0,60}$", stripped):
                    continue
            clean_lines.append(line)
        text = "\n".join(clean_lines).strip()

        if not text:
            return None

        # Normalize CRLF and collapse more than 2 consecutive blank lines
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text)

        # Remove a global extra indentation (LLM often returns code indented as if inside a function)
        text = self._dedent(text)

        return text

    def _extract_body_only(self, code: str) -> Optional[str]:
        """
        If code is a function definition, extract its body. If parsing fails,
        still try to ensure body indentation is suitable for insertion as a function body.
        """
        if not code:
            return None

        try:
            tree = ast.parse(code)
        except Exception:
            # If parse fails, return code but ensure its indentation is normalized for body usage
            return self._ensure_body_indentation(code)

        # If single top-level function def, extract its body
        if len(tree.body) == 1 and isinstance(tree.body[0], (ast.AsyncFunctionDef, ast.FunctionDef)):
            fn = tree.body[0]
            body_lines = code.splitlines()
            start = fn.body[0].lineno - 1
            extracted = "\n".join(body_lines[start:])
            return self._ensure_body_indentation(self._dedent(extracted))

        # Otherwise treat the entire code as the body block; ensure indentation is correct
        return self._ensure_body_indentation(self._dedent(code))

    def _ensure_body_indentation(self, code: str) -> str:
        """
        Ensure body block is consistently indented and parseable when wrapped in:
            async def _x():\n<INDENTED BLOCK>
        Strategy:
         - If every non-empty line already starts with some indent, keep relative indents but
           make the minimum indent zero (dedent) then re-indent uniformly by 4 spaces.
         - If some lines are flush-left (col 0) and others are indented (mixed), force uniform indent:
           dedent lines that have extra indent, then indent whole block by 4 spaces.
        """
        if not code:
            return code

        lines = code.splitlines()
        non_empty = [l for l in lines if l.strip()]
        if not non_empty:
            return code

        # compute indent of each non-empty line
        indents = [len(l) - len(l.lstrip()) for l in non_empty]
        min_indent = min(indents)
        max_indent = max(indents)

        # If all lines already have zero indent (fine) -> just indent by 4 spaces when used as body
        # If lines have mixed indentation (some zero, some >0), normalize:
        if min_indent == 0 and max_indent > 0:
            # dedent by min positive indent among lines that have indent > 0
            positive_indents = [i for i in indents if i > 0]
            dedent_by = min(positive_indents) if positive_indents else 0
            if dedent_by > 0:
                normalized = []
                for l in lines:
                    if l.strip():
                        if len(l) - len(l.lstrip()) >= dedent_by:
                            normalized.append(l[dedent_by:])
                        else:
                            normalized.append(l.lstrip())
                    else:
                        normalized.append("")
                lines = normalized

        # Final step: produce a consistently-indented block (4 spaces)
        result = []
        for l in lines:
            if l.strip():
                result.append("    " + l.lstrip())
            else:
                result.append("")
        return "\n".join(result).rstrip()  # strip trailing whitespace

    def _dedent(self, code: str) -> str:
        """
        Slightly stricter dedent: compute minimal indent among non-empty lines and remove it.
        If minimal indent is 0 but many lines have indent, don't remove theirs (the ensure step will handle).
        """
        lines = code.splitlines()
        non_empty = [l for l in lines if l.strip()]
        if not non_empty:
            return code

        # compute min indent but ignore a single leading unindented header line if followed by indented lines
        indents = [len(l) - len(l.lstrip()) for l in non_empty]
        min_indent = min(indents)

        # If min_indent == 0 but many lines are indented, keep as-is (we'll normalize later)
        if min_indent == 0 and any(i > 0 for i in indents):
            return "\n".join(lines)

        return "\n".join(l[min_indent:] if len(l) >= min_indent else l for l in lines)

    def _is_valid_python_body(self, code: str) -> bool:
        """
        Try a few attempts to ensure validity:
         - wrap with async def and simple indent
         - if that fails, try ensuring uniform body indentation and retry
        """
        if not code:
            return False

        # 1) naive attempt: indent and parse
        try:
            ast.parse("async def _x():\n" + self._indent(code))
            return True
        except SyntaxError:
            pass

        # 2) attempt after enforcing uniform body indentation
        try:
            normalized = self._ensure_body_indentation(code)
            ast.parse("async def _x():\n" + normalized)
            return True
        except SyntaxError:
            pass

        return False

    # ==================================================
    # VALIDATION
    # ==================================================

    def _violates_structure(self, original: str, generated: str) -> bool:
        orig_lines = [l.strip() for l in original.splitlines() if l.strip()]
        gen_lines = [l.strip() for l in generated.splitlines() if l.strip()]

        # If original had multiple steps, do not allow collapse
        if len(orig_lines) >= 3 and len(gen_lines) == 1:
            return True

        # Preserve intermediate variables
        for var in ["locator =", "target ="]:
            if var in original and var not in generated:
                return True

        # Preserve waits/assertions if they existed
        if "expect(" in original and "expect(" not in generated:
            return True

        if "wait_for(" in original and "wait_for(" not in generated:
            return True

        return False

    def _indent(self, code: str) -> str:
        return "\n".join("    " + l if l.strip() else l for l in code.splitlines())