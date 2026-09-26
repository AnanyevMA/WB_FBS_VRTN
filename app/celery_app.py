"""
Celery Application Configuration — WB FBS Manager

Configures Celery instance, broker, result backend, queues, task routing,
beat schedule, retry behavior, and time limits matching agents_config.json.
"""
from celery import Celery
from celery.schedules import crontab
from kombu import Queue

from app.config import settings

# Initialize Celery app with all agent modules included
celery_app = Celery(
    "wbfbs",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.agents.order_poller",
        "app.agents.supply_agent",
        "app.agents.cz_withdrawal",
        "app.agents.cz_return",
        "app.agents.archive_processor",
        "app.agents.notifier",
        "app.agents.cleanup",
        "app.agents.qa_test_agent",
        "app.agents.cz_token_refresher",
        "app.agents.morning_digest",
        "app.agents.kb_sync_agent",
        "app.agents.security_audit_agent",
        "app.agents.auto_kiz_queue_agent",
        "app.agents.wb_warehouse_sales_agent",
    ],
)

# Celery Configuration
celery_app.conf.update(
    # Timezone & Serialization
    timezone="Europe/Moscow",
    enable_utc=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_accept_content=["json"],
    # Queue Definitions matching agents_config.json
    task_queues=[
        Queue("default", routing_key="default"),
        Queue("orders", routing_key="orders"),
        Queue("supplies", routing_key="supplies"),
        Queue("cz_operations", routing_key="cz_operations"),
        Queue("archive", routing_key="archive"),
        Queue("notifications", routing_key="notifications"),
        Queue("maintenance", routing_key="maintenance"),
        Queue("qa_testing", routing_key="qa_testing"),
    ],
    task_default_queue="default",
    task_default_routing_key="default",
    # Task Routing by Agent Namespace
    task_routes={
        "app.agents.order_poller.*": {"queue": "orders"},
        "app.agents.supply_agent.*": {"queue": "supplies"},
        "app.agents.cz_withdrawal.*": {"queue": "cz_operations"},
        "app.agents.cz_return.*": {"queue": "cz_operations"},
        "app.agents.cz_token_refresher.*": {"queue": "cz_operations"},
        "app.agents.archive_processor.*": {"queue": "archive"},
        "app.agents.notifier.*": {"queue": "notifications"},
        "app.agents.morning_digest.*": {"queue": "notifications"},
        "app.agents.cleanup.*": {"queue": "maintenance"},
        "app.agents.kb_sync_agent.*": {"queue": "maintenance"},
        "app.agents.security_audit_agent.*": {"queue": "maintenance"},
        "app.agents.qa_test_agent.*": {"queue": "qa_testing"},
        "app.agents.auto_kiz_queue_agent.*": {"queue": "cz_operations"},
        "app.agents.wb_warehouse_sales_agent.*": {"queue": "cz_operations"},
    },
    # Periodic Tasks Beat Schedule matching agents_config.json
    beat_schedule={
        "poll-new-orders": {
            "task": "app.agents.order_poller.poll_all_sellers",
            "schedule": 60.0,  # every 60 seconds
            "options": {"queue": "orders"},
        },
        "refresh-cz-tokens": {
            "task": "app.agents.cz_token_refresher.refresh_all_tokens",
            "schedule": 1800.0,  # every 30 minutes
            "options": {"queue": "cz_operations"},
        },
        "sync-active-orders-cz-status": {
            "task": "app.agents.cz_token_refresher.sync_active_orders_cz_status",
            "schedule": 1800.0,  # every 30 minutes
            "options": {"queue": "cz_operations"},
        },
        "process-archive-daily": {
            "task": "app.agents.archive_processor.process_all_archives",
            "schedule": crontab(hour=3, minute=30),  # daily at 03:30 Moscow time
            "options": {"queue": "archive"},
        },
        "cleanup-old-logs-weekly": {
            "task": "app.agents.cleanup.cleanup_old_audit_logs",
            "schedule": crontab(hour=4, minute=0, day_of_week=0),  # weekly on Sunday at 04:00
            "options": {"queue": "maintenance"},
        },
        # Runs every 5 min; the agent checks each seller's configured local time and sends digest on-time
        "morning-digest-check": {
            "task": "app.agents.morning_digest.send_morning_digest",
            "schedule": 300.0,  # every 5 minutes (±5 min delivery precision is acceptable)
            "options": {"queue": "notifications"},
        },
        # Runs every 5 min to check sellers with notification_mode='scheduled' against their schedule
        "scheduled-orders-digest-check": {
            "task": "app.agents.notifier.send_scheduled_orders_digest",
            "schedule": 300.0,  # every 5 minutes for scheduled batch delivery
            "options": {"queue": "notifications"},
        },
        # Runs every 6 hours to maintain and validate knowledge base docs and indexes
        "sync-knowledge-base": {
            "task": "app.agents.kb_sync_agent.sync_knowledge_base",
            "schedule": crontab(minute=0, hour="*/6"),
            "options": {"queue": "maintenance"},
        },
        # Runs every 6 hours for continuous security & posture auditing
        "security-audit-check": {
            "task": "app.agents.security_audit_agent.run_security_audit",
            "schedule": crontab(minute=15, hour="*/6"),
            "options": {"queue": "maintenance"},
        },
        # Runs every 5 min to check sellers requiring archive upload reminder (every 2 days at configured time)
        "check-archive-reminders": {
            "task": "app.agents.archive_processor.check_archive_reminders",
            "schedule": 300.0,  # every 5 minutes for on-time delivery
            "options": {"queue": "notifications"},
        },
        # Runs every 5 min to check sellers requiring daily auto KIZ queue (by default at 17:00 local time)
        "daily-auto-kiz-queue-check": {
            "task": "app.agents.auto_kiz_queue_agent.check_and_run_auto_kiz_queue",
            "schedule": 300.0,  # every 5 minutes (grace window 3h, ±5 min is fine)
            "options": {"queue": "cz_operations"},
        },
        # Runs daily at 04:30 to sync repeat sales from WB warehouse (FBO / returns)
        "sync-warehouse-sales-daily": {
            "task": "app.agents.wb_warehouse_sales_agent.sync_all_sellers_warehouse_sales",
            "schedule": crontab(hour=4, minute=30),  # daily at 04:30 Moscow time
            "options": {"queue": "cz_operations"},
        },
    },
    # Task Retry Annotations & Time Limits
    task_annotations={
        "*": {
            "max_retries": 3,
            "retry_backoff": True,
            "retry_backoff_max": 600,
            "retry_jitter": True,
        }
    },
    task_soft_time_limit=300,  # 5 minutes
    task_hard_time_limit=600,  # 10 minutes
)

# Alias for standard Celery runner lookup (`celery -A app.celery_app worker`)
app = celery_app
