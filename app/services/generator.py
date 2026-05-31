# app/services/step_code_generator.py

from typing import List, Optional
import re

from app.models.cir import (
    CIRAction,
    ActionType,
    NavigateType,
    AssertionType,
    LocatorStrategy,
    DialogAction,
)

EXPECT_TIMEOUT = 5000


class StepCodeGenerator:
    """
    PURE step-level Playwright code generator.

    INPUT:
    - CIRAction (validated + repaired)

    OUTPUT:
    - List[str] → raw Playwright code lines
    - NO imports
    - NO async def

    HARD GUARANTEES:
    - Deterministic output
    - No side effects
    - CIR contract strictly enforced
    """

    # ==================================================
    # PUBLIC API
    # ==================================================

    def generate(
        self,
        action: CIRAction,
        *,
        original_lines: Optional[List[str]] = None,
    ) -> List[str]:

        if not isinstance(action, CIRAction):
            raise RuntimeError("Invalid CIRAction")

        match action.action_type:
            case ActionType.click:
                return self._click(action)

            case ActionType.type:
                return self._type(action)

            case ActionType.select:
                return self._select(action)

            case ActionType.navigate:
                return self._navigate(action)

            case ActionType.assert_action:
                return self._assert(action)

            case ActionType.handle_dialog:
                return self._handle_dialog(
                    action,
                    original_lines=original_lines or [],
                )

        # Exhaustive match – any new ActionType MUST be handled above
        raise RuntimeError(f"Unsupported action type: {action.action_type}")

    # ==================================================
    # ACTION RENDERERS
    # ==================================================

    def _click(self, action: CIRAction) -> List[str]:
        self._require_target(action, "CLICK")

        loc = self._locator(action)
        lines = self._visible_target(loc)
        lines.append("await target.click()")
        return lines

    def _type(self, action: CIRAction) -> List[str]:
        if action.value is None:
            raise RuntimeError("TYPE action missing value")

        # Keyboard typing (no locator)
        if not action.target:
            return [
                f"await page.keyboard.type({repr(str(action.value))})",
            ]

        loc = self._locator(action)
        lines = self._visible_target(loc)
        lines.append(f"await target.fill({repr(str(action.value))})")
        return lines

    def _select(self, action: CIRAction) -> List[str]:
        self._require_target(action, "SELECT")

        if action.value is None:
            raise RuntimeError("SELECT action missing value")

        loc = self._locator(action)
        lines = self._visible_target(loc)
        lines.append(f"await target.select_option({repr(str(action.value))})")
        return lines

    def _navigate(self, action: CIRAction) -> List[str]:
        nav_type = action.navigate_type or NavigateType.url

        if nav_type == NavigateType.url:
            if not action.value:
                raise RuntimeError("Navigate URL missing")
            return [
                f"await page.goto({repr(str(action.value))}, wait_until='domcontentloaded')"
            ]

        if nav_type == NavigateType.back:
            return ["await page.go_back()"]

        if nav_type == NavigateType.forward:
            return ["await page.go_forward()"]

        return ["await page.reload()"]

    def _assert(self, action: CIRAction) -> List[str]:
        assertion = action.assertion
        if not assertion:
            raise RuntimeError("ASSERT action missing assertion block")

        lines: List[str] = []

        if action.target:
            loc = self._locator(action)
            lines.extend([
                f"locator = {loc}",
                "target = locator.first",
            ])
            target = "target"
        else:
            target = "page"

        val = assertion.expected_value

        match assertion.assert_type:
            case AssertionType.text_equals:
                lines.append(
                    f"await expect({target}).to_have_text({repr(str(val))}, timeout={EXPECT_TIMEOUT})"
                )

            case AssertionType.text_contains:
                lines.append(
                    f"await expect({target}).to_contain_text({repr(str(val))}, timeout={EXPECT_TIMEOUT})"
                )

            case AssertionType.url_contains:
                # IMPORTANT:
                # Use regex string literal to avoid requiring imports
                pattern = re.escape(str(val))
                lines.append(
                    f"await expect(page).to_have_url(r'.*{pattern}.*')"
                )

            case AssertionType.element_is_visible:
                lines.append(
                    f"await expect({target}).to_be_visible(timeout={EXPECT_TIMEOUT})"
                )

            case _:
                raise RuntimeError(
                    f"Unsupported assertion type: {assertion.assert_type}"
                )

        return lines

    # ==================================================
    # HANDLE RUNTIME DIALOG (FALLBACK)
    # ==================================================

    def _handle_dialog(
        self,
        action: CIRAction,
        *,
        original_lines: List[str],
    ) -> List[str]:

        if not action.dialog:
            raise RuntimeError("HANDLE_DIALOG action missing dialog block")

        dialog = action.dialog
        fallback_lines: List[str] = []

        # --------------------------------------------------
        # Case 1: JavaScript dialogs (alert / confirm / prompt)
        # --------------------------------------------------
        if dialog.target is None:
            match dialog.action:
                case DialogAction.accept:
                    fallback_lines.append(
                        "page.once('dialog', lambda d: d.accept())"
                    )
                case DialogAction.dismiss | DialogAction.close:
                    fallback_lines.append(
                        "page.once('dialog', lambda d: d.dismiss())"
                    )

            # IMPORTANT:
            # JS dialog handlers MUST be registered BEFORE triggering actions
            return [
                *fallback_lines,
                *original_lines,
            ]

        # --------------------------------------------------
        # Case 2: DOM-based dialogs (cookie banner / modal)
        # --------------------------------------------------
        loc = self._locator(
            CIRAction(
                action_type=ActionType.click,
                target=dialog.target,
            )
        )

        fallback_lines.extend(self._visible_target(loc))
        fallback_lines.append("await target.click()")

        # Prepend dialog resolution before original action
        return [
            *fallback_lines,
            *original_lines,
        ]

    # ==================================================
    # LOCATOR RENDERING (CIR-CONTRACT SAFE)
    # ==================================================

    def _locator(self, action: CIRAction) -> str:
        loc = action.target
        if not loc:
            raise RuntimeError("Missing locator")

        value = loc.locator_value
        strategy = loc.locator_strategy

        match strategy:
            case LocatorStrategy.css | LocatorStrategy.xpath:
                return f"page.locator({repr(value)})"

            case LocatorStrategy.text:
                return f"page.get_by_text({repr(value)})"

            case LocatorStrategy.role:
                # NOTE: name/attributes can be added later if CIR expands
                return f"page.get_by_role({repr(value)})"

            case LocatorStrategy.label:
                return f"page.get_by_label({repr(value)})"

            case LocatorStrategy.placeholder:
                return f"page.get_by_placeholder({repr(value)})"

            case LocatorStrategy.test_id:
                return f"page.get_by_test_id({repr(value)})"

        raise RuntimeError(f"Unsupported locator strategy: {strategy}")

    # ==================================================
    # INTERNAL HELPERS (PURE / SAFE)
    # ==================================================

    def _visible_target(self, loc: str) -> List[str]:
        """
        Canonical pattern:
        - Resolve locator
        - Use first match
        - Assert visibility before interaction
        """
        return [
            f"locator = {loc}",
            "target = locator.first",
            f"await expect(target).to_be_visible(timeout={EXPECT_TIMEOUT})",
        ]

    def _require_target(self, action: CIRAction, name: str) -> None:
        if not action.target:
            raise RuntimeError(f"{name} action missing target locator")
