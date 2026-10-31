"""
Celery Beat Schedule — WB FBS Manager
Расписание периодических задач. Частый опрос (60 сек) разрешён только для poll-new-orders,
остальные задачи запускаются не чаще раза в 300 секунд (PROJECT_RULES.md, раздел 5).
"""
from celery.schedules import crontab

BEAT_SCHEDULE = {
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
    "sync-archive-api-daily": {
        "task": "app.agents.archive_processor.sync_all_sellers_archive_api_orders",
        "schedule": crontab(hour=3, minute=0),  # daily at 03:00 Moscow time (3 months window)
        "options": {"queue": "archive"},
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
    # Runs every 5 min to check sellers requiring auto KIZ queue (by default every 6 hours)
    "daily-auto-kiz-queue-check": {
        "task": "app.agents.auto_kiz_queue_agent.check_and_run_auto_kiz_queue",
        "schedule": 300.0,  # every 5 minutes (grace window adapted to interval, ±5 min is fine)
        "options": {"queue": "cz_operations"},
    },
    # Runs daily at 04:30 to sync repeat sales from WB warehouse (FBO / returns)
    "sync-warehouse-sales-daily": {
        "task": "app.agents.wb_warehouse_sales_agent.sync_all_sellers_warehouse_sales",
        "schedule": crontab(hour=4, minute=30),  # daily at 04:30 Moscow time
        "options": {"queue": "cz_operations"},
    },
    # Runs daily at 05:00 to sync detailed sales reports from WB Finance API and audit returns
    "sync-financial-reports-daily": {
        "task": "app.agents.wb_finance_agent.sync_all_sellers_financial_reports",
        "schedule": crontab(hour=5, minute=0),  # daily at 05:00 Moscow time
        "options": {"queue": "cz_operations"},
    },
    # Runs every 5 min to check sellers due for item returns sync (6h interval)
    "check-item-returns-schedule": {
        "task": "app.agents.wb_item_returns_agent.check_and_sync_item_returns",
        "schedule": 300.0,
        "options": {"queue": "cz_operations"},
    },
    # KIZ event log: orders are local data, WB reports have request limits
    "kiz-events-orders": {
        "task": "app.agents.kiz_events_agent.collect_kiz_events",
        "schedule": 1800.0,
        "args": (["fbs_orders"],),
        "options": {"queue": "cz_operations"},
    },
    "kiz-events-wb-reports": {
        "task": "app.agents.kiz_events_agent.collect_kiz_events",
        "schedule": crontab(minute=20, hour="*/6"),
        "args": (["wb_excise", "wb_item_returns"],),
        "options": {"queue": "cz_operations"},
    },
    # After sync-financial-reports-daily at 05:00, which also retries for a few minutes
    "kiz-events-finance": {
        "task": "app.agents.kiz_events_agent.collect_kiz_events",
        "schedule": crontab(hour=5, minute=40),
        "args": (["wb_finance"],),
        "options": {"queue": "cz_operations"},
    },
    "kiz-events-order-statuses": {
        "task": "app.agents.kiz_events_agent.collect_kiz_events",
        "schedule": crontab(hour=3, minute=15),
        "args": (["wb_order_status"],),
        "options": {"queue": "cz_operations"},
    },
}
