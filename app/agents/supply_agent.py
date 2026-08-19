"""
Supply Manager Agent — создание и управление поставками WB FBS
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import create_engine, select, and_
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import settings
from app.models.order import Order, OrderStatus
from app.models.supply import Supply, SupplyStatus
from app.models.seller import Seller
from app.models.audit import AuditLog
from app.services.encryption import decrypt

logger = logging.getLogger(__name__)
sync_engine = create_engine(settings.database_url_sync)


@celery_app.task(
    name="app.agents.supply_agent.create_supply_for_seller",
    queue="supplies",
    bind=True,
    max_retries=3,
)
def create_supply_for_seller(
    self,
    seller_id: str,
    order_ids: list[int],
    supply_name: Optional[str] = None,
):
    """
    Создаёт поставку WB и добавляет в неё указанные заказы.

    Flow:
    1. Create supply via WB API
    2. For each order: attach KIZ if required, add to supply
    3. Validate all KIZ via meta endpoint
    4. If validation OK → deliver supply
    5. Notify manager
    """
    import asyncio

    if not order_ids:
        logger.warning(f"No orders provided for supply creation (seller: {seller_id})")
        return

    with Session(sync_engine) as db:
        seller = db.execute(select(Seller).where(Seller.id == seller_id)).scalar_one_or_none()
        if not seller:
            logger.error(f"Seller {seller_id} not found")
            return

        wb_token = decrypt(seller.wb_api_token_encrypted)
        auto_name = supply_name or f"WB FBS {datetime.now().strftime('%d.%m.%Y %H:%M')}"

        try:
            result = asyncio.run(_create_and_deliver_supply(
                wb_token=wb_token,
                order_ids=order_ids,
                supply_name=auto_name,
            ))

            wb_supply_id = result["wb_supply_id"]
            orders_added = result["orders_added"]

            # Save supply to DB
            supply = Supply(
                seller_id=seller_id,
                wb_supply_id=wb_supply_id,
                name=auto_name,
                status=SupplyStatus.DELIVERING,
                delivered_at=datetime.now(timezone.utc),
            )
            db.add(supply)
            db.flush()

            # Update orders
            for order_id in orders_added:
                order = db.execute(select(Order).where(Order.id == order_id)).scalar_one_or_none()
                if order:
                    order.supply_id = supply.id
                    order.wb_supply_id = wb_supply_id
                    order.status = OrderStatus.DELIVERING
                    order.updated_at = datetime.now(timezone.utc)

            _log_audit(db, seller_id, "supply_agent", "SUPPLY_DELIVERED",
                       "supply", wb_supply_id,
                       payload={"orders_count": len(orders_added)})
            db.commit()

            logger.info(
                f"[Supply] Created {wb_supply_id} with {len(orders_added)} orders "
                f"for seller {seller_id}"
            )

            # Notify manager
            from app.agents.notifier import send_supply_notification
            send_supply_notification.delay(
                seller_id=seller_id,
                supply_id=wb_supply_id,
                orders_count=len(orders_added),
            )

        except Exception as exc:
            error_msg = str(exc)
            logger.error(f"[Supply] Failed for seller {seller_id}: {error_msg}")
            _log_audit(db, seller_id, "supply_agent", "SUPPLY_FAILED",
                       "supply", "N/A", error=error_msg)
            db.commit()
            raise self.retry(exc=exc, countdown=30)


async def _create_and_deliver_supply(
    wb_token: str,
    order_ids: list[int],
    supply_name: str,
) -> dict:
    """Async supply creation and delivery."""
    from app.services.wb_client import WBClient

    async with WBClient(wb_token) as client:
        # Step 1: Create supply
        supply_data = await client.create_supply(name=supply_name)
        wb_supply_id = supply_data["id"]
        logger.info(f"Created WB supply: {wb_supply_id}")

        # Step 2: Add orders to supply
        orders_added = []
        for order_id in order_ids:
            try:
                await client.add_order_to_supply(wb_supply_id, order_id)
                orders_added.append(order_id)
            except Exception as e:
                logger.error(f"Failed to add order {order_id} to supply: {e}")

        if not orders_added:
            raise RuntimeError("No orders could be added to supply")

        # Step 3: Validate KIZ (meta check)
        meta = await client.get_orders_meta(orders_added)
        meta_items = meta.get("meta", []) if isinstance(meta, dict) else (meta if isinstance(meta, list) else [])
        
        # Check for MetaValidationFail issues (sgtinStatus in REJECTED / INVALID)
        problematic = []
        if isinstance(meta_items, list):
            for mdata in meta_items:
                if isinstance(mdata, dict):
                    oid = mdata.get("orderId") or mdata.get("id")
                    status = (mdata.get("sgtinStatus") or mdata.get("kizStatus") or "").upper()
                    if status in ("REJECTED", "INVALID", "ERROR"):
                        if oid:
                            problematic.append(int(oid))
        elif isinstance(meta, dict):
            for oid, mdata in meta.items():
                if isinstance(mdata, dict) and (mdata.get("kizStatus") or "").upper() in ("REJECTED", "INVALID", "ERROR"):
                    problematic.append(int(oid))

        if problematic:
            logger.warning(f"Orders with KIZ issues: {problematic}")
            # Remove problematic orders from supply (they'll need manual fix)
            for oid in problematic:
                if oid in orders_added:
                    orders_added.remove(oid)

        if not orders_added:
            raise RuntimeError("All orders have KIZ validation failures")

        # Step 4: Deliver supply
        await client.deliver_supply(wb_supply_id)
        logger.info(f"Supply {wb_supply_id} delivered with {len(orders_added)} orders")

        return {
            "wb_supply_id": wb_supply_id,
            "orders_added": orders_added,
        }


def _log_audit(db, seller_id, agent, action, entity_type, entity_id,
               payload=None, error=None):
    import uuid
    log = AuditLog(
        seller_id=seller_id, agent=agent, action=action,
        entity_type=entity_type, entity_id=entity_id,
        payload=payload, error=error, trace_id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc),
    )
    db.add(log)
