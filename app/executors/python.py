from __future__ import annotations
import asyncio
import hashlib
import logging
import os
import sys
import time
import uuid
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.executors.base import BaseExecutor
from app.executors.models import ExecutionOutcome, ExecutionResult
from app.executors.sandbox import ScriptSecurityValidator

logger = logging.getLogger("python_executor")

class SandboxedPythonExecutor:
    """
    Executes Python scripts in a sandboxed environment.
    """
    
    def __init__(
        self,
        timeout_seconds: int = 300,
        max_memory_mb: int = 512,
        use_docker: bool = False,
        strict_validation: bool = False,
    ):
        self.timeout_seconds = timeout_seconds
        self.max_memory_mb = max_memory_mb
        self.use_docker = use_docker and settings.SANDBOX_USE_DOCKER
        self.validator = ScriptSecurityValidator(strict_mode=strict_validation)
    
    async def execute_sandboxed(
        self,
        script_content: str,
        env_vars: Optional[Dict[str, str]] = None,
    ) -> ExecutionResult:
        start_time = time.perf_counter()
        run_id = uuid.uuid4().hex[:8]
        
        if settings.SANDBOX_ENABLED:
            is_valid, error_msg = self.validator.validate(script_content)
            if not is_valid:
                logger.warning(f"Script validation failed: {error_msg}")
                return ExecutionResult(
                    success=False,
                    exit_code=-1,
                    stdout="",
                    stderr=error_msg,
                    duration_ms=0,
                    timed_out=False,
                    command=[],
                    working_dir="",
                    artifacts_dir=None,
                    script_path="<inline>",
                    run_id=run_id,
                    semantic_status="failed",
                    outcome=ExecutionOutcome.VALIDATION_ERROR,
                    error=f"Security validation failed: {error_msg}",
                )
        
        try:
            if self.use_docker:
                result = await self._execute_in_docker(
                    script_content, env_vars, run_id
                )
            else:
                result = await self._execute_in_subprocess(
                    script_content, env_vars, run_id
                )
            
            duration = int((time.perf_counter() - start_time) * 1000)
            result.duration_ms = duration
            
            return result
            
        except asyncio.TimeoutError:
            duration = int((time.perf_counter() - start_time) * 1000)
            return ExecutionResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr=f"Execution timed out after {self.timeout_seconds}s",
                duration_ms=duration,
                timed_out=True,
                command=[],
                working_dir="",
                artifacts_dir=None,
                script_path="<inline>",
                run_id=run_id,
                semantic_status="failed",
                outcome=ExecutionOutcome.TIMEOUT,
                error=f"Execution timed out after {self.timeout_seconds}s",
            )
        except Exception as e:
            duration = int((time.perf_counter() - start_time) * 1000)
            logger.exception("Execution failed with unexpected error")
            return ExecutionResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr=str(e),
                duration_ms=duration,
                timed_out=False,
                command=[],
                working_dir="",
                artifacts_dir=None,
                script_path="<inline>",
                run_id=run_id,
                semantic_status="failed",
                outcome=ExecutionOutcome.FAILURE,
                error=str(e),
            )
    
    async def _execute_in_subprocess(
        self,
        script_content: str,
        env_vars: Optional[Dict[str, str]],
        run_id: str,
    ) -> ExecutionResult:
        import tempfile
        
        with tempfile.TemporaryDirectory(prefix="playwright_sandbox_") as tmpdir:
            script_path = Path(tmpdir) / "test_script.py"
            script_path.write_text(script_content, encoding="utf-8")
            
            artifacts_dir = Path(tmpdir) / "artifacts"
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            
            env = os.environ.copy()
            if env_vars:
                env.update(env_vars)
            
            env["ARTIFACTS_DIR"] = str(artifacts_dir)
            env["RUN_ID"] = run_id
            
            if settings.SANDBOX_ENABLED:
                sensitive_vars = [
                    "AWS_SECRET_ACCESS_KEY",
                    "GOOGLE_API_KEY", 
                    "DATABASE_URL",
                    "API_SECRET_KEY",
                ]
                for var in sensitive_vars:
                    env.pop(var, None)
            
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTHONUNBUFFERED"] = "1"
            
            cmd = [sys.executable, str(script_path)]
            
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=tmpdir,
                    env=env,
                )
                
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout_seconds,
                )
                
                stdout_str = stdout.decode("utf-8", errors="replace")
                stderr_str = stderr.decode("utf-8", errors="replace")
                
                status_file = self._find_status_file(artifacts_dir)
                if status_file:
                    final_status = status_file.read_text(
                        encoding="utf-8"
                    ).strip().lower()
                    success = final_status == "passed"
                    semantic_status = final_status
                else:
                    success = process.returncode == 0
                    semantic_status = "passed" if success else "failed"
                
                step_results = self._parse_step_results(stdout_str)
                
                return ExecutionResult(
                    success=success,
                    exit_code=process.returncode or 0,
                    stdout=stdout_str,
                    stderr=stderr_str,
                    duration_ms=0,
                    timed_out=False,
                    command=cmd,
                    working_dir=tmpdir,
                    artifacts_dir=str(artifacts_dir),
                    script_path=str(script_path),
                    run_id=run_id,
                    semantic_status=semantic_status,
                    outcome=(
                        ExecutionOutcome.SUCCESS if success 
                        else ExecutionOutcome.FAILURE
                    ),
                    step_results=step_results,
                )
                
            except asyncio.TimeoutError:
                process.kill()
                raise
    
    async def _execute_in_docker(
        self,
        script_content: str,
        env_vars: Optional[Dict[str, str]],
        run_id: str,
    ) -> ExecutionResult:
        import tempfile
        
        with tempfile.TemporaryDirectory(prefix="playwright_docker_") as tmpdir:
            script_path = Path(tmpdir) / "test_script.py"
            script_path.write_text(script_content, encoding="utf-8")
            
            artifacts_dir = Path(tmpdir) / "artifacts"
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            
            cmd = [
                "docker", "run",
                "--rm",
                "--network", "none" if not settings.SANDBOX_ALLOW_NETWORK else "bridge",
                "--memory", f"{self.max_memory_mb}m",
                "--cpus", "1",
                "--read-only",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=100m",
                "-v", f"{tmpdir}:/workspace:ro",
                "-v", f"{artifacts_dir}:/artifacts:rw",
                "-w", "/workspace",
                "-e", f"ARTIFACTS_DIR=/artifacts",
                "-e", f"RUN_ID={run_id}",
                "--user", "nobody",
                "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges",
            ]
            
            if env_vars:
                for key, value in env_vars.items():
                    cmd.extend(["-e", f"{key}={value}"])
            
            cmd.extend([
                settings.SANDBOX_DOCKER_IMAGE,
                "python", "/workspace/test_script.py"
            ])
            
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout_seconds,
                )
                
                stdout_str = stdout.decode("utf-8", errors="replace")
                stderr_str = stderr.decode("utf-8", errors="replace")
                
                status_file = self._find_status_file(artifacts_dir)
                if status_file:
                    final_status = status_file.read_text(
                        encoding="utf-8"
                    ).strip().lower()
                    success = final_status == "passed"
                    semantic_status = final_status
                else:
                    success = process.returncode == 0
                    semantic_status = "passed" if success else "failed"
                
                step_results = self._parse_step_results(stdout_str)
                
                return ExecutionResult(
                    success=success,
                    exit_code=process.returncode or 0,
                    stdout=stdout_str,
                    stderr=stderr_str,
                    duration_ms=0,
                    timed_out=False,
                    command=cmd,
                    working_dir=tmpdir,
                    artifacts_dir=str(artifacts_dir),
                    script_path=str(script_path),
                    run_id=run_id,
                    semantic_status=semantic_status,
                    outcome=(
                        ExecutionOutcome.SUCCESS if success
                        else ExecutionOutcome.FAILURE
                    ),
                    step_results=step_results,
                )
                
            except asyncio.TimeoutError:
                raise
    
    @staticmethod
    def _find_status_file(artifacts_root: Path) -> Optional[Path]:
        if not artifacts_root.exists():
            return None
        files = list(artifacts_root.rglob("status.txt"))
        return max(files, key=lambda p: p.stat().st_mtime) if files else None
    
    @staticmethod
    def _parse_step_results(stdout: str) -> List[Dict[str, Any]]:
        import json
        results = []
        for line in stdout.split("\n"):
            line = line.strip()
            if line.startswith('{"step_'):
                try:
                    result = json.loads(line)
                    results.append(result)
                except json.JSONDecodeError:
                    pass
            elif line.startswith("STEP_RESULT:"):
                parts = line.split(":", 3)
                if len(parts) >= 3:
                    results.append({
                        "step_id": parts[1],
                        "status": parts[2],
                        "message": parts[3] if len(parts) > 3 else "",
                    })
        return results


class AsyncPythonExecutor(BaseExecutor):
    """
    ARTIFACT-DRIVEN ASYNC PYTHON EXECUTOR (WINDOWS SAFE)
    """

    def __init__(
        self,
        timeout_seconds: int = 6000,
        python_binary: Optional[str] = None,
        base_work_dir: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        enable_sandbox: bool = True,
    ):
        self.timeout_seconds = timeout_seconds
        self.python_binary = python_binary or sys.executable
        self.base_work_dir = Path(base_work_dir or os.getcwd())
        self.base_env = env or {}
        self.enable_sandbox = enable_sandbox
        self.validator = ScriptSecurityValidator()

    async def execute(
        self,
        script_path: str,
        *,
        args: Optional[List[str]] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> ExecutionResult:
        start_time = time.perf_counter()
        script = Path(script_path).resolve()
        self._validate_script(script)

        run_id = uuid.uuid4().hex[:8]
        run_dir = self.base_work_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        if self.enable_sandbox and settings.ENABLE_SANDBOX_EXECUTION:
            script_content = script.read_text(encoding="utf-8")
            is_valid, error_msg = self.validator.validate(script_content)
            if not is_valid:
                logger.warning(f"Script validation failed: {error_msg}")
                return ExecutionResult(
                    success=False,
                    exit_code=-1,
                    stdout="",
                    stderr=error_msg,
                    duration_ms=0,
                    timed_out=False,
                    command=[],
                    working_dir=str(run_dir),
                    artifacts_dir=None,
                    script_path=str(script),
                    run_id=run_id,
                    semantic_status="failed",
                    outcome=ExecutionOutcome.VALIDATION_ERROR,
                    error=f"Security validation failed: {error_msg}",
                )

        cmd = [self.python_binary, str(script)]
        if args:
            cmd.extend(args)

        env = os.environ.copy()
        env.update(self.base_env)
        if extra_env:
            env.update(extra_env)

        env["RUN_ID"] = run_id

        artifacts_root = run_dir / "artifacts"
        artifacts_root.mkdir(parents=True, exist_ok=True)
        env["ARTIFACTS_DIR"] = str(artifacts_root)

        try:
            result = await asyncio.to_thread(
                self._run_subprocess,
                cmd=cmd,
                cwd=str(run_dir),
                env=env,
                timeout=self.timeout_seconds,
            )

            duration_ms = self._duration_ms(start_time)

            stdout = result.stdout or ""
            stderr = result.stderr or ""

            status_file = self._find_status_file(artifacts_root)

            if not status_file:
                return ExecutionResult(
                    success=False,
                    exit_code=result.returncode,
                    stdout=stdout,
                    stderr=stderr,
                    duration_ms=duration_ms,
                    timed_out=False,
                    command=cmd,
                    working_dir=str(run_dir),
                    artifacts_dir=str(artifacts_root),
                    script_path=str(script),
                    run_id=run_id,
                    semantic_status="unknown",
                    outcome=ExecutionOutcome.UNKNOWN,
                )

            final_status = status_file.read_text(
                encoding="utf-8"
            ).strip().lower()

            success = final_status == "passed"

            return ExecutionResult(
                success=success,
                exit_code=result.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_ms=duration_ms,
                timed_out=False,
                command=cmd,
                working_dir=str(run_dir),
                artifacts_dir=str(artifacts_root),
                script_path=str(script),
                run_id=run_id,
                semantic_status=final_status,
                outcome=(
                    ExecutionOutcome.SUCCESS if success
                    else ExecutionOutcome.FAILURE
                ),
            )

        except subprocess.TimeoutExpired:
            duration_ms = self._duration_ms(start_time)

            return ExecutionResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="Execution timed out",
                duration_ms=duration_ms,
                timed_out=True,
                command=cmd,
                working_dir=str(run_dir),
                artifacts_dir=str(artifacts_root),
                script_path=str(script),
                run_id=run_id,
                semantic_status="timeout",
                outcome=ExecutionOutcome.TIMEOUT,
            )

        except Exception as exc:
            duration_ms = self._duration_ms(start_time)

            return ExecutionResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                duration_ms=duration_ms,
                timed_out=False,
                command=cmd,
                working_dir=str(run_dir),
                artifacts_dir=str(artifacts_root),
                script_path=str(script),
                run_id=run_id,
                semantic_status="error",
                outcome=ExecutionOutcome.FAILURE,
                error=str(exc),
            )

    @staticmethod
    def _run_subprocess(*, cmd, cwd, env, timeout):
        kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": cwd,
            "env": env,
            "text": True,
            "timeout": timeout,
        }
        if sys.platform != "win32":
            kwargs["start_new_session"] = True
        else:
            # Prevent console window popup on Windows
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        return subprocess.run(cmd, **kwargs)

    @staticmethod
    def _validate_script(script: Path):
        if not script.exists():
            raise FileNotFoundError(script)
        if script.suffix != ".py":
            raise ValueError("Only .py scripts allowed")

    @staticmethod
    def _duration_ms(start: float) -> int:
        return int((time.perf_counter() - start) * 1000)

    @staticmethod
    def _find_status_file(artifacts_root: Path) -> Optional[Path]:
        if not artifacts_root.exists():
            return None
        files = list(artifacts_root.rglob("status.txt"))
        return max(files, key=lambda p: p.stat().st_mtime) if files else None


def compute_script_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]

PythonExecutor = AsyncPythonExecutor
