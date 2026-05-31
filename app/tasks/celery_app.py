# app/tasks/celery_app.py
"""
Celery application configuration.
"""

from celery import Celery
from kombu import Queue, Exchange

from app.core.config import settings


# ==============================================================================
# CELERY APPLICATION
# ==============================================================================

celery_app = Celery(
    "repair_engine",
    broker=settings.celery.broker_url,
    backend=settings.celery.result_backend,
    include=[
        "app.tasks.repair_tasks",
    ],
)


# ==============================================================================
# CELERY CONFIGURATION
# ==============================================================================

celery_app.conf.update(
    # Task settings
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    
    # Task execution settings
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_time_limit=settings.celery.task_time_limit,
    task_soft_time_limit=settings.celery.task_soft_time_limit,
    
    # Worker settings
    worker_prefetch_multiplier=settings.celery.worker_prefetch_multiplier,
    worker_concurrency=settings.celery.worker_concurrency,
    worker_max_tasks_per_child=1000,
    
    # Result backend settings
    result_expires=3600,  # 1 hour
    result_extended=True,
    
    # Retry settings
    task_default_retry_delay=5,
    task_max_retries=3,
    
    # Beat scheduler (for periodic tasks)
    beat_schedule={
        "cleanup-old-fingerprints": {
            "task": "app.tasks.repair_tasks.cleanup_old_fingerprints",
            "schedule": 3600.0,  # Every hour
        },
        "collect-metrics": {
            "task": "app.tasks.repair_tasks.collect_metrics",
            "schedule": 60.0,  # Every minute
        },
    },
    
    # Task routes for priority queues
    task_routes={
        "app.tasks.repair_tasks.repair_step_async": {"queue": "repair"},
        "app.tasks.repair_tasks.execute_script_async": {"queue": "execution"},
        "app.tasks.repair_tasks.batch_repair_steps": {"queue": "batch"},
        "app.tasks.repair_tasks.cleanup_old_fingerprints": {"queue": "maintenance"},
        "app.tasks.repair_tasks.collect_metrics": {"queue": "maintenance"},
    },
    
    # Queue definitions
    task_queues=(
        Queue("repair", Exchange("repair"), routing_key="repair",
              queue_arguments={"x-max-priority": 10}),
        Queue("execution", Exchange("execution"), routing_key="execution",
              queue_arguments={"x-max-priority": 10}),
        Queue("batch", Exchange("batch"), routing_key="batch"),
        Queue("maintenance", Exchange("maintenance"), routing_key="maintenance"),
    ),
    
    # Default queue
    task_default_queue="repair",
    task_default_exchange="repair",
    task_default_routing_key="repair",
)


# ==============================================================================
# TASK BASE CLASS
# ==============================================================================

class BaseTask(celery_app.Task):
    """
    Base task class with common error handling and logging.
    """
    
    abstract = True
    
    def on_success(self, retval, task_id, args, kwargs):
        """Called when task succeeds."""
        pass
    
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Called when task fails."""
        from app.core.metrics import MetricsCollector
        
        # Record failure metric
        MetricsCollector.record_task_failure(self.name, type(exc).__name__)
    
    def on_retry(self, exc, task_id, args, kwargs, einfo):
        """Called when task is retried."""
        from app.core.metrics import MetricsCollector
        
        # Record retry metric
        MetricsCollector.record_task_retry(self.name)


celery_app.Task = BaseTask
