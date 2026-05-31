# app/services/validator.py

from typing import List, Iterable

from app.models.cir import (
    CIRBlock,
    CIRAction,
    ActionType,
    NavigateType,
    AssertionType,
    LocatorStrategy,
    DialogAction,
)


class CIRValidationError(Exception):
    """Raised when CIR violates non-recoverable invariants."""
    pass


class CIRValidator:
    """
    STEP-REPAIR CIR VALIDATOR.

    Purpose:
    - Enforce NON-RECOVERABLE invariants only
    - Allow placeholders for StepModifier to fix
    - Act as last hard gate before code generation

    Guarantees:
    - Never mutates CIR
    - Never calls LLM
    - Never invents data
    """

    # ==================================================
    # PUBLIC API
    # ==================================================

    def validate_blocks(self, blocks: Iterable[CIRBlock]) -> None:
        """
        Validate one or more CIRBlocks.
        """

        if isinstance(blocks, CIRBlock):
            blocks = [blocks]

        if not isinstance(blocks, Iterable):
            raise CIRValidationError("Blocks must be iterable")

        for block in blocks:
            if not isinstance(block, CIRBlock):
                raise CIRValidationError(
                    f"Invalid block type: {type(block)}"
                )

            # Empty blocks allowed
            if not block.actions:
                continue

            for action in block.actions:
                self._validate_action(action)

    # ==================================================
    # CORE VALIDATION
    # ==================================================

    def _validate_action(self, action: CIRAction) -> None:
        if not isinstance(action, CIRAction):
            raise CIRValidationError(
                f"Invalid action object: {type(action)}"
            )

        # -------------------------
        # ACTION TYPE (STRICT)
        # -------------------------
        if not isinstance(action.action_type, ActionType):
            raise CIRValidationError(
                f"Invalid action_type: {action.action_type}"
            )

        # -------------------------
        # TARGET / LOCATOR (STRICT VALUE)
        # -------------------------
        if action.target is not None:
            target = action.target

            if not isinstance(target.locator_strategy, LocatorStrategy):
                raise CIRValidationError(
                    f"Invalid locator strategy: {target.locator_strategy}"
                )

            if not isinstance(target.locator_value, str) or not target.locator_value.strip():
                raise CIRValidationError(
                    "Locator value must be a non-empty string"
                )

        # ==================================================
        # ACTION-SPECIFIC RULES
        # ==================================================

        # -------------------------
        # NAVIGATE
        # -------------------------
        if action.action_type == ActionType.navigate:
            nav_type = action.navigate_type or NavigateType.url

            if action.target is not None:
                raise CIRValidationError(
                    "Navigate action must NOT have a target"
                )

            if nav_type == NavigateType.url:
                if not action.value:
                    raise CIRValidationError(
                        "URL navigation requires value"
                    )
            else:
                if action.value is not None:
                    raise CIRValidationError(
                        f"{nav_type.value} navigation must NOT have value"
                    )

        # -------------------------
        # CLICK
        # -------------------------
        elif action.action_type == ActionType.click:
            if action.target is None:
                raise CIRValidationError(
                    "Click action requires a target"
                )
            if action.value is not None:
                raise CIRValidationError(
                    "Click action must NOT have value"
                )

        # -------------------------
        # TYPE
        # -------------------------
        elif action.action_type == ActionType.type:
            if action.target is None:
                raise CIRValidationError(
                    "Type action requires a target"
                )
            # value may be missing → modifier allowed

        # -------------------------
        # SELECT
        # -------------------------
        elif action.action_type == ActionType.select:
            if action.target is None:
                raise CIRValidationError(
                    "Select action requires a target"
                )
            if action.value is None:
                raise CIRValidationError(
                    "Select action requires a value"
                )

        # -------------------------
        # ASSERT
        # -------------------------
        elif action.action_type == ActionType.assert_action:
            if action.value is not None:
                raise CIRValidationError(
                    "Assert action must NOT have value"
                )

            if not action.assertion:
                raise CIRValidationError(
                    "Assert action requires assertion block"
                )

            assertion = action.assertion

            if not isinstance(assertion.assert_type, AssertionType):
                raise CIRValidationError(
                    f"Invalid assertion_type: {assertion.assert_type}"
                )

            at = assertion.assert_type

            if at == AssertionType.url_contains:
                if not assertion.expected_value:
                    raise CIRValidationError(
                        "url_contains assertion requires expected_value"
                    )
                if action.target is not None:
                    raise CIRValidationError(
                        "url_contains assertion must NOT have target"
                    )

            elif at == AssertionType.element_is_visible:
                if assertion.expected_value is not None:
                    raise CIRValidationError(
                        "element_is_visible must NOT have expected_value"
                    )
                # target may be missing → modifier allowed

            elif at in (
                AssertionType.text_contains,
                AssertionType.text_equals,
            ):
                # target/expected may be missing → modifier allowed
                pass

            else:
                raise CIRValidationError(
                    f"Unsupported assertion_type: {at}"
                )

        # -------------------------
        # HANDLE DIALOG
        # -------------------------
        elif action.action_type == ActionType.handle_dialog:
            if not action.dialog:
                raise CIRValidationError(
                    "handle_dialog action requires dialog block"
                )

            dialog = action.dialog

            if not isinstance(dialog.action, DialogAction):
                raise CIRValidationError(
                    f"Invalid dialog action: {dialog.action}"
                )

            if action.value is not None or action.assertion is not None:
                raise CIRValidationError(
                    "handle_dialog action must not have value or assertion"
                )

            # dialog.target may be None (JS alert)
            # or a valid locator (DOM popup)
            # BOTH ARE VALID

        # -------------------------
        # UNKNOWN ACTION
        # -------------------------
        else:
            raise CIRValidationError(
                f"Unsupported action_type: {action.action_type}"
            )
