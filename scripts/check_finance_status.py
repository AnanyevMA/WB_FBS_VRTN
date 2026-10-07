import asyncio
import json
from datetime import datetime, timezone
from sqlalchemy import select, func, desc

from app.database import AsyncSessionLocal
from app.models.seller import Seller
from app.models.audit import AuditLog
from app.models.wb_finance import WbSalesReportRow
from app.services.encryption import decrypt
from app.services.wb_finance_client import WBFinanceClient

async def check():
    async with AsyncSessionLocal() as db:
        cnt = await db.scalar(select(func.count(WbSalesReportRow.id)))
        max_rr = await db.scalar(select(func.max(WbSalesReportRow.rr_date)))
        max_order = await db.scalar(select(func.max(WbSalesReportRow.order_dt)))
        max_sale = await db.scalar(select(func.max(WbSalesReportRow.sale_dt)))
        max_date_to = await db.scalar(select(func.max(WbSalesReportRow.date_to)))
        
        print("=== DATABASE STATUS ===")
        print(f"Total finance rows in DB: {cnt}")
        print(f"Max rr_date: {max_rr}")
        print(f"Max order_dt: {max_order}")
        print(f"Max sale_dt: {max_sale}")
        print(f"Max date_to: {max_date_to}")

        # Group by report_id
        rep_ids = (await db.execute(
            select(
                WbSalesReportRow.report_id,
                func.count(WbSalesReportRow.id),
                func.min(WbSalesReportRow.rr_date),
                func.max(WbSalesReportRow.rr_date)
            )
            .group_by(WbSalesReportRow.report_id)
            .order_by(desc(func.max(WbSalesReportRow.rr_date)))
            .limit(10)
        )).all()
        print("\nLatest reports in DB:")
        for r_id, count, r_min, r_max in rep_ids:
            print(f"  • Report ID: {r_id} | Rows: {count} | Period: {r_min} .. {r_max}")

        # Audit logs
        logs = (await db.execute(
            select(AuditLog)
            .where(AuditLog.action.ilike("%FINANC%"))
            .order_by(desc(AuditLog.created_at))
            .limit(5)
        )).scalars().all()
        print("\nLatest finance audit logs:")
        for l in logs:
            print(f"  • {l.created_at} | Agent: {l.agent} | Action: {l.action} | Error: {l.error} | Payload: {json.dumps(l.payload, ensure_ascii=False)}")

        # Now test live WB Finance API
        sellers = (await db.execute(select(Seller).where(Seller.is_active == True))).scalars().all()
        print("\n=== LIVE WB FINANCE API CHECK ===")
        for s in sellers:
            print(f"Seller: {s.name} ({s.id})")
            if not s.wb_api_token_encrypted:
                print("  No WB token.")
                continue
            token = decrypt(s.wb_api_token_encrypted)
            
            async with WBFinanceClient(token) as client:
                # 1. Test query for previous week: 2026-09-28 to 2026-10-04
                try:
                    p1 = await client.get_sales_reports_page(
                        date_from="2026-09-28T00:00:00Z",
                        date_to="2026-10-04T23:59:59Z",
                        limit=50
                    )
                    print(f"  Query 2026-09-28..2026-10-04: {len(p1)} rows returned")
                    if p1:
                        rep_id = p1[0].get("realizationreport_id") or p1[0].get("reportId")
                        print(f"    Sample row: report_id={rep_id}, doc_type={p1[0].get('doc_type_name') or p1[0].get('docTypeName')}, rrd_id={p1[0].get('rrd_id') or p1[0].get('rrdId')}")
                except Exception as e:
                    print(f"  Query 2026-09-28..2026-10-04 FAILED: {e}")

                # 2. Test query for week ending 2026-09-27 (the last known in DB)
                try:
                    p0 = await client.get_sales_reports_page(
                        date_from="2026-09-21T00:00:00Z",
                        date_to="2026-09-27T23:59:59Z",
                        limit=5
                    )
                    print(f"  Query 2026-09-21..2026-09-27: {len(p0)} rows returned (sanity check)")
                except Exception as e:
                    print(f"  Query 2026-09-21..2026-09-27 FAILED: {e}")

                # 3. Test query up to today: 2026-09-28 to 2026-10-07
                try:
                    p2 = await client.get_sales_reports_page(
                        date_from="2026-09-28T00:00:00Z",
                        date_to="2026-10-07T23:59:59Z",
                        limit=50
                    )
                    print(f"  Query 2026-09-28..2026-10-07: {len(p2)} rows returned")
                    if p2:
                        rep_id = p2[0].get("realizationreport_id") or p2[0].get("reportId")
                        print(f"    Sample row: report_id={rep_id}, doc_type={p2[0].get('doc_type_name') or p2[0].get('docTypeName')}, rr_date={p2[0].get('rr_dt') or p2[0].get('rrDate')}")
                except Exception as e:
                    print(f"  Query 2026-09-28..2026-10-07 FAILED: {e}")

if __name__ == "__main__":
    asyncio.run(check())
