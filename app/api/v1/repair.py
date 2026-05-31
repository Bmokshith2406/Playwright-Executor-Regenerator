"""
API v1 - Repair Routes

Provides step repair functionality with full OpenAPI documentation.
"""

from typing import Optional
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.security import get_api_key, RateLimiter
from app.core.metrics import MetricsCollector
from app.core.tracing import create_span, set_span_attribute
from app.core.database import get_db_session, RepairRepository
from app.models.step_repair import StepRepairRequest, StepRepairResponse
from app.services.cir_builder import CIRBuilder
from app.services.generator import StepCodeGenerator
from app.services.step_verifier import StepVerifier

logger = logging.getLogger("api.v1.repair")

router = APIRouter()

# Rate limiter instance
rate_limiter = RateLimiter(
    requests_per_minute=settings.RATE_LIMIT_REQUESTS_PER_MINUTE,
    burst_size=settings.RATE_LIMIT_BURST_SIZE,
)


# =============================================================================
# Request/Response Models with OpenAPI Examples
# =============================================================================

class RepairRequestExample(BaseModel):
    """Example repair request for OpenAPI documentation."""
    
    class Config:
        json_schema_extra = {
            "example": {
                "step_id": "test_login__step_2",
                "step_intent": "Click the login button to submit credentials",
                "original_code": 'await page.click("#login-btn")',
                "error_classification": {
                    "type": "LOCATOR_NOT_FOUND",
                    "subtype": "timeout",
                    "confidence": 0.95
                },
                "error_details": {
                    "message": "Timeout 5000ms exceeded waiting for selector '#login-btn'",
                    "stack_trace": "at Page.click (page.py:123)"
                },
                "dom_context": {
                    "html_snippet": '<button class="btn-primary" data-testid="login">Sign In</button>',
                    "visible_text": "Sign In",
                    "attributes": {"class": "btn-primary", "data-testid": "login"}
                }
            }
        }


class RepairResponseExample(BaseModel):
    """Example repair response for OpenAPI documentation."""
    
    class Config:
        json_schema_extra = {
            "example": {
                "step_id": "test_login__step_2",
                "repaired_code": 'await page.locator(\'button:has-text("Sign In")\').click()',
                "confidence": 0.92,
                "repair_strategy": "text_locator",
                "verification_passed": True,
                "metadata": {
                    "original_locator": "#login-btn",
                    "new_locator": 'button:has-text("Sign In")',
                    "action_type": "click",
                    "llm_model": "gemini-2.0-flash",
                    "processing_time_ms": 1234
                }
            }
        }


# =============================================================================
# API Endpoints
# =============================================================================

@router.post(
    "",
    response_model=StepRepairResponse,
    summary="Repair a failed Playwright step",
    description="""
    Analyzes a failed Playwright test step and generates repaired code.
    
    The repair process involves:
    1. **Classification** - Identifying the action type (click, type, select, assert)
    2. **CIR Building** - Creating a Canonical Intermediate Representation
    3. **Code Generation** - Generating new Playwright code
    4. **Verification** - Validating the generated code
    
    Supports multimodal analysis when screenshots are provided.
    """,
    responses={
        200: {
            "description": "Successfully repaired step",
            "content": {
                "application/json": {
                    "example": RepairResponseExample.Config.json_schema_extra["example"]
                }
            }
        },
        400: {"description": "Invalid request format"},
        401: {"description": "Missing or invalid API key"},
        429: {"description": "Rate limit exceeded"},
        500: {"description": "Internal server error"},
        503: {"description": "Service temporarily unavailable (circuit breaker open)"},
    }
)
async def repair_step(
    request: StepRepairRequest,
    http_request: Request,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(get_api_key),
):
    """
    Repair a failed Playwright test step.
    
    This endpoint accepts details about a failed step and returns repaired code
    that should resolve the failure while maintaining the original intent.
    """
    start_time = time.perf_counter()
    correlation_id = getattr(http_request.state, "correlation_id", "unknown")
    
    logger.info(
        "Repair request received",
        extra={
            "step_id": request.step_id,
            "correlation_id": correlation_id,
            "error_type": request.error_classification.type,
        }
    )
    
    # Check rate limit
    client_id = api_key[:8] if api_key else "anonymous"
    if not rate_limiter.allow_request(client_id):
        MetricsCollector.get_instance().record_rate_limit_hit(client_id)
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Please retry later.",
            headers={"Retry-After": "60"}
        )
    
    try:
        with create_span("repair_step") as span:
            set_span_attribute("step_id", request.step_id)
            set_span_attribute("error_type", request.error_classification.type)
            
            # Build CIR
            cir_builder = CIRBuilder()
            cir_block, context = await cir_builder.build(request=request)
            
            if not cir_block or not cir_block.actions:
                raise HTTPException(
                    status_code=400,
                    detail="Could not understand the step intent"
                )
            
            # Generate code
            generator = StepCodeGenerator()
            repaired_code = await generator.generate(cir_block, context)
            
            if not repaired_code:
                raise HTTPException(
                    status_code=500,
                    detail="Code generation failed"
                )
            
            # Verify code
            verifier = StepVerifier()
            verification = await verifier.verify(repaired_code, request.step_intent)
            
            # Calculate processing time
            processing_time_ms = int((time.perf_counter() - start_time) * 1000)
            
            # Record metrics
            metrics = MetricsCollector.get_instance()
            metrics.record_repair_request(
                outcome="success" if verification.passed else "partial",
                duration_ms=processing_time_ms,
            )
            
            # Build response
            response = StepRepairResponse(
                step_id=request.step_id,
                repaired_code=repaired_code,
                confidence=verification.confidence if verification.passed else 0.5,
                repair_strategy=cir_block.actions[0].action_type.value,
                verification_passed=verification.passed,
                metadata={
                    "original_code": request.original_code,
                    "action_type": cir_block.actions[0].action_type.value,
                    "processing_time_ms": processing_time_ms,
                    "correlation_id": correlation_id,
                }
            )
            
            # Record to database in background
            if settings.DATABASE_URL:
                background_tasks.add_task(
                    _record_repair_history,
                    request, response, processing_time_ms
                )
            
            return response
            
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            "Repair failed",
            extra={
                "step_id": request.step_id,
                "correlation_id": correlation_id,
                "error": str(e),
            }
        )
        
        # Record failure metric
        processing_time_ms = int((time.perf_counter() - start_time) * 1000)
        MetricsCollector.get_instance().record_repair_request(
            outcome="failure",
            duration_ms=processing_time_ms,
        )
        
        raise HTTPException(
            status_code=500,
            detail="Internal repair error. Please check logs for details."
        )


@router.post(
    "/batch",
    summary="Repair multiple steps in batch",
    description="Process multiple step repairs in a single request.",
    responses={
        200: {"description": "Batch repair results"},
        400: {"description": "Invalid request format"},
        401: {"description": "Missing or invalid API key"},
        429: {"description": "Rate limit exceeded"},
    }
)
async def repair_steps_batch(
    requests: list[StepRepairRequest],
    http_request: Request,
    api_key: str = Depends(get_api_key),
):
    """
    Repair multiple failed steps in batch.
    
    Maximum 10 steps per batch request.
    """
    if len(requests) > 10:
        raise HTTPException(
            status_code=400,
            detail="Maximum 10 steps per batch request"
        )
    
    results = []
    for req in requests:
        try:
            # Create a mock request object for the single repair
            result = await repair_step(
                request=req,
                http_request=http_request,
                background_tasks=BackgroundTasks(),
                api_key=api_key,
            )
            results.append({"step_id": req.step_id, "result": result, "error": None})
        except HTTPException as e:
            results.append({"step_id": req.step_id, "result": None, "error": e.detail})
        except Exception as e:
            results.append({"step_id": req.step_id, "result": None, "error": str(e)})
    
    return {"results": results, "total": len(requests), "successful": sum(1 for r in results if r["result"])}


# =============================================================================
# Background Tasks
# =============================================================================

async def _record_repair_history(
    request: StepRepairRequest,
    response: StepRepairResponse,
    duration_ms: int,
):
    """Record repair to database history."""
    try:
        async with get_db_session() as session:
            repo = RepairRepository(session)
            await repo.record_repair(
                step_id=request.step_id,
                original_code=request.original_code,
                repaired_code=response.repaired_code,
                error_type=request.error_classification.type,
                outcome="success" if response.verification_passed else "partial",
                duration_ms=duration_ms,
            )
    except Exception as e:
        logger.warning(f"Failed to record repair history: {e}")
