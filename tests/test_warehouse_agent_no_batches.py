"""
Tests for the nightly warehouse agent without signature batches.
The agent only updates order statuses: its batches missed fresh returns and repeat sales.
"""
import random
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.wb_warehouse_sales_agent import sync_engine, sync_seller_warehouse_sales
from app.database import AsyncSessionLocal, init_db
from app.models.audit import AuditLog
from app.models.kiz import KizProductInfo, KizSignatureBatch
from app.models.order import KizStatus, Order, OrderStatus
from app.models.seller import Seller
from app.services.wb_warehouse_sales_service import process_warehouse_sales_for_seller

INN = "190207495060"
SERVICE = "app.services.wb_warehouse_sales_service"


@pytest.fixture(autouse=True)
async def setup_test_db():
    await init_db()


def _code() -> str:
    return "0104630199251318215" + uuid.uuid4().hex[:12].upper()


def _excise_row(code: str) -> dict:
    return {
        "excise_short": code, "fiscal_doc_number": 100, "fiscal_drive_number": "FN1",
        "fiscal_dt": "2026-10-29", "price": 3000, "nm_id": 1, "srid": f"srid-{code[-6:]}",
    }


def _record(code: str, status: str) -> KizProductInfo:
    return KizProductInfo(
        kiz_code=code, clean_cis=code, cz_status=status, cz_owner_inn=INN,
        raw_cz_payload={"ownerInn": INN, "status": status},
    )


async def _batches(db, seller_id: str) -> int:
    return (await db.execute(
        select(func.count()).select_from(KizSignatureBatch).where(KizSignatureBatch.seller_id == seller_id)
    )).scalar_one()


@pytest.mark.asyncio
async def test_status_sync_creates_no_batch_and_sends_no_notification():
    async with AsyncSessionLocal() as db:
        seller = Seller(id=str(uuid.uuid4()), name="Warehouse", wb_api_token_encrypted="mock", cz_inn=INN)
        db.add(seller)
        await db.commit()
        sold, retired = _code(), _code()
        verified = {sold: _record(sold, "INTRODUCED"), retired: _record(retired, "RETIRED")}

        with patch(f"{SERVICE}.fetch_wb_excise_data", AsyncMock(return_value=[_excise_row(sold), _excise_row(retired)])), \
             patch(f"{SERVICE}.batch_verify_and_sync_cises", AsyncMock(return_value=verified)), \
             patch(f"{SERVICE}._send_telegram_notification", new_callable=AsyncMock) as notify:
            result = await process_warehouse_sales_for_seller(seller=seller, db=db, create_batch=False)

        notify.assert_not_called()
        assert result["created"] is False
        assert result["sales_seen"] == 1
        assert result["already_retired_count"] == 1
        assert await _batches(db, seller.id) == 0
        audit = (await db.execute(select(AuditLog).where(
            AuditLog.seller_id == seller.id, AuditLog.action == "WAREHOUSE_SALES_STATUS_SYNC",
        ))).scalars().one()
        assert audit.payload["sales_seen"] == 1


@pytest.mark.asyncio
async def test_status_sync_still_updates_order_status():
    async with AsyncSessionLocal() as db:
        seller = Seller(id=str(uuid.uuid4()), name="Warehouse", wb_api_token_encrypted="mock", cz_inn=INN)
        db.add(seller)
        code = _code()
        order_id = random.randint(10**9, 10**12)
        db.add(Order(
            id=order_id, seller_id=seller.id, status=OrderStatus.DELIVERED, wb_status="sold", kiz_code=code,
            kiz_status=KizStatus.VALIDATED, wb_created_at=datetime.now(timezone.utc), price=3000,
        ))
        await db.commit()

        with patch(f"{SERVICE}.fetch_wb_excise_data", AsyncMock(return_value=[_excise_row(code)])), \
             patch(f"{SERVICE}.batch_verify_and_sync_cises", AsyncMock(return_value={code: _record(code, "RETIRED")})):
            await process_warehouse_sales_for_seller(seller=seller, db=db, create_batch=False)

        assert (await db.get(Order, order_id)).kiz_status == KizStatus.WITHDRAWN


def test_nightly_agent_does_not_request_batches():
    seller_id = str(uuid.uuid4())
    with Session(sync_engine) as db:
        db.add(Seller(id=seller_id, name="Agent", wb_api_token_encrypted="mock", cz_inn=INN))
        db.commit()

    process = AsyncMock(return_value={"success": True, "created": False})
    with patch("app.agents.wb_warehouse_sales_agent.process_warehouse_sales_for_seller", process):
        sync_seller_warehouse_sales(seller_id=seller_id)

    process.assert_awaited_once()
    assert process.await_args.kwargs["create_batch"] is False
