# app/services/step_modifier.py

import ast
from typing import Optional, List, Tuple, Dict, Any
import logging
import re

from app.core.llm_executor import LLMExecutor

logger = logging.getLogger("step_modifier")


class StepModifier:
    """
    VERIFIER-DRIVEN STEP MODIFIER (MICRO-REPAIR)

    Guarantees:
    - Preserves step structure
    - Allows only expression-level edits
    - Never changes step count or order
    - NEVER modifies runtime fallback / dialog logic
    - Sandboxed against code injection
    """

    # --------------------------------------------------
    # HARD FORBIDDEN (STRUCTURAL / EXECUTION ESCAPES)
    # --------------------------------------------------
    _FORBIDDEN_PATTERNS = (
        r"\basync_playwright\b",
        r"\bbrowser\s*=",
        r"\bcontext\s*=",
        r"\bpage\s*=",
        r"\bpage\.goto\b",
        r"\bpage\.go_back\b",
        r"\bpage\.go_forward\b",
        r"\bpage\.evaluate\b",
        r"\bexec\b|\beval\b",
        r"__import__",
        r"\bopen\b|\blistdir\b",
        r"\blambda\b",
        r";",                      # multiple statements
        r"\\",                     # line continuation escape
        r"'''|\"\"\"",             # multiline strings
        r"^\s*import\s+",
        r"^\s*from\s+",
        r"\basync\s+def\b",
        r"^\s*def\b",
        r"^\s*class\b",
        r"\btry\s*:?|\bexcept\b|\bfinally\b",
        r"^\s*for\s+|\s+while\s+",
    )

    # Runtime / fallback logic — NEVER TOUCH
    _FALLBACK_PATTERNS = (
        r"\bpage\.once\(\s*['\"]dialog['\"]",
        r"\bd\.accept\(\)",
        r"\bd\.dismiss\(\)",
        r"\bhandle_dialog\b",
        r"\bcookie\b",
        r"\bpopup\b",
        r"\bmodal\b",
    )

    def __init__(self, llm: Optional[LLMExecutor] = None):
        self.llm = llm or LLMExecutor.get_instance()
        logger.warning("StepModifier initialized | mode=verifier_driven")

    # ==================================================
    # PUBLIC ENTRY
    # ==================================================

    async def modify(
        self,
        *,
        intent: str,
        generated_code: str,
        verifier_reason: str,
        error_message: Optional[str] = None,
        failure_history: Optional[list[str]] = None,
    ) -> str:

        original = (generated_code or "").strip()
        if not original:
            return original

        # --------------------------------------------------
        # HARD GUARD: DO NOT MODIFY FALLBACK / DIALOG CODE
        # --------------------------------------------------
        if self._contains_fallback_logic(original):
            logger.info(
                "STEP MODIFIER SKIPPED | reason=runtime_fallback_code"
            )
            return original

        if not verifier_reason or not verifier_reason.strip():
            logger.info(
                "STEP MODIFIER SKIPPED | reason=missing_verifier_feedback"
            )
            return original

        prompt = self._build_prompt(
            intent=intent,
            code=original,
            verifier_reason=verifier_reason,
            error_message=error_message,
            failure_history=failure_history,
        )

        try:
            llm_output = await self.llm.run_modifier(prompt)
        except Exception:
            logger.exception("STEP MODIFIER | LLM call failed")
            return original

        if not llm_output or not isinstance(llm_output, str):
            logger.info(
                "STEP MODIFIER NO-OP | reason=empty_llm_output"
            )
            return original

        sanitized = self._sanitize_llm_output(llm_output)
        if not sanitized:
            logger.warning(
                "STEP MODIFIER REJECTED | reason=sanitization_failed"
            )
            return original

        if not self._is_structurally_equivalent(original, sanitized):
            logger.info(
                "STEP MODIFIER REJECTED | reason=structural_mismatch\n"
                "----- ORIGINAL STEP -----\n%s\n"
                "----- MODIFIED STEP -----\n%s\n"
                "-------------------------",
                original,
                sanitized,
            )
            return original

        # NEW: deterministic literal-preservation check
        ok, reason = self._runtime_literals_preserved(original, sanitized)
        if not ok:
            logger.info(
                "STEP MODIFIER REJECTED | reason=literal_preservation_failed | details=%s",
                reason,
            )
            return original

        if sanitized == original:
            logger.info(
                "STEP MODIFIER NO-OP | reason=identical_code"
            )
            return original

        logger.warning(
            "STEP MODIFIER APPLIED | source=verifier_guided_llm"
        )
        return sanitized

    # ==================================================
    # PROMPT
    # ==================================================

    def _build_prompt(
        self,
        *,
        intent: str,
        code: str,
        verifier_reason: str,
        error_message: Optional[str],
        failure_history: Optional[list[str]],
    ) -> str:
        history_block = "N/A"

        if failure_history:
            formatted = []
            for idx, failed_code in enumerate(failure_history, start=1):
                formatted.append(f"Attempt {idx}:\n{failed_code}")
            history_block = "\n\n".join(formatted)

        # Prompt now explicitly instructs model not to change runtime data literals.
        return f"""
You are making a MINIMAL correction to Playwright Python STEP CODE
that was judged INCORRECT by a verifier.

This step has failed in previous attempts.

Error message:
{error_message or "N/A"}

Previous failed attempts:
{history_block}

CRITICAL DATA RULE (MANDATORY):
- DO NOT change any runtime data literals (string/number/boolean literals that affect runtime behavior).
  Examples: .fill('john'), .type("abc"), expect(...).to_have_text("Done"), page.goto("https://..."), select option literal values, cookie/header literal values.
- Locators/selectors (e.g., "input[name='username']") may be changed to fix selection problems.
- Allowed minor literal edits: single <-> double quotes and whitespace only.
- If you replace a literal with a variable, the variable MUST be assigned the EXACT SAME literal value in the same function body.

You MUST NOT reuse any locator strategy, selector pattern, role, visible text,
or interaction style used in ANY previous attempt.

You MUST generate a DIFFERENT locator strategy.
Do NOT reuse the same method.

STRICT RULES (NON-NEGOTIABLE):
- Do NOT add or remove lines
- Do NOT reorder lines
- ONLY edit existing expressions
- Do NOT introduce new locators
- Do NOT modify runtime fallback or dialog logic
- No imports
- No async def
- No comments
- One statement per line ONLY
- No Special Characters 

INTENT:
{intent}

VERIFIER FEEDBACK:
{verifier_reason}

CURRENT STEP BODY:
{code}

Return ONLY the corrected step body.
""".strip()

    # ==================================================
    # SANITIZATION
    # ==================================================

    def _sanitize_llm_output(self, text: str) -> Optional[str]:
        lines = []

        for line in text.splitlines():
            stripped = line.rstrip()

            if any(
                re.search(pattern, stripped, re.IGNORECASE)
                for pattern in self._FORBIDDEN_PATTERNS
            ):
                return None

            lines.append(stripped)

        result = "\n".join(lines).strip()
        return result or None

    # ==================================================
    # STRUCTURAL SAFETY (EXPRESSION-LEVEL ONLY)
    # ==================================================

    def _is_structurally_equivalent(
        self,
        original: str,
        candidate: str,
    ) -> bool:
        orig_lines = original.splitlines()
        cand_lines = candidate.splitlines()

        if len(orig_lines) != len(cand_lines):
            return False

        for o, c in zip(orig_lines, cand_lines):
            if not o.strip() or not c.strip():
                return False

            # Preserve indentation exactly
            if o[: len(o) - len(o.lstrip())] != c[: len(c) - len(c.lstrip())]:
                return False

            # Preserve left-hand side assignment only
            o_lhs = o.strip().split("=", 1)[0]
            c_lhs = c.strip().split("=", 1)[0]

            if o_lhs != c_lhs:
                return False

        return True

    # ==================================================
    # LITERAL PRESERVATION CHECK (DETERMINISTIC)
    # ==================================================

    def _runtime_literals_preserved(self, original: str, candidate: str) -> Tuple[bool, str]:
        """
        Ensure that runtime literals (strings/numbers/booleans) present per-statement
        in `original` are preserved in the corresponding `candidate` statement.

        Allowed:
        - Quote style changes
        - Whitespace differences
        - Replacing a literal with a variable only if that variable is assigned
          the exact same literal value elsewhere in the candidate function body.

        Returns (ok: bool, reason: str)
        """
        try:
            orig_nodes = self._parse_as_function_body_nodes(original)
            cand_nodes = self._parse_as_function_body_nodes(candidate)
        except Exception as exc:
            return False, f"ast_parse_failed: {exc}"

        if len(orig_nodes) != len(cand_nodes):
            return False, "statement_count_mismatch"

        # Build assignment maps (var -> literal value) for candidate so we can accept var references
        cand_assign_map = self._collect_constant_assignments(cand_nodes)

        for idx, (o_node, c_node) in enumerate(zip(orig_nodes, cand_nodes), start=1):
            o_consts = self._collect_constants_from_node(o_node)
            c_consts = self._collect_constants_from_node(c_node)

            # If original had no constants for that statement, nothing to preserve
            if not o_consts:
                continue

            # If candidate has constants, compare sequences (order-sensitive)
            if c_consts:
                if len(o_consts) != len(c_consts):
                    return False, f"statement_{idx}_const_count_mismatch"
                for o_val, c_val in zip(o_consts, c_consts):
                    if not self._constant_values_equal(o_val, c_val):
                        return False, f"statement_{idx}_const_value_mismatch: {o_val!r} -> {c_val!r}"
                continue

            # Candidate has no constants; maybe it used variables instead.
            # Collect variable names used in this candidate statement and see if any map to original constants.
            used_names = self._collect_names_from_node(c_node)
            if not used_names:
                return False, f"statement_{idx}_missing_literal_and_no_variable"

            # For each original constant position, check if there's a variable in candidate assigned same literal
            for o_val in o_consts:
                found_match = False
                for name in used_names:
                    if name in cand_assign_map and self._constant_values_equal(o_val, cand_assign_map[name]):
                        found_match = True
                        break
                if not found_match:
                    return False, f"statement_{idx}_literal_not_preserved_for_value:{o_val!r}"

        return True, "ok"

    def _parse_as_function_body_nodes(self, body_code: str) -> List[ast.stmt]:
        """
        Parse the code by wrapping it in an async function so we can access the body statements.
        """
        wrapper = "async def _x():\n" + self._indent(body_code)
        tree = ast.parse(wrapper)
        fn = tree.body[0]
        if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
            raise ValueError("wrapped node is not a function")
        return fn.body

    def _collect_constants_from_node(self, node: ast.AST) -> List[Any]:
        """
        Walk node and collect literal values (str, int, float, bool) in appearance order.
        """
        consts = []
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, (str, int, float, bool)):
                consts.append(child.value)
        return consts

    def _collect_names_from_node(self, node: ast.AST) -> List[str]:
        names = []
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                names.append(child.id)
        # prefer unique preserving order
        seen = set()
        ordered = []
        for n in names:
            if n not in seen:
                ordered.append(n)
                seen.add(n)
        return ordered

    def _collect_constant_assignments(self, nodes: List[ast.stmt]) -> Dict[str, Any]:
        """
        From a sequence of statements, collect any top-level assignments of constants
        like `username = 'john'` and return dict {varname: value}.
        """
        assigns: Dict[str, Any] = {}
        for node in nodes:
            # ast.Assign: targets (list) and value
            if isinstance(node, ast.Assign):
                val = node.value
                if isinstance(val, ast.Constant) and isinstance(val.value, (str, int, float, bool)):
                    # support simple single-target assignments
                    if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                        assigns[node.targets[0].id] = val.value
            # annotated assign
            if isinstance(node, ast.AnnAssign):
                target = node.target
                val = node.value
                if isinstance(target, ast.Name) and isinstance(val, ast.Constant) and isinstance(val.value, (str, int, float, bool)):
                    assigns[target.id] = val.value
        return assigns

    def _constant_values_equal(self, a: Any, b: Any) -> bool:
        """
        Semantic equality for runtime constants: exact match for strings/booleans/numbers.
        """
        return a == b

    # ==================================================
    # FALLBACK DETECTION
    # ==================================================

    def _contains_fallback_logic(self, code: str) -> bool:
        for pattern in self._FALLBACK_PATTERNS:
            if re.search(pattern, code, re.IGNORECASE):
                return True
        return False

    # ==================================================
    # FALLBACK DETECTION (END)
    # ==================================================

    def _is_structurally_equivalent_quiet(self, original: str, candidate: str) -> bool:
        # kept for debugging if needed externally
        return self._is_structurally_equivalent(original, candidate)

    # ==================================================
    # UTILS
    # ==================================================

    def _runtime_literal_tokens(self, code: str) -> List[str]:
        # convenience: not used in main flow but available for debugging
        try:
            nodes = self._parse_as_function_body_nodes(code)
        except Exception:
            return []
        tokens = []
        for n in nodes:
            tokens.extend([repr(v) for v in self._collect_constants_from_node(n)])
        return tokens

    def _indent(self, code: str) -> str:
        return "\n".join("    " + l if l.strip() else l for l in code.splitlines())