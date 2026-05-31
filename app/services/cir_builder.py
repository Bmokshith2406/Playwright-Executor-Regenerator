# app/services/cir_builder.py

from typing import Optional
import logging

from app.models.cir import (
    CIRBlock,
    CIRAction,
    ActionType,
    AssertionType,
    CIRAssertion,
    CIRBlockType,
    CIRDialog,
)
from app.models.context import StepRepairContext
from app.models.extraction import ExtractedLocator
from app.models.step_repair import StepRepairRequest
from app.core.exceptions import StepNotRepairableError

from app.services.llm_classifier import LLMActionClassifier
from app.services.extractors import (
    ClickActionExtractor,
    TypeActionExtractor,
    SelectActionExtractor,
    AssertActionExtractor,
    DialogActionExtractor,
)
from app.services.atomic_normalizer import AtomicNormalizer

logger = logging.getLogger("cir.builder")


class CIRBuilder:
    """
    STEP REPAIR CIR BUILDER.

    Responsibility:
    - Best-effort CIR construction
    - NEVER decides final repairability
    - Allows incomplete actions only when verifier/modifier can handle them
    """

    def __init__(self):
        self.classifier = LLMActionClassifier()
        self.click_extractor = ClickActionExtractor()
        self.type_extractor = TypeActionExtractor()
        self.select_extractor = SelectActionExtractor()
        self.assert_extractor = AssertActionExtractor()
        self.dialog_extractor = DialogActionExtractor()
        self.atomic_normalizer = AtomicNormalizer()

    async def build(
        self,
        *,
        request: StepRepairRequest,
    ) -> tuple[CIRBlock, StepRepairContext]:

        step_id = request.step_id
        step_intent = request.step_intent
        original_code = request.original_code

        error_message = (
            request.error_details.message
            if request.error_details
            else ""
        )

        artifacts = request.artifacts

        dom_snapshot = artifacts.dom_snapshot if artifacts else None
        error_image_bytes = artifacts.error_image_bytes if artifacts else None
        page_url = getattr(artifacts, "page_url", None)

        logger.info(
            "CIR BUILD START | step_id=%s | intent=%r",
            step_id,
            step_intent,
        )

        context = StepRepairContext(
            reference_code=original_code,
        )

        # --------------------------------------------------
        # 1️⃣ Classify action
        # --------------------------------------------------
        action_type = await self.classifier.classify(
            step_intent=step_intent,
            original_code=original_code,
            error_type=request.error_classification.type
            if request.error_classification
            else None,
            error_image_bytes=error_image_bytes,
        )

        logger.info(
            "CIR CLASSIFIER RESULT | step_id=%s | action_type=%s",
            step_id,
            action_type,
        )

        actions: list[CIRAction] = []
        extractor_type: Optional[str] = None

        # --------------------------------------------------
        # 2️⃣ RUNTIME DIALOG (FALLBACK BLOCK)
        # --------------------------------------------------
        if action_type == ActionType.handle_dialog:
            extractor_type = "DIALOG"

            extracted = await self.dialog_extractor.extract(
                step_intent=step_intent,
                original_code=original_code,
                error_message=error_message,
                dom_snapshot=dom_snapshot,
                error_image_bytes=error_image_bytes,
            )

            if not extracted:
                raise StepNotRepairableError(
                    "Dialog inferred but no dialog evidence extracted",
                    details={"step_id": step_id},
                )

            dialog_action, locator = extracted

            return (
                CIRBlock(
                    block_id=f"{step_id}_fallback",
                    intent="Handle runtime dialog",
                    block_type=CIRBlockType.fallback,
                    actions=[
                        CIRAction(
                            action_type=ActionType.handle_dialog,
                            dialog=CIRDialog(
                                action=dialog_action,
                                target=self._to_cir_locator(locator),
                            ),
                        )
                    ],
                    meta={"extractor": extractor_type},
                ),
                context,
            )

        # --------------------------------------------------
        # 3️⃣ CLICK
        # --------------------------------------------------
        if action_type == ActionType.click:
            extractor_type = "CLICK"

            locator = await self.click_extractor.extract(
                step_intent=step_intent,
                original_code=original_code,
                error_message=error_message,
                dom_snapshot=dom_snapshot,
                page_url=page_url,
                error_image_bytes=error_image_bytes,
            )

            if not locator:
                raise StepNotRepairableError(
                    "Click inferred but no locator extracted",
                    details={"step_id": step_id},
                )

            actions.append(
                CIRAction(
                    action_type=ActionType.click,
                    target=self._to_cir_locator(locator),
                )
            )

        # --------------------------------------------------
        # 4️⃣ TYPE
        # --------------------------------------------------
        elif action_type == ActionType.type:
            extractor_type = "TYPE"

            locator, value = await self.type_extractor.extract(
                step_intent=step_intent,
                original_code=original_code,
                error_message=error_message,
                dom_snapshot=dom_snapshot,
                page_url=page_url,
                error_image_bytes=error_image_bytes,
            )

            if not locator:
                raise StepNotRepairableError(
                    "Type inferred but no locator extracted",
                    details={"step_id": step_id},
                )

            actions.append(
                CIRAction(
                    action_type=ActionType.type,
                    target=self._to_cir_locator(locator),
                    value=value.value if value else None,
                )
            )

        # --------------------------------------------------
        # 5️⃣ SELECT
        # --------------------------------------------------
        elif action_type == ActionType.select:
            extractor_type = "SELECT"

            locator, value = await self.select_extractor.extract(
                step_intent=step_intent,
                original_code=original_code,
                error_message=error_message,
                dom_snapshot=dom_snapshot,
                page_url=page_url,
                error_image_bytes=error_image_bytes,
            )

            if not locator:
                raise StepNotRepairableError(
                    "Select inferred but no locator extracted",
                    details={"step_id": step_id},
                )

            actions.append(
                CIRAction(
                    action_type=ActionType.select,
                    target=self._to_cir_locator(locator),
                    value=value.value if value else None,
                )
            )

        # --------------------------------------------------
        # 6️⃣ ASSERT
        # --------------------------------------------------
        elif action_type == ActionType.assert_action:
            extractor_type = "ASSERT"

            assertion = await self.assert_extractor.extract(
                step_intent=step_intent,
                original_code=original_code,
                error_message=error_message,
                dom_snapshot=dom_snapshot,
                page_url=page_url,
                error_image_bytes=error_image_bytes,
            )

            if not assertion:
                raise StepNotRepairableError(
                    "Assertion inferred but no assertion extracted",
                    details={"step_id": step_id},
                )

            cir_locator = self._to_cir_locator(assertion.locator)

            if assertion.type != AssertionType.url_contains and cir_locator is None:
                raise StepNotRepairableError(
                    "Assertion requires locator but none could be constructed",
                    details={"step_id": step_id},
                )

            actions.append(
                CIRAction(
                    action_type=ActionType.assert_action,
                    target=cir_locator,
                    assertion=CIRAssertion(
                        assert_type=assertion.type,
                        expected_value=assertion.expected,
                    ),
                )
            )

        # --------------------------------------------------
        # FINAL SAFETY GATE
        # --------------------------------------------------
        if not actions:
            raise StepNotRepairableError(
                "Action classified but no CIR actions produced",
                details={
                    "step_id": step_id,
                    "action_type": str(action_type),
                },
            )

        logger.info(
            "CIR BUILD COMPLETE | step_id=%s | actions=%d | extractor=%s",
            step_id,
            len(actions),
            extractor_type,
        )

        block = CIRBlock(
            block_id=step_id,
            intent=step_intent,
            block_type=CIRBlockType.step,
            actions=actions,
            meta={"extractor": extractor_type},
        )


        block = self.atomic_normalizer.normalize_block(block)

        return block, context

    def _to_cir_locator(self, extracted: Optional[ExtractedLocator]):
        return extracted.to_cir() if extracted else None
