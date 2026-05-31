"""
API v1 - Executor Routes

Provides script execution with self-healing capabilities.
"""

from typing import Optional, Dict, Any
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.security import get_api_key
from app.core.metrics import MetricsCollector
from app.core.tracing import create_span, set_span_attribute
from app.services.execution_orchestrator import ExecutionOrchestrator
from app.executors import AsyncPythonExecutor, ExecutionResult

logger = logging.getLogger("api.v1.executor")

router = APIRouter()


# =============================================================================
# Request/Response Models
# =============================================================================

class ExecuteScriptRequest(BaseModel):
    """Request to execute a Playwright script."""
    
    script_path: str = Field(
        ...,
        description="Path to the Python script to execute",
        example="/tests/test_login.py"
    )
    timeout_seconds: int = Field(
        default=300,
        ge=10,
        le=3600,
        description="Maximum execution time in seconds"
    )
    enable_self_healing: bool = Field(
        default=True,
        description="Enable automatic repair of failed steps"
    )
    max_repair_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum repair attempts per step"
    )
    env_vars: Optional[Dict[str, str]] = Field(
        default=None,
        description="Additional environment variables"
    )
    
    class Config:
        json_schema_extra = {
            "example": {
                "script_path": "/tests/e2e/test_checkout.py",
                "timeout_seconds": 300,
                "enable_self_healing": True,
                "max_repair_attempts": 3,
                "env_vars": {
                    "BASE_URL": "https://staging.example.com",
                    "HEADLESS": "true"
                }
            }
        }


class ExecuteScriptResponse(BaseModel):
    """Response from script execution."""
    
    success: bool = Field(description="Whether the script passed")
    run_id: str = Field(description="Unique execution run ID")
    duration_ms: int = Field(description="Total execution time in milliseconds")
    steps_total: int = Field(description="Total number of steps")
    steps_passed: int = Field(description="Number of passed steps")
    steps_repaired: int = Field(description="Number of steps that required repair")
    steps_failed: int = Field(description="Number of failed steps")
    artifacts_dir: Optional[str] = Field(description="Path to execution artifacts")
    stdout: str = Field(description="Standard output from execution")
    stderr: str = Field(description="Standard error from execution")
    repairs: list[Dict[str, Any]] = Field(
        default_factory=list,
        description="Details of repairs performed"
    )
    
    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "run_id": "a1b2c3d4",
                "duration_ms": 45230,
                "steps_total": 15,
                "steps_passed": 14,
                "steps_repaired": 1,
                "steps_failed": 0,
                "artifacts_dir": "/runs/a1b2c3d4/artifacts",
                "stdout": "Test execution completed successfully",
                "stderr": "",
                "repairs": [
                    {
                        "step_id": "test_checkout__step_5",
                        "original_error": "Timeout waiting for selector",
                        "repair_strategy": "text_locator",
                        "success": True
                    }
                ]
            }
        }


class ExecutionStatusResponse(BaseModel):
    """Response for execution status check."""
    
    run_id: str
    status: str = Field(description="Current status: pending, running, completed, failed")
    progress: float = Field(description="Progress percentage (0-100)")
    current_step: Optional[str] = Field(description="Currently executing step")
    elapsed_ms: int = Field(description="Elapsed time in milliseconds")
    
    class Config:
        json_schema_extra = {
            "example": {
                "run_id": "a1b2c3d4",
                "status": "running",
                "progress": 45.5,
                "current_step": "test_checkout__step_7",
                "elapsed_ms": 23450
            }
        }


# =============================================================================
# API Endpoints
# =============================================================================

@router.post(
    "/execute",
    response_model=ExecuteScriptResponse,
    summary="Execute a Playwright script",
    description="""
    Executes a Playwright test script with optional self-healing.
    
    When self-healing is enabled, failed steps will automatically be repaired
    using the LLM-powered repair engine. The execution continues after
    successful repairs.
    
    **Features:**
    - Automatic step repair on failure
    - Configurable retry limits
    - Artifact collection (screenshots, traces)
    - Detailed execution metrics
    """,
    responses={
        200: {"description": "Execution completed (check success field for result)"},
        400: {"description": "Invalid request or script not found"},
        401: {"description": "Missing or invalid API key"},
        408: {"description": "Execution timed out"},
        500: {"description": "Internal execution error"},
    }
)
async def execute_script(
    request: ExecuteScriptRequest,
    http_request: Request,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(get_api_key),
):
    """
    Execute a Playwright test script with self-healing capabilities.
    """
    start_time = time.perf_counter()
    correlation_id = getattr(http_request.state, "correlation_id", "unknown")
    
    logger.info(
        "Execution request received",
        extra={
            "script_path": request.script_path,
            "correlation_id": correlation_id,
            "self_healing": request.enable_self_healing,
        }
    )
    
    try:
        with create_span("execute_script") as span:
            set_span_attribute("script_path", request.script_path)
            set_span_attribute("self_healing", request.enable_self_healing)
            
            if request.enable_self_healing and settings.ENABLE_SELF_HEALING:
                # Use orchestrator with self-healing
                orchestrator = ExecutionOrchestrator(
                    max_repair_attempts=request.max_repair_attempts,
                )
                result = await orchestrator.execute_with_healing(
                    script_path=request.script_path,
                    timeout_seconds=request.timeout_seconds,
                    env_vars=request.env_vars,
                )
                
                duration_ms = int((time.perf_counter() - start_time) * 1000)
                
                return ExecuteScriptResponse(
                    success=result.success,
                    run_id=result.run_id,
                    duration_ms=duration_ms,
                    steps_total=result.steps_total,
                    steps_passed=result.steps_passed,
                    steps_repaired=result.steps_repaired,
                    steps_failed=result.steps_failed,
                    artifacts_dir=result.artifacts_dir,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    repairs=result.repairs,
                )
            else:
                # Direct execution without self-healing
                executor = AsyncPythonExecutor(
                    timeout_seconds=request.timeout_seconds,
                )
                result = await executor.execute(
                    script_path=request.script_path,
                    extra_env=request.env_vars,
                )
                
                duration_ms = int((time.perf_counter() - start_time) * 1000)
                
                return ExecuteScriptResponse(
                    success=result.success,
                    run_id=result.run_id,
                    duration_ms=duration_ms,
                    steps_total=0,  # Not tracked without orchestrator
                    steps_passed=0,
                    steps_repaired=0,
                    steps_failed=0 if result.success else 1,
                    artifacts_dir=result.artifacts_dir,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    repairs=[],
                )
                
    except FileNotFoundError:
        raise HTTPException(
            status_code=400,
            detail=f"Script not found: {request.script_path}"
        )
    except Exception as e:
        logger.exception(
            "Execution failed",
            extra={
                "script_path": request.script_path,
                "correlation_id": correlation_id,
                "error": str(e),
            }
        )
        raise HTTPException(
            status_code=500,
            detail="Internal execution error"
        )


@router.post(
    "/execute/async",
    summary="Execute script asynchronously",
    description="Submit a script for background execution and receive a run ID for status tracking.",
    responses={
        202: {"description": "Execution accepted, returns run ID"},
        400: {"description": "Invalid request"},
        401: {"description": "Missing or invalid API key"},
    }
)
async def execute_script_async(
    request: ExecuteScriptRequest,
    http_request: Request,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(get_api_key),
):
    """
    Submit a script for asynchronous execution.
    
    Returns immediately with a run ID that can be used to check status.
    """
    import uuid
    
    run_id = uuid.uuid4().hex[:8]
    
    # Queue execution in background
    background_tasks.add_task(
        _execute_in_background,
        run_id=run_id,
        request=request,
    )
    
    return {
        "run_id": run_id,
        "status": "accepted",
        "status_url": f"/api/v1/executor/status/{run_id}",
    }


@router.get(
    "/status/{run_id}",
    response_model=ExecutionStatusResponse,
    summary="Get execution status",
    description="Check the status of an asynchronous execution.",
    responses={
        200: {"description": "Execution status"},
        404: {"description": "Run ID not found"},
        401: {"description": "Missing or invalid API key"},
    }
)
async def get_execution_status(
    run_id: str,
    api_key: str = Depends(get_api_key),
):
    """
    Get the status of an execution by run ID.
    """
    # This would need Redis/database integration for real implementation
    # For now, return a placeholder
    return ExecutionStatusResponse(
        run_id=run_id,
        status="unknown",
        progress=0.0,
        current_step=None,
        elapsed_ms=0,
    )


# =============================================================================
# Background Tasks
# =============================================================================

async def _execute_in_background(run_id: str, request: ExecuteScriptRequest):
    """Execute script in background."""
    try:
        orchestrator = ExecutionOrchestrator(
            max_repair_attempts=request.max_repair_attempts,
        )
        result = await orchestrator.execute_with_healing(
            script_path=request.script_path,
            timeout_seconds=request.timeout_seconds,
            env_vars=request.env_vars,
        )
        
        # Store result for later retrieval
        # This would use Redis in production
        logger.info(f"Background execution completed: {run_id}, success={result.success}")
        
    except Exception as e:
        logger.exception(f"Background execution failed: {run_id}")
