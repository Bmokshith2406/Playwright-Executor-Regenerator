# app/models/cir.py

from pydantic import BaseModel, Field, model_validator
from typing import List, Optional, Any
from enum import Enum


# =========================
# ENUMS
# =========================

class ActionType(str, Enum):
    navigate = "navigate"
    click = "click"
    type = "type"
    select = "select"
    assert_action = "assert"
    handle_dialog = "handle_dialog"


class NavigateType(str, Enum):
    url = "url"
    back = "back"
    forward = "forward"
    refresh = "refresh"


class LocatorStrategy(str, Enum):
    id = "id"
    name = "name"
    css = "css"
    xpath = "xpath"
    class_name = "class"
    tag = "tag"
    text = "text"
    role = "role"
    test_id = "test_id"
    placeholder = "placeholder"
    label = "label"


class WaitCondition(str, Enum):
    visible = "visible"
    hidden = "hidden"
    attached = "attached"
    detached = "detached"
    url_contains = "url_contains"


class AssertionType(str, Enum):
    text_equals = "text_equals"
    text_contains = "text_contains"
    element_is_visible = "element_is_visible"
    url_contains = "url_contains"


class CIRBlockType(str, Enum):
    setup = "setup"
    step = "step"
    fallback = "fallback"
    teardown = "teardown"


class DialogAction(str, Enum):
    accept = "accept"
    dismiss = "dismiss"
    close = "close"


# =========================
# CORE MODELS
# =========================

class CIRLocator(BaseModel):
    locator_strategy: LocatorStrategy
    locator_value: str = Field(..., min_length=1)


class CIRWait(BaseModel):
    condition: WaitCondition
    timeout: int = Field(default=15, ge=1, le=60)


class StepWait(BaseModel):
    """
    Required by extraction layer.
    DO NOT REMOVE.
    """
    condition: WaitCondition = WaitCondition.visible
    timeout_seconds: int = Field(default=15, ge=1, le=60)


class CIRAssertion(BaseModel):
    assert_type: AssertionType
    expected_value: Optional[str] = None

    @model_validator(mode="after")
    def validate_expected_value(self):
        if self.assert_type in {
            AssertionType.text_equals,
            AssertionType.text_contains,
            AssertionType.url_contains,
        } and not self.expected_value:
            raise ValueError(
                f"expected_value required for assertion type {self.assert_type}"
            )
        return self


# =========================
# DIALOG MODEL
# =========================

class CIRDialog(BaseModel):
    """
    Represents a runtime interruption (cookie banner, alert, modal).
    """
    action: DialogAction
    target: Optional[CIRLocator] = None


# =========================
# ACTION MODEL
# =========================

class CIRAction(BaseModel):
    action_type: ActionType

    # Shared / optional
    target: Optional[CIRLocator] = None
    locator: Optional[str] = None
    description: Optional[str] = None
    value: Optional[str] = None
    wait: Optional[CIRWait] = None

    # Navigate
    navigate_type: Optional[NavigateType] = None

    # Assert
    assertion: Optional[CIRAssertion] = None

    # Dialog
    dialog: Optional[CIRDialog] = None

    @model_validator(mode="before")
    @classmethod
    def populate_target_and_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            locator = data.get("locator")
            target = data.get("target")
            
            if locator and not target:
                strategy = LocatorStrategy.css
                val = locator
                if isinstance(locator, str):
                    if locator.startswith("text="):
                        strategy = LocatorStrategy.text
                        val = locator[5:].strip("'\"")
                    elif locator.startswith("xpath="):
                        strategy = LocatorStrategy.xpath
                        val = locator[6:]
                    elif locator.startswith("id="):
                        strategy = LocatorStrategy.id
                        val = locator[3:]
                data["target"] = {
                    "locator_strategy": strategy,
                    "locator_value": val
                }
            elif target and not locator:
                if isinstance(target, dict):
                    data["locator"] = target.get("locator_value")
                elif hasattr(target, "locator_value"):
                    data["locator"] = target.locator_value
        return data

    @model_validator(mode="after")
    def validate_action_semantics(self):
        # -------------------------
        # NAVIGATE
        # -------------------------
        if self.action_type == ActionType.navigate:
            if not self.navigate_type:
                raise ValueError("navigate_type required for navigate action")

        # -------------------------
        # CLICK
        # -------------------------
        if self.action_type == ActionType.click:
            if not self.target:
                raise ValueError("target required for click action")

        # -------------------------
        # TYPE
        # -------------------------
        if self.action_type == ActionType.type:
            if self.value is None:
                raise ValueError("value required for type action")

        # -------------------------
        # SELECT
        # -------------------------
        if self.action_type == ActionType.select:
            if not self.target:
                raise ValueError("target required for select action")
            if not self.value:
                raise ValueError("value required for select action")

        # -------------------------
        # ASSERT
        # -------------------------
        if self.action_type == ActionType.assert_action:
            if not self.assertion:
                raise ValueError("assertion required for assert action")

        # -------------------------
        # HANDLE DIALOG
        # -------------------------
        if self.action_type == ActionType.handle_dialog:
            if not self.dialog:
                raise ValueError("dialog required for handle_dialog action")

        return self


# =========================
# BLOCKS & TESTCASE
# =========================

class CIRBlock(BaseModel):
    block_id: Optional[str] = None
    step_id: Optional[str] = None
    intent: str
    actions: List[CIRAction] = Field(default_factory=list)
    block_type: CIRBlockType = Field(default=CIRBlockType.step)
    meta: Optional[dict] = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def sync_block_and_step_id(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "step_id" in data and "block_id" not in data:
                data["block_id"] = data["step_id"]
            elif "block_id" in data and "step_id" not in data:
                data["step_id"] = data["block_id"]
        return data


class CIRTestCase(BaseModel):
    test_case_id: str
    description: str
    setup: List[CIRBlock] = Field(default_factory=list)
    steps: List[CIRBlock] = Field(default_factory=list)
    teardown: List[CIRBlock] = Field(default_factory=list)
