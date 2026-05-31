"""
API v1 - Health Routes

Provides health check endpoints for monitoring and orchestration.
"""

from typing import Optional
import logging
import time

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from app.core.health import HealthChecker, HealthStatus

logger = logging.getLogger("api.v1.health")

router = APIRouter()


# =============================================================================
# Response Models
# =============================================================================

class ComponentHealth(BaseModel):
    """Health status of a single component."""
    
    status: str = Field(description="Component status: healthy, degraded, unhealthy")
    latency_ms: Optional[int] = Field(description="Response latency in milliseconds")
    message: Optional[str] = Field(description="Additional status message")
    
    class Config:
        json_schema_extra = {
            "example": {
                "status": "healthy",
                "latency_ms": 15,
                "message": None
            }
        }


class HealthResponse(BaseModel):
    """Complete health check response."""
    
    status: str = Field(description="Overall status: healthy, degraded, unhealthy")
    version: str = Field(description="Application version")
    uptime_seconds: int = Field(description="Application uptime in seconds")
    checks: dict[str, ComponentHealth] = Field(description="Individual component checks")
    
    class Config:
        json_schema_extra = {
            "example": {
                "status": "healthy",
                "version": "2.1.0",
                "uptime_seconds": 86400,
                "checks": {
                    "llm": {"status": "healthy", "latency_ms": 45, "message": None},
                    "database": {"status": "healthy", "latency_ms": 12, "message": None},
                    "redis": {"status": "healthy", "latency_ms": 3, "message": None},
                    "disk": {"status": "healthy", "latency_ms": 1, "message": "85% free"},
                    "memory": {"status": "healthy", "latency_ms": 0, "message": "45% used"}
                }
            }
        }


# =============================================================================
# Endpoints
# =============================================================================

@router.get(
    "",
    response_model=HealthResponse,
    summary="Full health check",
    description="""
    Comprehensive health check of all system components.
    
    Checks include:
    - **LLM Service** - Google Gemini API connectivity
    - **Database** - PostgreSQL connection and query
    - **Redis** - Cache service connectivity
    - **Disk** - Available disk space
    - **Memory** - Available system memory
    
    Returns 200 if healthy/degraded, 503 if unhealthy.
    """,
    responses={
        200: {"description": "System is healthy or degraded"},
        503: {"description": "System is unhealthy"},
    }
)
async def health_check(response: Response):
    """
    Perform a comprehensive health check.
    """
    checker = HealthChecker.get_instance()
    result = await checker.check_all()
    
    # Set appropriate status code
    if result.status == HealthStatus.UNHEALTHY:
        response.status_code = 503
    
    # Convert to response format
    checks = {}
    for name, check in result.checks.items():
        checks[name] = ComponentHealth(
            status=check.status.value,
            latency_ms=check.latency_ms,
            message=check.message,
        )
    
    return HealthResponse(
        status=result.status.value,
        version=result.version,
        uptime_seconds=result.uptime_seconds,
        checks=checks,
    )


@router.get(
    "/live",
    summary="Liveness probe",
    description="Simple liveness check for Kubernetes. Returns 200 if the process is running.",
    responses={
        200: {"description": "Process is alive"},
    }
)
async def liveness_probe():
    """
    Kubernetes liveness probe.
    
    Returns 200 if the process is running. Does not check dependencies.
    """
    return {"status": "alive"}


@router.get(
    "/ready",
    summary="Readiness probe",
    description="Readiness check for Kubernetes. Returns 200 only if the service can accept traffic.",
    responses={
        200: {"description": "Service is ready to accept traffic"},
        503: {"description": "Service is not ready"},
    }
)
async def readiness_probe(response: Response):
    """
    Kubernetes readiness probe.
    
    Returns 200 only if critical dependencies are available.
    """
    checker = HealthChecker.get_instance()
    
    # Check only critical dependencies for readiness
    critical_checks = await checker.check_critical()
    
    if not critical_checks.is_ready:
        response.status_code = 503
        return {
            "status": "not_ready",
            "reason": critical_checks.reason,
        }
    
    return {"status": "ready"}


@router.get(
    "/startup",
    summary="Startup probe",
    description="Startup check for Kubernetes. Returns 200 once initial startup is complete.",
    responses={
        200: {"description": "Startup complete"},
        503: {"description": "Still starting up"},
    }
)
async def startup_probe(response: Response):
    """
    Kubernetes startup probe.
    
    Returns 200 once the application has completed initial startup.
    """
    checker = HealthChecker.get_instance()
    
    if not checker.startup_complete:
        response.status_code = 503
        return {"status": "starting"}
    
    return {"status": "started"}
