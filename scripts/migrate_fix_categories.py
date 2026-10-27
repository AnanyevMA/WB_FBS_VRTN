import asyncio
from app.database import AsyncSessionLocal
from app.national_catalog.models import ProductCard
from app.national_catalog.router import resolve_legprom_category
from sqlalchemy import select

async def main():
    async with AsyncSessionLocal() as session:
        # 1. Fix all cards with category_id >= 500000 or invalid category
        stmt = select(ProductCard).where(ProductCard.category_id >= 500000)
        res = await session.execute(stmt)
        cards = res.scalars().all()
        print(f"Found {len(cards)} cards with category_id >= 500000 to fix:")
        fixed_count = 0
        for c in cards:
            old_cat = c.category_id
            old_name = c.category_name
            new_cat, new_name = resolve_legprom_category(c.category_id, c.tnved, c.category_name)
            c.category_id = new_cat
            c.category_name = new_name
            print(f"  Card {c.id} ({c.name}): {old_cat} -> {new_cat} ({new_name})")
            fixed_count += 1

        # 2. Reset the 3 rejected cards from feed 502425843 to draft so they can be re-sent
        stmt_rejected = select(ProductCard).where(ProductCard.feed_id == 502425843)
        res_rej = await session.execute(stmt_rejected)
        rej_cards = res_rej.scalars().all()
        print(f"\nResetting {len(rej_cards)} rejected cards from feed 502425843:")
        for rc in rej_cards:
            rc.category_id = 30694
            rc.category_name = "Джемперы, свитеры, пуловеры"
            rc.status = "draft"
            rc.feed_id = None
            rc.feed_status = None
            if hasattr(rc, "error_details"):
                rc.error_details = None
            print(f"  Reset card {rc.id} ({rc.name}) to draft with category 30694")

        await session.commit()
        print(f"\nSuccessfully migrated {fixed_count} cards and reset {len(rej_cards)} rejected cards.")

if __name__ == "__main__":
    asyncio.run(main())
