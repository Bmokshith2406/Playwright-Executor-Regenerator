"""
Executor API Route - Production Grade

Features:
- Self-healing execution
- Metrics integration
- Database persistence
- Sandbox execution support
- Enhanced error handling
"""
import zipfile
from fastapi.responses import FileResponse
from fastapi import APIRouter, HTTPException, UploadFile, File, Request
from fastapi.responses import PlainTextResponse, JSONResponse
import logging
import time
import asyncio
import uuid
import hashlib
import tempfile
from pathlib import Path

from app.core.utils import set_correlation_id


from app.executors import AsyncPythonExecutor
from app.services.auto_repair_trigger import AutoRepairTrigger
from app.services.repair_service import RepairService
from app.services.repair_pipeline import repair_pipeline_safe
from app.services.script_patcher import ScriptPatcher
from app.services.execution_orchestrator import (
    SelfHealingExecutorOrchestrator,
    ExecutorOrchestratorConfig,
)
from app.core.config import get_settings
from app.core.metrics import get_metrics
from app.core.tracing import trace, SpanKind
from app.core.database import get_repository, ExecutionRecord
from app.core.security import SandboxGuard

router = APIRouter()
logger = logging.getLogger("api.executor")
settings = get_settings()

# --------------------------------------------------
# Singletons
# --------------------------------------------------

_executor = AsyncPythonExecutor(
    timeout_seconds=settings.EXECUTOR_TIMEOUT_SECONDS,
    base_work_dir=settings.EXECUTOR_BASE_WORK_DIR,
)
# Bounded concurrency control
MAX_CONCURRENT_REPAIRS = 5
repair_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REPAIRS)

_repair_service = RepairService(
    semaphore=repair_semaphore,
    pipeline_fn=repair_pipeline_safe
)
_repair_trigger = AutoRepairTrigger()
_patcher = ScriptPatcher()
_sandbox_guard = SandboxGuard()

_orchestrator = SelfHealingExecutorOrchestrator(
    executor=_executor,
    repair_trigger=_repair_trigger,
    repair_service=_repair_service,
    patcher=_patcher,
    config=ExecutorOrchestratorConfig(),
)

# --------------------------------------------------
# API Route
# --------------------------------------------------

@router.post(
    "",
    summary="Execute a Python file with bounded self-healing",
    description="Executes a Python script and automatically repairs failed steps using the repair API.",
    response_description="Final execution output after auto-repair (if any)",
    tags=["executor"],
    responses={
        200: {"description": "Execution completed (check semantic_status for result)"},
        400: {"description": "Invalid request (not a .py file)"},
        403: {"description": "Script rejected by sandbox guard"},
        500: {"description": "Internal execution error"},
    },
)
@router.post(
    "/run",
    summary="Execute a Python file with bounded self-healing",
    description="Executes a Python script and automatically repairs failed steps using the repair API.",
    response_description="Final execution output after auto-repair (if any)",
    tags=["executor"],
    responses={
        200: {"description": "Execution completed (check semantic_status for result)"},
        400: {"description": "Invalid request (not a .py file)"},
        403: {"description": "Script rejected by sandbox guard"},
        500: {"description": "Internal execution error"},
    },
)
@trace(name="executor.execute", kind=SpanKind.SERVER)
async def execute_python_file(
    request: Request,
    script: UploadFile = File(..., description="Python script to execute"),
):
    """
    Execute a Python file with bounded self-healing.

    This endpoint:
    - Executes the provided Python script
    - On semantic failure, attempts bounded step-level repair
    - Applies at most one patch per iteration
    - Re-runs script from scratch after patch
    - Returns final execution result

    IMPORTANT:
    - Orchestrator controls execution bounds
    - Semantic status is resolved ONLY via status.txt
    - Route NEVER infers success/failure
    """

    request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex[:12]
    set_correlation_id(request_id)
    start_ts = time.perf_counter()
    metrics = get_metrics()

    logger.info(
        "EXEC_START | request_id=%s | filename=%s",
        request_id,
        script.filename,
    )

    # Validate file type
    if not script.filename or not script.filename.endswith(".py"):
        metrics.script_executions_total.inc(status="rejected")
        raise HTTPException(
            status_code=400,
            detail="Only .py files are supported",
        )

    try:
        script_content = await script.read()
        script_text = script_content.decode("utf-8")
        script_hash = hashlib.sha256(script_content).hexdigest()[:12]

        # Sandbox validation (if enabled)
        if settings.ENABLE_SANDBOX_EXECUTION:
            is_safe, reason = _sandbox_guard.validate_script(script_text)
            if not is_safe:
                logger.warning(
                    "EXEC_REJECTED | request_id=%s | reason=%s",
                    request_id,
                    reason,
                )
                metrics.script_executions_total.inc(status="rejected")
                raise HTTPException(
                    status_code=403,
                    detail=f"Script rejected by sandbox: {reason}",
                )

        # Check self-healing feature flag
        if not settings.ENABLE_SELF_HEALING:
            logger.info(
                "EXEC_NO_HEALING | request_id=%s | self_healing=disabled",
                request_id,
            )

        with tempfile.TemporaryDirectory() as tmp_dir:
            script_path = Path(tmp_dir) / script.filename
            script_path.write_text(script_text)

            # --------------------------------------------------
            # SELF-HEALING EXECUTION
            # --------------------------------------------------

            if settings.ENABLE_SELF_HEALING:
                result = await _orchestrator.execute_script_with_self_healing(
                    script_path=str(script_path)
                )
            else:
                # Direct execution without self-healing
                result = await _executor.execute(str(script_path))

            duration_ms = round(
                (time.perf_counter() - start_ts) * 1000, 2
            )

            # Record metrics
            metrics.script_executions_total.inc(status=result.semantic_status)
            metrics.script_execution_duration_seconds.observe(
                duration_ms / 1000,
                status=result.semantic_status,
            )

            # Save to database
            await _save_execution_record(
                run_id=result.run_id,
                script_path=str(script_path),
                script_hash=script_hash,
                result=result,
                duration_ms=int(duration_ms),
                request_id=request_id,
            )

            # --------------------------------------------------
            # ZIP RUN DIRECTORY (SUCCESS-AWARE)
            # --------------------------------------------------

            if result.semantic_status == "passed":
                run_dir_temp = Path(result.working_dir)
                project_root = run_dir_temp.parents[1]
                successful_runs_dir = project_root / "successful_runs"
                run_dir = successful_runs_dir / result.run_id
                               
                # ADD THIS LINE
                logger.info(
                    "RUN_DIRECTORY_RESOLVED | request_id=%s | run_id=%s | status=%s | path=%s",
                    request_id,
                    result.run_id,
                    result.semantic_status,
                    str(run_dir),
                )
            else:
                # fallback to original working directory
                run_dir = Path(result.working_dir)
                # ADD THIS LINE
                logger.info(
                    "RUN_DIRECTORY_RESOLVED FROM ELSE | request_id=%s | run_id=%s | status=%s | path=%s",
                    request_id,
                    result.run_id,
                    result.semantic_status,
                    str(run_dir),
                )

            if not run_dir.exists():
                logger.error(
                    "RUN_DIR_MISSING | request_id=%s | run_id=%s | status=%s",
                    request_id,
                    result.run_id,
                    result.semantic_status,
                )
                raise HTTPException(
                    status_code=500,
                    detail="Run directory not found",
                )

            zip_path = run_dir.parent / f"{result.run_id}.zip"

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                for file_path in run_dir.rglob("*"):
                    if file_path.is_file():
                        zipf.write(
                            file_path,
                            arcname=file_path.relative_to(run_dir),
                        )

            logger.info(
                "EXEC_END | request_id=%s | status=%s | zip_created=%s",
                request_id,
                result.semantic_status,
                zip_path,
            )

            return FileResponse(
                path=zip_path,
                filename=f"{result.run_id}.zip",
                media_type="application/zip",
                headers={
                    "X-Request-ID": request_id,
                    "X-Semantic-Status": result.semantic_status,
                    "X-Run-ID": result.run_id,
                    "X-Duration-Ms": str(duration_ms),
                    "X-Script-Hash": script_hash,
                },
            )

    except HTTPException:
        raise

    except Exception as exc:
        duration_ms = round((time.perf_counter() - start_ts) * 1000, 2)
        metrics.script_executions_total.inc(status="error")
        
        logger.exception(
            "EXEC_FAILURE | request_id=%s | filename=%s | error=%s",
            request_id,
            script.filename,
            str(exc),
        )

        raise HTTPException(
            status_code=500,
            detail="Internal execution error",
        )


# --------------------------------------------------
# Statistics Endpoint
# --------------------------------------------------

@router.get(
    "/stats",
    summary="Get execution statistics",
    description="Returns statistics about script executions.",
    tags=["executor"],
)
async def get_execution_stats():
    """Get execution statistics from the database."""
    try:
        repo = await get_repository()
        stats = await repo.get_execution_stats()
        return JSONResponse(content=stats)
    except Exception as e:
        logger.warning("Failed to get execution stats: %s", e)
        return JSONResponse(content={"error": "Stats unavailable"})


# --------------------------------------------------
# Database Persistence
# --------------------------------------------------

async def _save_execution_record(
    *,
    run_id: str,
    script_path: str,
    script_hash: str,
    result,
    duration_ms: int,
    request_id: str,
):
    """Save execution record to database."""
    try:
        repo = await get_repository()
        
        raw_meta = getattr(result, "metadata", {})
        meta = {}
        if isinstance(raw_meta, dict) and type(raw_meta).__name__ not in ('MagicMock', 'AsyncMock', 'Mock'):
            meta = raw_meta

        record = ExecutionRecord(
            run_id=run_id,
            script_path=script_path,
            script_hash=script_hash,
            status=result.semantic_status,
            exit_code=result.exit_code,
            duration_ms=duration_ms,
            stdout=result.stdout[:10000] if result.stdout else None,  # Truncate
            stderr=result.stderr[:10000] if result.stderr else None,
            request_id=request_id,
            metadata=meta,
        )
        
        await repo.save_execution(record)
        logger.debug("Saved execution record: %s", record.id)
    except Exception as e:
        # Don't fail the request if database save fails
        logger.warning("Failed to save execution record: %s", e)
