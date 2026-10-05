"""
Deduplicates rows in kiz_product_info where the same clean_cis exists multiple times.
Keeps the most up-to-date row (preferring RETIRED status and newest updated_at),
and removes redundant older duplicate rows.
"""
import asyncio
from sqlalchemy import text
from app.database import AsyncSessionLocal

async def dedup():
    async with AsyncSessionLocal() as db:
        res = await db.execute(text("""
            SELECT clean_cis, count(*) as cnt
            FROM kiz_product_info
            WHERE clean_cis IS NOT NULL
            GROUP BY clean_cis
            HAVING count(*) > 1
        """))
        duplicates = res.fetchall()
        print(f"Found {len(duplicates)} duplicate clean_cis groups.")

        deleted_count = 0
        for row in duplicates:
            cis = row[0]
            # Fetch all rows for this clean_cis
            recs_res = await db.execute(text("""
                SELECT id, kiz_code, cz_status, updated_at
                FROM kiz_product_info
                WHERE clean_cis = :cis
                ORDER BY 
                    CASE WHEN cz_status = 'RETIRED' THEN 0 ELSE 1 END,
                    updated_at DESC
            """), {"cis": cis})
            recs = recs_res.fetchall()
            if len(recs) <= 1:
                continue
            
            # Keep the first record (best status and newest)
            best_id = recs[0][0]
            other_ids = [r[0] for r in recs[1:]]
            
            del_res = await db.execute(text("""
                DELETE FROM kiz_product_info
                WHERE id = ANY(:ids)
            """), {"ids": other_ids})
            deleted_count += del_res.rowcount

        await db.commit()
        print(f"Successfully cleaned up {deleted_count} duplicate records in kiz_product_info.")

if __name__ == "__main__":
    asyncio.run(dedup())
