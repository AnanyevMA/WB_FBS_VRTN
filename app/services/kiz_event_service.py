"""
KIZ Event Service — WB FBS Manager
Запись событий о КИЗ в журнал kiz_events без дублей.
"""
import logging
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kiz_event import KizEvent

logger = logging.getLogger(__name__)

CHUNK_SIZE = 500
# A later poll may fill in receipt details, but never rewrites what happened and when
FILLABLE_FIELDS = ("srid", "order_id", "price", "article", "name", "receipt_number", "fn_number", "receipt_date")


async def save_events(db: AsyncSession, seller_id: str, drafts: List[Dict[str, Any]]) -> Dict[str, int]:
    """Adds new events and fills empty fields of known ones. The caller commits."""
    unique = {(d["source"], d["external_id"]): d for d in drafts}
    by_source: Dict[str, List[Dict[str, Any]]] = {}
    for draft in unique.values():
        by_source.setdefault(draft["source"], []).append(draft)

    inserted, updated = 0, 0
    for source, items in by_source.items():
        for start in range(0, len(items), CHUNK_SIZE):
            chunk = items[start:start + CHUNK_SIZE]
            new, changed = await _save_chunk(db, seller_id, source, chunk)
            inserted += new
            updated += changed
    return {"inserted": inserted, "updated": updated}


async def _save_chunk(db: AsyncSession, seller_id: str, source: str, chunk: List[Dict[str, Any]]) -> tuple[int, int]:
    existing_rows = await db.execute(
        select(KizEvent).where(
            KizEvent.seller_id == seller_id,
            KizEvent.source == source,
            KizEvent.external_id.in_([d["external_id"] for d in chunk]),
        )
    )
    existing = {row.external_id: row for row in existing_rows.scalars().all()}

    inserted, updated = 0, 0
    for draft in chunk:
        row = existing.get(draft["external_id"])
        if row is None:
            db.add(KizEvent(seller_id=seller_id, **draft))
            inserted += 1
        elif _fill_empty_fields(row, draft):
            updated += 1
    await db.flush()
    return inserted, updated


def _fill_empty_fields(row: KizEvent, draft: Dict[str, Any]) -> bool:
    changed = False
    for field in FILLABLE_FIELDS:
        value = draft.get(field)
        if value not in (None, "") and getattr(row, field) in (None, ""):
            setattr(row, field, value)
            changed = True
    return changed
