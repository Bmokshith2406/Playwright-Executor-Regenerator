from __future__ import annotations

from typing import Iterable, Optional, Sequence

PROMPT_NOT_AVAILABLE = "N/A"

ALL_PROMPT_NAMES = (
    "health_llm_ping",
    "action_classifier",
    "extract_click",
    "extract_type",
    "extract_select",
    "extract_assert",
    "extract_dialog",
    "step_verifier",
    "step_modifier",
    "fallback_repair",
    "fallback_structure_retry",
    "repair_explanation",
)

HEALTHCHECK_LLM_PING_PROMPT = "ping"


def _normalize_text(value: Optional[str]) -> str:
    if value is None:
        return ""
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def clip_for_prompt(value: Optional[str], limit: int, default: str = PROMPT_NOT_AVAILABLE) -> str:
    text = _normalize_text(value)
    if not text:
        return default
    if len(text) <= limit:
        return text
    suffix = "... [truncated]"
    if limit <= len(suffix):
        return suffix[:limit]
    head = max(0, limit - len(suffix))
    return text[:head].rstrip() + suffix


def format_attempt_history(
    attempts: Optional[Sequence[str]],
    *,
    max_items: int = 3,
    item_limit: int = 350,
) -> str:
    if not attempts:
        return PROMPT_NOT_AVAILABLE

    blocks = []
    for idx, attempt in enumerate(attempts[:max_items], start=1):
        text = clip_for_prompt(attempt, item_limit, default="")
        if text:
            blocks.append(f"Attempt {idx}:\n{text}")

    return "\n\n".join(blocks) if blocks else PROMPT_NOT_AVAILABLE


def _section(title: str, value: Optional[str]) -> str:
    return f"{title}:\n{value or PROMPT_NOT_AVAILABLE}"


def _prompt(lines: Iterable[str]) -> str:
    parts = [part.strip() for part in lines if part and part.strip()]
    return "\n\n".join(parts)


def build_action_classifier_prompt(
    *,
    step_intent: str,
    original_code: str,
    error_type: Optional[str],
) -> str:
    return _prompt(
        [
            "Classify the failed Playwright step.",
            "Return exactly one word: navigate, click, type, select, assert, or dialog.",
            "\n".join(
                [
                    "Rules:",
                    "- Use a screenshot only to detect a blocking dialog, popup, modal, permission prompt, or overlay.",
                    "- Return assert only for explicit verification intent.",
                    "- If the step performs navigation, clicking, typing, or selecting, do not return assert.",
                    "- Prefer the intent over the code unless a dialog is clearly blocking execution.",
                    "- No explanation.",
                ]
            ),
            _section("Intent", clip_for_prompt(step_intent, 300)),
            _section("Code", clip_for_prompt(original_code, 700)),
            _section("Failure type", clip_for_prompt(error_type, 160)),
        ]
    )


def build_click_extractor_prompt(
    *,
    step_intent: str,
    original_code: str,
    error_message: str,
    dom_snapshot: Optional[str],
) -> str:
    return _prompt(
        [
            "Find the CLICK target for a failed Playwright step.",
            'Return one line only: none OR click:text("<exact visible text>").',
            "\n".join(
                [
                    "Rules:",
                    "- Preserve visible-text casing and spacing exactly.",
                    "- No CSS, XPath, or invented values.",
                ]
            ),
            _section("Intent", clip_for_prompt(step_intent, 260)),
            _section("Code", clip_for_prompt(original_code, 500)),
            _section("Error", clip_for_prompt(error_message, 300)),
            _section("DOM", clip_for_prompt(dom_snapshot, 700)),
        ]
    )


def build_type_extractor_prompt(
    *,
    step_intent: str,
    original_code: str,
    error_message: str,
    dom_snapshot: Optional[str],
) -> str:
    return _prompt(
        [
            "Find the TYPE target for a failed Playwright step.",
            'Return one line only: none, type:label("<label>") value("<kind>"), type:placeholder("<placeholder>") value("<kind>"), or type:role(textbox, name="<name>") value("<kind>").',
            "Allowed value kinds: email, username, password, text, number.",
            "\n".join(
                [
                    "Rules:",
                    "- No CSS, XPath, or invented literals.",
                    "- Preserve labels and placeholders exactly.",
                ]
            ),
            _section("Intent", clip_for_prompt(step_intent, 260)),
            _section("Code", clip_for_prompt(original_code, 500)),
            _section("Error", clip_for_prompt(error_message, 300)),
            _section("DOM", clip_for_prompt(dom_snapshot, 700)),
        ]
    )


def build_select_extractor_prompt(
    *,
    step_intent: str,
    original_code: str,
    error_message: str,
    dom_snapshot: Optional[str],
) -> str:
    return _prompt(
        [
            "Find the SELECT target for a failed Playwright step.",
            'Return one line only: none, select:text("<dropdown text>") value("<option text>"), or select:label("<label text>") value("<option text>").',
            "\n".join(
                [
                    "Rules:",
                    "- Preserve visible text exactly.",
                    "- No CSS, XPath, or invented values.",
                ]
            ),
            _section("Intent", clip_for_prompt(step_intent, 260)),
            _section("Code", clip_for_prompt(original_code, 500)),
            _section("Error", clip_for_prompt(error_message, 300)),
            _section("DOM", clip_for_prompt(dom_snapshot, 700)),
        ]
    )


def build_assert_extractor_prompt(
    *,
    step_intent: str,
    original_code: str,
    error_message: str,
    dom_snapshot: Optional[str],
) -> str:
    return _prompt(
        [
            "Find the ASSERT target for a failed Playwright assertion.",
            'Return one line only: none, url_contains:<fragment>, element_visible, or element_visible:text("<exact visible text>").',
            "\n".join(
                [
                    "Rules:",
                    "- Preserve visible text exactly.",
                    "- No CSS, XPath, or invented values.",
                ]
            ),
            _section("Intent", clip_for_prompt(step_intent, 260)),
            _section("Code", clip_for_prompt(original_code, 500)),
            _section("Error", clip_for_prompt(error_message, 300)),
            _section("DOM", clip_for_prompt(dom_snapshot, 700)),
        ]
    )


def build_dialog_extractor_prompt(
    *,
    step_intent: str,
    original_code: str,
    error_message: str,
    dom_snapshot: Optional[str],
) -> str:
    return _prompt(
        [
            "Detect a blocking runtime dialog or popup for the failed Playwright step.",
            'Return one line only: none, dialog:accept:text("<visible text>"), dialog:dismiss:text("<visible text>"), dialog:close:text("<visible text>"), dialog:accept:none, dialog:dismiss:none, or dialog:close:none.',
            "\n".join(
                [
                    "Rules:",
                    "- Focus on dialogs, popups, modals, and overlays only.",
                    "- Use exact visible text when present.",
                ]
            ),
            _section("Intent", clip_for_prompt(step_intent, 260)),
            _section("Code", clip_for_prompt(original_code, 500)),
            _section("Error", clip_for_prompt(error_message, 300)),
            _section("DOM", clip_for_prompt(dom_snapshot, 700)),
        ]
    )


def build_step_verifier_prompt(
    *,
    verification_mode: str,
    intent: str,
    matched_script: Optional[str],
    generated_code: str,
    error_message: Optional[str],
    failure_history: Optional[Sequence[str]],
) -> str:
    return _prompt(
        [
            "Verify whether GENERATED CODE satisfies the Playwright step intent.",
            'Return JSON only: {"verdict":"correct|incorrect","reason":"brief technical explanation"}.',
            "\n".join(
                [
                    "Rules:",
                    "- Ignore extra dialog/cookie fallback unless it breaks the intended step.",
                    "- Mark incorrect if the code repeats a locator, selector, text, role, or interaction pattern that already failed with not-found or visibility errors.",
                    "- Preserve runtime literals by semantic role: input values, expected assertion values, URLs, select values, cookie/header/payload values. Locator strings may change.",
                    "- Quote-style and whitespace-only literal differences are allowed.",
                    "- Variable substitution is allowed only if the variable carries the exact same literal value.",
                    "- Judge realistically: could this code succeed on the page?",
                    "- If the reference script is missing, say so in the reason.",
                ]
            ),
            _section("Mode", verification_mode),
            _section("Intent", clip_for_prompt(intent, 320)),
            _section("Error", clip_for_prompt(error_message, 500)),
            _section("Previous failed attempts", format_attempt_history(failure_history, max_items=3, item_limit=320)),
            _section("Reference script", clip_for_prompt(matched_script, 1800)),
            _section("Generated code", clip_for_prompt(generated_code, 1800)),
        ]
    )


def build_step_modifier_prompt(
    *,
    intent: str,
    code: str,
    verifier_reason: str,
    error_message: Optional[str],
    failure_history: Optional[Sequence[str]],
) -> str:
    return _prompt(
        [
            "Repair the Playwright step body with the smallest possible change.",
            "Return only the corrected step body.",
            "\n".join(
                [
                    "Rules:",
                    "- Keep the same line count, line order, indentation, and assignment targets.",
                    "- Edit expressions only; no imports, defs, classes, comments, or multi-statement lines.",
                    "- Do not touch dialog, popup, modal, cookie, or fallback handling.",
                    "- Do not reuse locator, selector, role, visible text, or interaction patterns from previous failed attempts.",
                    "- Preserve runtime literals exactly except for quote-style or whitespace-only changes.",
                    "- If a literal becomes a variable reference, the variable must carry the exact same literal value.",
                ]
            ),
            _section("Intent", clip_for_prompt(intent, 320)),
            _section("Verifier feedback", clip_for_prompt(verifier_reason, 700)),
            _section("Error", clip_for_prompt(error_message, 500)),
            _section("Previous failed attempts", format_attempt_history(failure_history, max_items=3, item_limit=280)),
            _section("Current step body", clip_for_prompt(code, 1800)),
        ]
    )


def build_fallback_repair_prompt(
    *,
    step_intent: str,
    current_code: str,
    error_text: str,
    dom_snapshot: Optional[str],
) -> str:
    return _prompt(
        [
            "Repair the failed Playwright step.",
            "Return Python function body code only. No function definition, markdown, comments, or imports.",
            "\n".join(
                [
                    "Rules:",
                    "- Keep the existing structure, step flow, intermediate variables, and waits unless one is clearly broken.",
                    "- Fix only the broken locator, wait, or interaction.",
                    "- Preserve runtime literals exactly: input values, expected values, URLs, select values, cookie/header/payload values.",
                    "- Locator strings may change. Quote-style or whitespace-only literal changes are allowed.",
                    "- Prefer role, text, or label locators and minimal waits.",
                    "- If a dialog or overlay blocks the step, you may dismiss or accept it.",
                    "- Do not add assertions unless the intent explicitly asks for one.",
                ]
            ),
            _section("Intent", clip_for_prompt(step_intent, 320)),
            _section("Current step code", clip_for_prompt(current_code, 1800)),
            _section("Error logs", clip_for_prompt(error_text, 700)),
            _section("DOM snapshot", clip_for_prompt(dom_snapshot, 900)),
        ]
    )


def build_fallback_structure_retry_prompt(
    *,
    step_intent: str,
    current_code: str,
    error_text: str,
) -> str:
    return _prompt(
        [
            "Your previous repair changed structure or runtime data. Rewrite it.",
            "Return Python function body code only.",
            "\n".join(
                [
                    "Rules:",
                    "- Keep the same step flow and intermediate variables.",
                    "- Do not collapse the code into one-liners or merge steps.",
                    "- Preserve runtime literals exactly.",
                    "- Change only the broken locator, wait, or interaction.",
                ]
            ),
            _section("Intent", clip_for_prompt(step_intent, 320)),
            _section("Current step code", clip_for_prompt(current_code, 1800)),
            _section("Error logs", clip_for_prompt(error_text, 700)),
        ]
    )


def build_repair_explanation_prompt(
    *,
    step_id: str,
    step_intent: str,
    original_code: str,
    repaired_code: str,
    error_text: str,
    dom_snapshot: Optional[str],
) -> str:
    return _prompt(
        [
            "Explain why an automated Playwright repair worked.",
            'Return JSON only with keys: failure_reason, repair_action, why_previous_failed, why_repair_passed, failure_type, summary.',
            "Allowed failure_type values: LOCATOR_CHANGE, ELEMENT_NOT_VISIBLE, DOM_CHANGE, TIMING_ISSUE, ASSERTION_CHANGE, UNKNOWN.",
            _section("Step ID", clip_for_prompt(step_id, 120)),
            _section("Step intent", clip_for_prompt(step_intent, 260)),
            _section("Original code", clip_for_prompt(original_code, 1200)),
            _section("Repaired code", clip_for_prompt(repaired_code, 1200)),
            _section("Error logs", clip_for_prompt(error_text, 600)),
            _section("DOM snapshot", clip_for_prompt(dom_snapshot, 800)),
        ]
    )


PROMPT_BUILDERS = {
    "action_classifier": build_action_classifier_prompt,
    "extract_click": build_click_extractor_prompt,
    "extract_type": build_type_extractor_prompt,
    "extract_select": build_select_extractor_prompt,
    "extract_assert": build_assert_extractor_prompt,
    "extract_dialog": build_dialog_extractor_prompt,
    "step_verifier": build_step_verifier_prompt,
    "step_modifier": build_step_modifier_prompt,
    "fallback_repair": build_fallback_repair_prompt,
    "fallback_structure_retry": build_fallback_structure_retry_prompt,
    "repair_explanation": build_repair_explanation_prompt,
}

STATIC_PROMPTS = {
    "health_llm_ping": HEALTHCHECK_LLM_PING_PROMPT,
}
