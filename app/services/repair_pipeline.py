# Revised pipeline: safer, more testable, better logs
import logging
import asyncio
from typing import Optional, Tuple, Any
from copy import deepcopy

from app.models.step_repair import StepRepairRequest
from app.models.cir import CIRBlockType, ActionType
from app.services.cir_builder import CIRBuilder
from app.services.generator import StepCodeGenerator
from app.services.step_verifier import StepVerifier
from app.services.step_modifier import StepModifier
from app.core.exceptions import StepNotRepairableError
from app.core.metrics import get_metrics

logger = logging.getLogger("pipeline.repair")

_cir_builder: Optional[CIRBuilder] = None
_generator: Optional[StepCodeGenerator] = None
_verifier: Optional[StepVerifier] = None
_modifier: Optional[StepModifier] = None


def _get_cir_builder() -> CIRBuilder:
    global _cir_builder
    if _cir_builder is None:
        _cir_builder = CIRBuilder()
    return _cir_builder


def _get_generator() -> StepCodeGenerator:
    global _generator
    if _generator is None:
        _generator = StepCodeGenerator()
    return _generator


def _get_verifier() -> StepVerifier:
    global _verifier
    if _verifier is None:
        _verifier = StepVerifier()
    return _verifier


def _get_modifier() -> StepModifier:
    global _modifier
    if _modifier is None:
        _modifier = StepModifier()
    return _modifier

def _normalize_code_for_compare(code: Optional[str]) -> str:
    if not code:
        return ""
    # strip trailing whitespace on each line and normalize line endings
    lines = [ln.rstrip() for ln in code.replace("\r\n", "\n").split("\n")]
    # compress multiple blank lines? (optional)
    return "\n".join(lines).strip()

async def execute_repair_pipeline(
    *,
    request: StepRepairRequest,
    error_image_bytes: Optional[bytes],
    request_id: str,
) -> Tuple[str, str]:
    metrics = get_metrics()

    # operate on a copy to avoid mutating caller-owned objects
    req = deepcopy(request)

    # attach screenshot if provided
    if error_image_bytes:
        if not getattr(req, "artifacts", None):
            raise StepNotRepairableError("Artifacts missing in request")
        req.artifacts.error_image_bytes = error_image_bytes

    try:
        cir_builder = _get_cir_builder()
        generator = _get_generator()
        verifier = _get_verifier()
        modifier = _get_modifier()

        with metrics.repair_pipeline_stage_duration.time(stage="cir_build"):
            block, context = await cir_builder.build(request=req)

        action_type = (
            block.actions[0].action_type
            if getattr(block, "actions", None)
            else "unknown"
        )

        # log minimal info, avoid logging full code
        logger.info(
            "PIPELINE_CIR_BUILT | request_id=%s | step_id=%s | block_type=%s | action_type=%s | actions=%d",
            request_id,
            req.step_id,
            getattr(block.block_type, "value", str(block.block_type)),
            getattr(action_type, "value", str(action_type)),
            len(getattr(block, "actions", []) or []),
        )

        # Code generation
        with metrics.repair_pipeline_stage_duration.time(stage="code_generation"):
            original_lines = (context.reference_code.splitlines() if getattr(context, "reference_code", None) else [])
            code_lines: list[str] = []
            for action in block.actions:
                if action.action_type == ActionType.handle_dialog:
                    code_lines.extend(generator.generate(action, original_lines=original_lines))
                else:
                    code_lines.extend(generator.generate(action))
            candidate_code = "\n".join(code_lines).strip()

        # Fallback block shortcut
        if block.block_type == CIRBlockType.fallback:
            logger.info("PIPELINE_FALLBACK | request_id=%s | step_id=%s", request_id, req.step_id)
            return candidate_code, getattr(action_type, "value", str(action_type))

        # Verifier Pass #1
        with metrics.repair_pipeline_stage_duration.time(stage="verification_1"):
            failure_history = [*(req.previous_failed_codes or []), req.original_code]
            verdict_1 = await verifier.verify(
                intent=block.intent,
                generated_code=candidate_code,
                matched_script=getattr(context, "matched_script", None),
                error_message=req.error_details.message,
                failure_history=failure_history,
            )

        if verdict_1 and verdict_1.get("verdict") == "correct":
            logger.info("PIPELINE_VERIFIER_PASS_1_SUCCESS | request_id=%s | step_id=%s", request_id, req.step_id)
            return candidate_code, getattr(action_type, "value", str(action_type))

        # Modifier
        with metrics.repair_pipeline_stage_duration.time(stage="modification"):
            modified_code = await modifier.modify(
                intent=block.intent,
                generated_code=candidate_code,
                verifier_reason=verdict_1.get("reason") if verdict_1 else None,
                failure_history=failure_history,
                error_message=req.error_details.message,
            )

        # Normalize before comparing
        norm_candidate = _normalize_code_for_compare(candidate_code)
        norm_modified = _normalize_code_for_compare(modified_code)

        if not modified_code or norm_modified == norm_candidate:
            # include more context in the raised error
            raise StepNotRepairableError(
                "Verifier-guided modification produced no meaningful change",
                details={"step_id": req.step_id, "block_id": getattr(block, "block_id", None)},
            )

        # Verifier Pass #2
        with metrics.repair_pipeline_stage_duration.time(stage="verification_2"):
            verdict_2 = await verifier.verify(
                intent=block.intent,
                generated_code=modified_code,
                matched_script=getattr(context, "matched_script", None),
                error_message=req.error_details.message,
                failure_history=failure_history,
            )

        if verdict_2 and verdict_2.get("verdict") == "correct":
            logger.info("PIPELINE_VERIFIER_PASS_2_SUCCESS | request_id=%s | step_id=%s", request_id, req.step_id)
            return modified_code, getattr(action_type, "value", str(action_type))

        # Final failure
        raise StepNotRepairableError(
            "Modified code failed verifier hard gate",
            details={"step_id": req.step_id, "block_id": getattr(block, "block_id", None)},
        )

    except StepNotRepairableError:
        # expected domain error: re-raise to allow caller to handle
        logger.info("PIPELINE_STEP_NOT_REPAIRABLE | request_id=%s | step_id=%s", request_id, getattr(req, "step_id", None))
        raise
    except Exception as exc:
        # unexpected: log lots of context and re-raise
        logger.exception("PIPELINE_UNEXPECTED_ERROR | request_id=%s | step_id=%s | error=%s", request_id, getattr(req, "step_id", None), exc)
        # optionally increment an error metric: metrics.repair_pipeline_errors.inc()
        raise

# Backward compatibility alias
repair_pipeline_safe = execute_repair_pipeline
