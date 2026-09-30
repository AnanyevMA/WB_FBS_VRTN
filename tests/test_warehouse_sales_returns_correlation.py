"""
Tests for Warehouse Sales and Returns Correlation (WbSalesReportRow cross-check).
Verifies that sales in excise report that were subsequently returned in finance reports
are skipped from withdrawal batches.
"""
import uuid
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

from app.database import AsyncSessionLocal, init_db
from app.models.seller import Seller
from app.models.wb_finance import WbSalesReportRow
from app.services.encryption import encrypt
from app.services.wb_warehouse_sales_service import process_warehouse_sales_for_seller


@pytest.mark.asyncio
async def test_warehouse_sales_skips_returned_srid():
    """Verify that a sale in excise report matching a RETURN row in finance reports is skipped."""
    await init_db()
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Return Correlation Seller",
            wb_api_token_encrypted=encrypt("wb_tok"),
            cz_inn="190207495060",
            is_active=True,
        )
        session.add(seller)

        # Add finance report row with doc_type_name="Возврат" for the same srid
        test_cis = "0104630199251844215a&hTOsiaepo1"
        test_srid = "eBQ.rd91e22f284234ab39bb4606c0a2b434b.0.0"
        ret_row = WbSalesReportRow(
            id=str(uuid.uuid4()),
            seller_id=seller_id,
            rrd_id=123456789,
            clean_cis=test_cis,
            srid=test_srid,
            doc_type_name="Возврат",
            retail_amount=4641.0,
            rr_date=datetime.now(timezone.utc).date(),
        )
        session.add(ret_row)
        await session.commit()

        mock_excise_rows = [
            {
                "barcode": "2044967817804",
                "excise_short": test_cis,
                "fiscal_doc_number": 171782,
                "fiscal_drive_number": "7380440903834812",
                "fiscal_dt": "2026-09-14",
                "nm_id": 492864166,
                "price": 4641.0,
                "srid": test_srid,
            }
        ]

        mock_kinfo = MagicMock()
        mock_kinfo.cz_status = "INTRODUCED"
        mock_kinfo.cz_status_ex = None
        mock_kinfo.raw_cz_payload = {"ownerInn": "190207495060"}

        with patch("app.services.wb_warehouse_sales_service.fetch_wb_excise_data", new_callable=AsyncMock) as mock_fetch, \
             patch("app.services.wb_warehouse_sales_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_verify:
            mock_fetch.return_value = mock_excise_rows
            mock_verify.return_value = {test_cis: mock_kinfo}

            res = await process_warehouse_sales_for_seller(seller, session, days=30)
            # The sale was returned, so NO withdrawal batch should be created!
            assert res["created"] is False
            assert res["needs_withdrawal_count"] == 0
