"""
Celery Application Configuration — WB FBS Manager

Configures Celery instance, broker, result backend, queues, task routing,
beat schedule, retry behavior, and time limits matching agents_config.json.
"""
import html
import json
import logging
import os
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from celery import Celery
from celery.exceptions import WorkerLostError
from celery.schedules import crontab
from celery.signals import task_failure
from kombu import Queue

from app.config import settings

logger = logging.getLogger(__name__)

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
        "app.agents.wb_finance_agent",
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
        "app.agents.wb_finance_agent.*": {"queue": "cz_operations"},
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
        # Runs daily at 05:00 to sync detailed sales reports from WB Finance API and audit returns
        "sync-financial-reports-daily": {
            "task": "app.agents.wb_finance_agent.sync_all_sellers_financial_reports",
            "schedule": crontab(hour=5, minute=0),  # daily at 05:00 Moscow time
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


# =============================================================================
# Celery Signals: WorkerLostError / OOM Alerts
# =============================================================================

def send_worker_lost_telegram_alert(
    task_name: str,
    task_id: str,
    exception: Exception,
    args: Optional[tuple] = None,
    kwargs: Optional[dict] = None,
) -> bool:
    """
    Отправляет экстренное уведомление администратору в Telegram при падении воркера (WorkerLostError / SIGKILL / OOM).
    Выполняет прямой синхронный HTTP-запрос к Telegram Bot API без зависимости от очередей Celery.
    """
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from app.models.seller import Seller
    from app.services.encryption import decrypt

    from app.services.telegram_service import filter_private_chats, is_private_chat, get_personal_manager_chats

    seller_id = (kwargs or {}).get("seller_id")
    if not seller_id and args and len(args) > 0 and isinstance(args[0], str):
        seller_id = args[0]

    seller_name = None
    recipients: list[tuple[str, list[str]]] = []

    # 1. Проверяем глобальный токен и чат администратора из конфигурации / .env (только личный чат!)
    admin_bot_token = getattr(settings, "telegram_bot_token", None) or os.getenv("TELEGRAM_BOT_TOKEN")
    admin_chat_id = getattr(settings, "telegram_admin_chat_id", None) or os.getenv("TELEGRAM_ADMIN_CHAT_ID")
    if admin_bot_token and admin_chat_id and is_private_chat(admin_chat_id):
        recipients.append((admin_bot_token.strip(), [str(admin_chat_id).strip()]))

    # 2. Ищем настройки Telegram в БД (целевой селлер или активные селлеры) — строго личные чаты
    try:
        engine = create_engine(settings.database_url_sync)
        with Session(engine) as db:
            if seller_id:
                sel = db.execute(select(Seller).where(Seller.id == str(seller_id))).scalar_one_or_none()
                if sel:
                    seller_name = sel.name
                    if sel.telegram_bot_token_encrypted:
                        try:
                            tok = decrypt(sel.telegram_bot_token_encrypted)
                            chats = get_personal_manager_chats(sel) or filter_private_chats(sel.telegram_chat_ids or [])
                            if chats:
                                recipients.append((tok, chats))
                        except Exception as dec_err:
                            logger.warning(f"[WorkerLost Alert] Decryption failed for seller {seller_id}: {dec_err}")

            if not recipients:
                active_sellers = db.execute(
                    select(Seller).where(
                        Seller.is_active == True,
                        Seller.telegram_bot_token_encrypted.isnot(None),
                    )
                ).scalars().all()
                for s in active_sellers:
                    try:
                        tok = decrypt(s.telegram_bot_token_encrypted)
                        chats = get_personal_manager_chats(s) or filter_private_chats(s.telegram_chat_ids or [])
                        if chats:
                            recipients.append((tok, chats))
                            break
                    except Exception:
                        continue
    except Exception as db_err:
        logger.error(f"[WorkerLost Alert] DB query failed: {db_err}")

    seller_display = f"{seller_name} ({seller_id})" if seller_name else (str(seller_id) if seller_id else "Все / Система")
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    msg_text = (
        "🚨 <b>АВАРИЙНОЕ ЗАВЕРШЕНИЕ CELERY WORKER</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <b>Тип сбоя:</b> <code>WorkerLostError</code> (SIGKILL / OOM)\n"
        f"⚙️ <b>Задача:</b> <code>{html.escape(str(task_name or '—'))}</code>\n"
        f"🆔 <b>Task ID:</b> <code>{html.escape(str(task_id or '—'))}</code>\n"
        f"🏪 <b>Продавец:</b> {html.escape(seller_display)}\n"
        f"⏰ <b>Время:</b> {now_str}\n"
        f"💥 <b>Ошибка:</b> <code>{html.escape(str(exception))}</code>\n\n"
        "🔴 <b>Причина:</b> Дочерний процесс воркера был принудительно убит ядром ОС (SIGKILL / Linux OOM Killer). "
        "Превышен лимит оперативной памяти контейнера <code>wbfbs_worker</code>."
    )

    sent_any = False
    for bot_tok, chat_list in recipients:
        private_chats = filter_private_chats(chat_list)
        for cid in private_chats:
            try:
                payload = json.dumps({
                    "chat_id": cid,
                    "text": msg_text,
                    "parse_mode": "HTML",
                }).encode("utf-8")
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{bot_tok}/sendMessage",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        sent_any = True
            except Exception as http_err:
                logger.error(f"[WorkerLost Alert] HTTP Telegram send failed to {cid}: {http_err}")

    # Запись в аудит-лог
    try:
        with Session(create_engine(settings.database_url_sync)) as db:
            from app.models.audit import AuditLog
            audit = AuditLog(
                id=str(uuid.uuid4()),
                seller_id=str(seller_id) if seller_id else None,
                agent="celery_worker_supervisor",
                action="WORKER_LOST_ALERT",
                entity_type="celery_task",
                entity_id=str(task_id),
                payload={
                    "task_name": str(task_name),
                    "exception": str(exception),
                    "args": [str(a) for a in (args or ())],
                    "kwargs": {k: str(v) for k, v in (kwargs or {}).items()},
                    "sent_telegram": sent_any,
                },
                error=str(exception),
                trace_id=str(task_id),
                created_at=datetime.now(timezone.utc),
            )
            db.add(audit)
            db.commit()
    except Exception as audit_err:
        logger.error(f"[WorkerLost Alert] Failed to write AuditLog: {audit_err}")

    return sent_any


@task_failure.connect
def handle_celery_task_failure(
    sender=None,
    task_id=None,
    exception=None,
    args=None,
    kwargs=None,
    traceback=None,
    einfo=None,
    **extra
):
    """
    Перехватывает аварийные сбои задач в Celery.
    Если задача упала по WorkerLostError (OOM / SIGKILL), немедленно отправляет алерт в Telegram.
    """
    if not isinstance(exception, WorkerLostError):
        return

    task_name = getattr(sender, "name", str(sender)) if sender else "unknown_task"
    logger.critical(
        f"[Celery WorkerLostError] Task {task_name} [{task_id}] terminated unexpectedly (WorkerLostError): {exception}"
    )

    send_worker_lost_telegram_alert(
        task_name=task_name,
        task_id=str(task_id or ""),
        exception=exception,
        args=args,
        kwargs=kwargs,
    )
