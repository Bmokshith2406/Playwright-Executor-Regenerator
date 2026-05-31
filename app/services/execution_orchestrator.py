from __future__ import annotations

import asyncio
import json
import shutil
import logging
from pathlib import Path
from typing import Dict, Optional, List, Tuple
from datetime import datetime, UTC
from dataclasses import dataclass, field, replace, is_dataclass

from app.executors import AsyncPythonExecutor
from app.services.auto_repair_trigger import AutoRepairTrigger
from app.services.repair_service import RepairService
from app.services.repair_explanation_service import RepairExplanationService
from app.core.exceptions import StepNotRepairableError
from app.services.script_patcher import ScriptPatcher
from app.services.llm_fallback_repair import LLMFallbackRepairEngine
from app.core.config import settings

# Safety & Resilience components from new locations
from app.core.resilience import CircuitBreaker, BackoffPolicy
from app.core.utils import FailureFingerprint
from app.core.io import AtomicWriter
from app.services.rollback import RollbackManager

logger = logging.getLogger("execution.orchestrator.self_healing")


# ==================================================
# CONFIG
# ==================================================

@dataclass
class ExecutorOrchestratorConfig:
    max_repairs_per_step: int = 5
    repair_timeout_sec: int = 200
    max_running_retries: int = 3
    artifacts_root: Path = Path.cwd()
    status_filename: str = "status.txt"


# ==================================================
# CONTEXT
# ==================================================

@dataclass
class ExecutionContext:
    repair_attempts: Dict[str, int] = field(default_factory=dict)
    repair_history: List[dict] = field(default_factory=list)
    failure_fingerprints: Dict[str, int] = field(default_factory=dict)
    fallback_attempts: Dict[str, int] = field(default_factory=dict)
    running_retries: int = 0


# ==================================================
# ORCHESTRATOR
# ==================================================

class SelfHealingExecutorOrchestrator:
    VALID_STATUSES = {"passed", "failed", "running"}

    def __init__(
        self,
        *,
        executor: AsyncPythonExecutor,
        repair_trigger: AutoRepairTrigger,
        repair_service: RepairService,
        patcher: ScriptPatcher,
        config: ExecutorOrchestratorConfig = ExecutorOrchestratorConfig(),
    ):
        self.executor = executor
        self.repair_trigger = repair_trigger
        self.repair_service = repair_service
        self.patcher = patcher
        self.config = config
        
        self.fallback_repair = LLMFallbackRepairEngine()
        self.circuit_breaker = CircuitBreaker()
        self.backoff = BackoffPolicy()
        self.explainer = RepairExplanationService()
        self.rollback_manager = RollbackManager()

    # ==================================================
    # PUBLIC API
    # ==================================================

    async def execute_script_with_self_healing(self, *, script_path: str):
        ctx = ExecutionContext()
        iteration = 0

        while True:
            iteration += 1
            logger.info("EXECUTION_START", extra={"iteration": iteration})

            # --------------------------------------------------
            # ALWAYS RE-EXECUTE SCRIPT EACH ITERATION
            # --------------------------------------------------

            result = await asyncio.wait_for(
                self.executor.execute(script_path),
                timeout=settings.EXECUTOR_TIMEOUT_SECONDS
            )

            status, execution_dir = self._resolve_execution_status(
                result.artifacts_dir
            )

            # ----------------------------
            # STATUS MUST BE VALID
            # ----------------------------

            if status is None:
                logger.error("STATUS_RESOLUTION_FAILED_FATAL")
                return self._with_semantic_status(result, "failed")
            
            if status == "running":
                ctx.running_retries += 1

                if ctx.running_retries > self.config.max_running_retries:
                    logger.error("RUNNING_STATUS_STUCK_ABORTING")
                    return self._with_semantic_status(result, "failed")

                logger.info("STATUS_RUNNING_REEXECUTING", extra={"iteration": iteration})
                await asyncio.sleep(1)
                continue

            # ----------------------------
            # PASSED → FINALIZE
            # ----------------------------

            if status == "passed":
                if execution_dir:
                    self._emit_final_docs(
                        script_path=script_path,
                        execution_dir=execution_dir,
                        iterations=iteration,
                        final_status="passed",
                        ctx=ctx,
                    )
                return self._with_semantic_status(result, "passed")

            # ----------------------------
            # FAILED → TRY REPAIR
            # ----------------------------

            repair_request = self.repair_trigger.build_request_from_artifacts(
                result.artifacts_dir
            )

            if not repair_request:
                logger.error("NO_REPAIR_REQUEST_STOPPING")
                return self._with_semantic_status(result, "failed")

            step_id = repair_request.step_id
            attempt = self._increment_attempt(ctx, step_id)

            if attempt > self.config.max_repairs_per_step:
                logger.error("MAX_REPAIRS_PER_STEP_EXCEEDED", extra={"step_id": step_id})
                await self._handle_final_failure(result, repair_request, ctx)
                return self._with_semantic_status(result, "failed")

            fingerprint = FailureFingerprint.compute(
                step_id,
                result.stderr or "",
                result.stdout or "",
            )

            count = ctx.failure_fingerprints.get(fingerprint, 0) + 1
            ctx.failure_fingerprints[fingerprint] = count

            if count > 2:
                logger.error(
                    "REPEATED_FAILURE_ABORT",
                    extra={"step_id": step_id, "fingerprint": fingerprint, "count": count},
                )
                await self._handle_final_failure(result, repair_request, ctx)
                return self._with_semantic_status(result, "failed")

            if not self.circuit_breaker.allow():
                logger.error("CIRCUIT_BREAKER_OPEN")
                await self._handle_final_failure(result, repair_request, ctx)
                return self._with_semantic_status(result, "failed")

            try:
                repaired_code, _ = await asyncio.wait_for(
                    self.repair_service.repair_step(
                        request=repair_request,
                        error_image_bytes=repair_request.artifacts.error_image_bytes,
                        request_id=result.run_id,
                    ),
                    timeout=self.config.repair_timeout_sec,
                )
                self.circuit_breaker.record_success()

            except StepNotRepairableError:
                logger.warning(
                    "PRIMARY_REPAIR_NOT_POSSIBLE_TRYING_FALLBACK",
                    extra={"step_id": step_id},
                )

                # -----------------------------------
                # Count fallback toward TOTAL repair limit
                # -----------------------------------
                attempt = self._increment_attempt(ctx, step_id)

                if attempt > self.config.max_repairs_per_step:
                    logger.error(
                        "MAX_REPAIRS_PER_STEP_EXCEEDED (INCLUDING FALLBACK)",
                        extra={"step_id": step_id, "attempt": attempt},
                    )
                    await self._handle_final_failure(result, repair_request, ctx)
                    return self._with_semantic_status(result, "failed")

                # -----------------------------------
                # Fallback-only limit (max 2)
                # -----------------------------------
                fallback_count = ctx.fallback_attempts.get(step_id, 0) + 1
                ctx.fallback_attempts[step_id] = fallback_count

                if fallback_count > 2:
                    logger.error(
                        "FALLBACK_REPAIR_LIMIT_EXCEEDED",
                        extra={"step_id": step_id},
                    )
                    await self._handle_final_failure(result, repair_request, ctx)
                    return self._with_semantic_status(result, "failed")

                # -----------------------------------
                # Call fallback LLM
                # -----------------------------------
                fallback_code = await self.fallback_repair.repair(
                    step_intent=repair_request.step_intent,
                    current_code=repair_request.original_code,
                    error_text=repair_request.artifacts.error_text or "",
                    error_image_bytes=repair_request.artifacts.error_image_bytes,
                    dom_snapshot=repair_request.artifacts.dom_snapshot,
                )
                logger.info(
                    "FALLBACK_LLM_OUTPUT | step_id=%s | output=\n%s",
                    step_id,
                    fallback_code,
                )

                if not fallback_code:
                    logger.error(
                        "FALLBACK_REPAIR_FAILED_ABORTING",
                        extra={"step_id": step_id},
                    )
                    await self._handle_final_failure(result, repair_request, ctx)
                    return self._with_semantic_status(result, "failed")

                step_fn = self._extract_step_function(step_id)
                if not step_fn:
                    logger.error("INVALID_STEP_ID_FORMAT")
                    return self._with_semantic_status(result, "failed")

                backup_path = self.patcher.patch_step(
                    script_path=script_path,
                    step_function_name=step_fn,
                    new_step_body=fallback_code,
                    backup=True,
                )

                self.rollback_manager.register(step_id, backup_path)

                # ----------------------------------------
                # Generate explanation for fallback repair
                # ----------------------------------------
                explanation = await self.explainer.generate_explanation(
                    step_id=step_id,
                    step_intent=repair_request.step_intent,
                    original_code=repair_request.original_code,
                    repaired_code=fallback_code,
                    error_text=repair_request.artifacts.error_text or "",
                    dom_snapshot=repair_request.artifacts.dom_snapshot,
                    error_image_bytes=repair_request.artifacts.error_image_bytes,
                )

                self._record_repair(
                    ctx,
                    step_id,
                    attempt,
                    "fallback_patched",
                    explanation=explanation,
                )

                delay = self.backoff.compute(attempt)
                await asyncio.sleep(delay)

                continue

            except asyncio.TimeoutError:
                self.circuit_breaker.record_failure()
                delay = self.backoff.compute(attempt)
                await asyncio.sleep(delay)
                continue

            except Exception:
                self.circuit_breaker.record_failure()
                logger.exception("REPAIR_API_FAILURE")
                delay = self.backoff.compute(attempt)
                await asyncio.sleep(delay)
                continue

            if not repaired_code:
                logger.error("EMPTY_REPAIR_RESPONSE_FATAL")
                return self._with_semantic_status(result, "failed")

            step_fn = self._extract_step_function(step_id)
            if not step_fn:
                logger.error("INVALID_STEP_ID_FORMAT")
                return self._with_semantic_status(result, "failed")

            # ----------------------------
            # APPLY PATCH
            # ----------------------------

            backup_path = self.patcher.patch_step(
                script_path=script_path,
                step_function_name=step_fn,
                new_step_body=repaired_code,
                backup=True,
            )

            self.rollback_manager.register(step_id, backup_path)

            # ----------------------------------------
            # Generate explanation
            # ----------------------------------------
            explanation = await self.explainer.generate_explanation(
                step_id=step_id,
                step_intent=repair_request.step_intent,
                original_code=repair_request.original_code,
                repaired_code=repaired_code,
                error_text=repair_request.artifacts.error_text or "",
                dom_snapshot=repair_request.artifacts.dom_snapshot,
                error_image_bytes=repair_request.artifacts.error_image_bytes,
            )

            self._record_repair(
                ctx,
                step_id,
                attempt,
                "patched",
                explanation=explanation,
            )

            delay = self.backoff.compute(attempt)
            await asyncio.sleep(delay)

            # 🔁 CRITICAL: RE-RUN SCRIPT AFTER PATCH
            continue

    async def execute_with_self_healing(self, *, script_path: str):
        """Backward compatibility wrapper."""
        return await self.execute_script_with_self_healing(script_path=script_path)

    async def _handle_final_failure(
        self,
        result,
        repair_request,
        ctx: ExecutionContext,
    ):
        step_id = repair_request.step_id
        logger.info("GENERATING_FINAL_FAILURE_EXPLANATION | step_id=%s", step_id)
        
        try:
            explanation = await self.explainer.generate_explanation(
                step_id=step_id,
                step_intent=repair_request.step_intent,
                original_code=repair_request.original_code,
                repaired_code="PERMANENT_FAILURE (Self-healing attempts exhausted)",
                error_text=repair_request.artifacts.error_text or "",
                dom_snapshot=repair_request.artifacts.dom_snapshot,
                error_image_bytes=repair_request.artifacts.error_image_bytes,
            )
            
            if explanation:
                if not hasattr(result, "metadata") or result.metadata is None:
                    result.metadata = {}
                result.metadata["final_failure_explanation"] = explanation
                
                explanation_path = Path(result.working_dir) / "final_failure_explanation.json"
                AtomicWriter.write(
                    explanation_path,
                    json.dumps(explanation, indent=2),
                )
                logger.info("Saved final_failure_explanation.json to %s", explanation_path)
        except Exception as e:
            logger.warning("Failed to generate/save final failure explanation: %s", e)

    # ==================================================
    # CONTEXT OPS
    # ==================================================

    def _increment_attempt(self, ctx: ExecutionContext, step_id: str) -> int:
        ctx.repair_attempts[step_id] = ctx.repair_attempts.get(step_id, 0) + 1
        return ctx.repair_attempts[step_id]

    def _record_repair(
        self,
        ctx: ExecutionContext,
        step_id: str,
        attempt: int,
        outcome: str,
        explanation: Optional[dict] = None,
    ):
        ctx.repair_history.append(
            {
                "step_id": step_id,
                "attempt": attempt,
                "outcome": outcome,
                "explanation": explanation,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

    # ==================================================
    # RESULT REHYDRATION
    # ==================================================

    @staticmethod
    def _with_semantic_status(result, semantic_status: str):
        assert semantic_status in {"passed", "failed"}

        if is_dataclass(result):
            return replace(result, semantic_status=semantic_status)

        try:
            setattr(result, "semantic_status", semantic_status)
            return result
        except Exception:
            return result

    # ==================================================
    # STATUS RESOLUTION
    # ==================================================

    def _resolve_execution_status(
        self, artifacts_dir: Optional[str]
    ) -> Tuple[Optional[str], Optional[Path]]:

        if not artifacts_dir:
            logger.error("STATUS_RESOLUTION_FAILED: artifacts_dir is None")
            return None, None

        base = Path(artifacts_dir).resolve()

        logger.info(
            "STATUS_SEARCH_START",
            extra={
                "artifacts_dir_input": artifacts_dir,
                "resolved_base": str(base),
                "status_filename": self.config.status_filename,
            },
        )

        if not base.exists():
            logger.error(
                "STATUS_SEARCH_BASE_NOT_FOUND",
                extra={"resolved_base": str(base)},
            )
            return None, None

        logger.info(
            "STATUS_SEARCH_WALKING_TREE",
            extra={"search_root": str(base)},
        )

        status_files = list(base.rglob(self.config.status_filename))

        logger.info(
            "STATUS_SEARCH_RESULTS",
            extra={
                "count": len(status_files),
                "paths": [str(p) for p in status_files],
            },
        )

        if not status_files:
            logger.error("STATUS_FILE_NOT_FOUND", extra={"search_root": str(base)})
            return None, None

        status_file = max(status_files, key=lambda p: p.stat().st_mtime)

        logger.info(
            "STATUS_FILE_SELECTED",
            extra={"path": str(status_file)},
        )

        value = status_file.read_text(encoding="utf-8").strip().lower()

        logger.info(
            "STATUS_FILE_READ",
            extra={"value": value, "path": str(status_file)},
        )

        if value not in self.VALID_STATUSES:
            logger.error(
                "INVALID_STATUS",
                extra={"value": value, "file": str(status_file)},
            )
            return None, None

        logger.info(
            "STATUS_RESOLVED",
            extra={"value": value, "path": str(status_file)},
        )

        return value, status_file.parent

    # ==================================================
    # SUCCESS ARTIFACTS
    # ==================================================

    def _emit_final_docs(
        self,
        *,
        script_path: str,
        execution_dir: Path,
        iterations: int,
        final_status: str,
        ctx: ExecutionContext,
    ):
        # --------------------------------------------------
        # Derive execution folder name
        # --------------------------------------------------
        execution_id = execution_dir.name

        root = self.config.artifacts_root
        successful_root = root / "successful_runs"

        target_dir = successful_root / execution_id
        target_dir.mkdir(parents=True, exist_ok=True)

        # --------------------------------------------------
        # Copy ENTIRE execution directory
        # --------------------------------------------------
        for item in execution_dir.iterdir():
            dest = target_dir / item.name

            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

        # --------------------------------------------------
        # Write final healed script
        # --------------------------------------------------
        AtomicWriter.write(
            target_dir / "final_script.py",
            Path(script_path).read_text(encoding="utf-8"),
        )

        # --------------------------------------------------
        # Write repair report
        # --------------------------------------------------
        report = {
            "final_status": final_status,
            "iterations": iterations,
            "repairs": ctx.repair_history,
            "execution_id": execution_id,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        AtomicWriter.write(
            target_dir / "repair_report.json",
            json.dumps(report, indent=2),
        )

    # ==================================================
    # UTILS
    # ==================================================

    @staticmethod
    def _extract_step_function(step_id: str) -> Optional[str]:
        if "__" not in step_id:
            return None
        return step_id.split("__", 1)[1]


# Backward compatibility aliases
OrchestratorConfig = ExecutorOrchestratorConfig
ExecutionOrchestratorV2_1 = SelfHealingExecutorOrchestrator
