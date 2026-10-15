import asyncio
import json
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func, desc

from app.database import AsyncSessionLocal
from app.models.seller import Seller
from app.models.order import Order
from app.models.audit import AuditLog
from app.models.wb_finance import WbSalesReportRow

async def run():
    async with AsyncSessionLocal() as db:
        now_utc = datetime.now(timezone.utc)
        since = now_utc - timedelta(hours=48)
        
        print(f"=== DIAGNOSTICS FOR LAST 48 HOURS (since {since.isoformat()} UTC) ===")
        print(f"Current time: {now_utc.isoformat()} UTC")

        # 1. Orders statistics
        total_orders = await db.scalar(select(func.count(Order.id)))
        recent_orders_created = await db.scalar(select(func.count(Order.id)).where(Order.created_at >= since))
        recent_orders_updated = await db.scalar(select(func.count(Order.id)).where(Order.updated_at >= since))
        print(f"\n1. ORDERS:")
        print(f"   • Total in DB: {total_orders}")
        print(f"   • Created in last 48h: {recent_orders_created}")
        print(f"   • Updated in last 48h: {recent_orders_updated}")

        # 2. Audit logs summary
        audit_total_48h = await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.created_at >= since))
        audit_errors_48h = await db.scalar(
            select(func.count(AuditLog.id))
            .where(AuditLog.created_at >= since, AuditLog.error.isnot(None), AuditLog.error != "")
        )
        print(f"\n2. AUDIT LOGS (last 48h):")
        print(f"   • Total entries: {audit_total_48h}")
        print(f"   • Errors recorded: {audit_errors_48h}")

        # Group actions in last 48h
        actions = (await db.execute(
            select(AuditLog.action, func.count(AuditLog.id))
            .where(AuditLog.created_at >= since)
            .group_by(AuditLog.action)
            .order_by(desc(func.count(AuditLog.id)))
        )).all()
        print(f"   • Breakdown by action:")
        for action, count in actions:
            print(f"      - {action}: {count}")

        # Any errors in audit logs?
        if audit_errors_48h > 0:
            error_logs = (await db.execute(
                select(AuditLog)
                .where(AuditLog.created_at >= since, AuditLog.error.isnot(None), AuditLog.error != "")
                .order_by(desc(AuditLog.created_at))
                .limit(10)
            )).scalars().all()
            print(f"\n   [!] Details of latest errors:")
            for e in error_logs:
                print(f"      - {e.created_at} | Agent: {e.agent} | Action: {e.action} | Error: {e.error}")

        # 3. Finance sync checks
        finance_sync_logs = (await db.execute(
            select(AuditLog)
            .where(AuditLog.created_at >= since, AuditLog.action.ilike("%FINANC%"))
            .order_by(desc(AuditLog.created_at))
            .limit(10)
        )).scalars().all()
        print(f"\n3. FINANCE SYNC (last 48h):")
        if not finance_sync_logs:
            print("   • No finance audit logs recorded in last 48h.")
        else:
            for fl in finance_sync_logs:
                p = fl.payload or {}
                print(f"   • {fl.created_at} | Action: {fl.action} | Error: {fl.error} | Payload: {json.dumps(p, ensure_ascii=False)}")

        # Finance rows in DB
        fin_rows_count = await db.scalar(select(func.count(WbSalesReportRow.id)))
        max_rr = await db.scalar(select(func.max(WbSalesReportRow.rr_date)))
        print(f"   • Current total finance rows: {fin_rows_count}")
        print(f"   • Max rr_date in DB: {max_rr}")

if __name__ == "__main__":
    asyncio.run(run())
