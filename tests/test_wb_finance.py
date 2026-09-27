"""
Unit and Integration Tests for WB Finance Sales Reports & Return KIZ Audit.
Verifies WBFinanceClient, wb_finance_service, Celery tasks, and API endpoints.
"""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from httpx import Response
from fastapi.testclient import TestClient

from app.main import app
from app.database import AsyncSessionLocal, init_db
from app.models.seller import Seller
from app.models.wb_finance import WbSalesReportRow
from app.models.kiz import KizProductInfo
from app.services.encryption import encrypt
from app.services.auth_service import create_access_token, ensure_initial_admin
from app.services.wb_finance_client import (
    WBFinanceClient,
    WBFinanceRateLimitError,
    WBFinanceUnauthorizedError,
)
from app.services.wb_finance_service import (
    _parse_decimal,
    _parse_iso_datetime,
    _parse_iso_date,
    _map_raw_row_to_dict,
    sync_seller_financial_reports,
)
from app.agents.wb_finance_agent import (
    sync_all_sellers_financial_reports,
    sync_seller_financial_reports_task,
)


@pytest.fixture(autouse=True)
async def setup_test_db():
    await init_db()
    async with AsyncSessionLocal() as session:
        await ensure_initial_admin(session)


def test_helpers_parsing():
    assert _parse_decimal("150.50") == Decimal("150.50")
    assert _parse_decimal("150,50") == Decimal("150.50")
    assert _parse_decimal(None) == Decimal("0.00")
    assert _parse_decimal("") == Decimal("0.00")

    dt = _parse_iso_datetime("2026-08-01T10:00:00Z")
    assert dt is not None
    assert dt.year == 2026

    d = _parse_iso_date("2026-08-01")
    assert d is not None
    assert d.day == 1

    # Map raw row with KIZ
    raw = {
        "rrdId": 1234567,
        "docTypeName": "Возврат",
        "sellerOperName": "Возврат",
        "kiz": "0104630199251844215f7iE!r2G:fR\x1d91EE10\x1d92qXlY5R6rFqV070H88R781DsmF+jLqHw+K7Y4f4h/lFc=",
        "nmId": 234298132,
        "retailAmount": 790.0,
        "srid": "srid_ret_123",
        "orderDt": "2026-08-01T10:48:47Z",
        "rrDate": "2026-08-01",
    }
    mapped = _map_raw_row_to_dict(raw, "seller_1")
    assert mapped["rrd_id"] == 1234567
    assert mapped["clean_cis"] == "0104630199251844215f7iE!r2G:fR"
    assert mapped["doc_type_name"] == "Возврат"
    assert mapped["retail_amount"] == Decimal("790.0")


@pytest.mark.asyncio
async def test_wb_finance_client_page_and_stream():
    client = WBFinanceClient(api_token="test_finance_token")

    page_1 = [
        {"rrdId": 100, "docTypeName": "Продажа", "kiz": "kiz_1"},
        {"rrdId": 200, "docTypeName": "Возврат", "kiz": "kiz_2"},
    ]
    resp_200 = Response(status_code=200, json=page_1)
    resp_204 = Response(status_code=204)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        # 1. Single page success
        mock_post.return_value = resp_200
        res = await client.get_sales_reports_page("2026-08-01T00:00:00Z", "2026-08-31T23:59:59Z")
        assert len(res) == 2
        assert res[0]["rrdId"] == 100

        # 2. 204 No Content
        mock_post.return_value = resp_204
        res_empty = await client.get_sales_reports_page("2026-08-01T00:00:00Z", "2026-08-31T23:59:59Z")
        assert res_empty == []

        # 3. 401 Unauthorized
        mock_post.return_value = Response(status_code=401, text="Unauthorized")
        with pytest.raises(WBFinanceUnauthorizedError):
            await client.get_sales_reports_page("2026-08-01T00:00:00Z", "2026-08-31T23:59:59Z")


@pytest.mark.asyncio
async def test_sync_seller_financial_reports_and_ownership():
    seller_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as session:
        seller = Seller(
            id=seller_id,
            name="Test Finance Seller",
            wb_api_token_encrypted=encrypt("wb_fin_token_secret"),
            cz_inn="190207495060",
            cz_token_encrypted=encrypt("cz_token_secret"),
            is_active=True,
        )
        session.add(seller)
        await session.commit()

    sample_rows = [
        # Sale 1
        {
            "rrdId": 5001,
            "docTypeName": "Продажа",
            "sellerOperName": "Продажа",
            "retailAmount": "1200.00",
            "kiz": "0104630199251844215sale11111111",
            "srid": "srid_sale_1",
            "rrDate": "2026-08-01",
        },
        # Return 1 - belongs to seller
        {
            "rrdId": 5002,
            "docTypeName": "Возврат",
            "sellerOperName": "Возврат",
            "retailAmount": "1200.00",
            "returnAmount": 1,
            "kiz": "0104630199251844215seller_own11\x1d91EE10\x1d92tail",
            "srid": "srid_ret_seller",
            "rrDate": "2026-08-02",
        },
        # Return 2 - belongs to WB (9714053621)
        {
            "rrdId": 5003,
            "docTypeName": "Возврат",
            "sellerOperName": "Возврат",
            "retailAmount": "850.00",
            "returnAmount": 1,
            "kiz": "0104630199251844215wb_owned2222\x1d91EE10\x1d92tail",
            "srid": "srid_ret_wb",
            "rrDate": "2026-08-03",
        },
    ]

    async def mock_stream(*args, **kwargs):
        yield sample_rows

    mock_cz_info = {
        "0104630199251844215seller_own11": KizProductInfo(
            kiz_code="0104630199251844215seller_own11",
            clean_cis="0104630199251844215seller_own11",
            cz_status="RETIRED",
            cz_owner_inn="190207495060",
            cz_owner_name="ИП АНАНЬЕВ МАКСИМ АНДРЕЕВИЧ",
        ),
        "0104630199251844215wb_owned2222": KizProductInfo(
            kiz_code="0104630199251844215wb_owned2222",
            clean_cis="0104630199251844215wb_owned2222",
            cz_status="RETIRED",
            cz_owner_inn="9714053621",
            cz_owner_name="ООО РВБ",
        ),
    }

    with patch.object(WBFinanceClient, "fetch_all_sales_reports", side_effect=mock_stream):
        with patch("app.services.wb_finance_service.batch_verify_and_sync_cises", new_callable=AsyncMock) as mock_cz:
            mock_cz.return_value = mock_cz_info

            async with AsyncSessionLocal() as session:
                sel = await session.get(Seller, seller_id)
                res = await sync_seller_financial_reports(sel, session, days=30, verify_cz=True)

            assert res["success"] is True
            assert res["total_rows"] == 3
            assert res["sales_count"] == 1
            assert res["returns_count"] == 2
            assert res["returns_with_kiz"] == 2
            assert res["cz_audit"]["seller_owned"] == 1
            assert res["cz_audit"]["other_owned"] == 1

            # Verify in DB
            async with AsyncSessionLocal() as session:
                from sqlalchemy import select
                stmt = select(WbSalesReportRow).where(WbSalesReportRow.seller_id == seller_id).order_by(WbSalesReportRow.rrd_id)
                rows = (await session.execute(stmt)).scalars().all()
                assert len(rows) == 3

                row_seller = [r for r in rows if r.rrd_id == 5002][0]
                assert row_seller.is_seller_owner is True
                assert row_seller.cz_owner_inn == "190207495060"
                assert row_seller.cz_status == "RETIRED"

                row_wb = [r for r in rows if r.rrd_id == 5003][0]
                assert row_wb.is_seller_owner is False
                assert row_wb.cz_owner_inn == "9714053621"

                # Test idempotency on second sync
                res_repeat = await sync_seller_financial_reports(sel, session, days=30, verify_cz=False)
                assert res_repeat["success"] is True
                # No duplicates created
                rows_repeat = (await session.execute(stmt)).scalars().all()
                assert len(rows_repeat) == 3


@pytest.mark.asyncio
async def test_api_finance_endpoints():
    from httpx import AsyncClient, ASGITransport
    from app.config import settings

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Login to obtain real valid token
        login_res = await client.post(
            "/api/v1/auth/login",
            json={"username": settings.admin_username, "password": settings.admin_password}
        )
        assert login_res.status_code == 200, f"Login failed: {login_res.text}"
        token = login_res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        seller_id = str(uuid.uuid4())
        async with AsyncSessionLocal() as session:
            seller = Seller(
                id=seller_id,
                name="API Test Finance Shop",
                wb_api_token_encrypted=encrypt("token_123"),
                cz_inn="190207495060",
                is_active=True,
            )
            session.add(seller)

            # Add pre-populated row
            row = WbSalesReportRow(
                id=str(uuid.uuid4()),
                seller_id=seller_id,
                rrd_id=9999,
                doc_type_name="Возврат",
                seller_oper_name="Возврат",
                kiz="0104630199251844215test_kiz",
                clean_cis="0104630199251844215test_kiz",
                retail_amount=Decimal("1500.00"),
                is_seller_owner=True,
                cz_owner_inn="190207495060",
                cz_status="RETIRED",
            )
            session.add(row)
            await session.commit()

        # 1. Summary
        res = await client.get(f"/api/v1/sellers/{seller_id}/finance/summary", headers=headers)
        assert res.status_code == 200
        summary = res.json()
        assert summary["seller_id"] == seller_id
        assert summary["returns"]["count"] == 1
        assert summary["returns"]["seller_owned_kiz_count"] == 1

        # 2. List returns
        res_ret = await client.get(f"/api/v1/sellers/{seller_id}/finance/returns?seller_owned_only=true", headers=headers)
        assert res_ret.status_code == 200
        ret_data = res_ret.json()
        assert ret_data["total"] == 1
        assert ret_data["items"][0]["clean_cis"] == "0104630199251844215test_kiz"
        assert ret_data["items"][0]["is_seller_owner"] is True



@pytest.mark.asyncio
async def test_celery_finance_agent_dispatch():
    seller_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as session:
        seller = Seller(
            id=seller_id,
            name="Celery Finance Shop",
            wb_api_token_encrypted=encrypt("wb_agent_token"),
            is_active=True,
        )
        session.add(seller)
        await session.commit()

    with patch("app.agents.wb_finance_agent.sync_seller_financial_reports_task.delay") as mock_delay:
        res = sync_all_sellers_financial_reports()
        assert res["dispatched"] >= 1
        mock_delay.assert_called()


@pytest.mark.asyncio
async def test_create_finance_return_signature_batch():
    from app.models.order import Order, OrderStatus, KizStatus
    from app.models.kiz import KizSignatureBatch, BatchStatus
    from app.services.wb_finance_batch_service import create_finance_return_signature_batch

    seller_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as session:
        seller = Seller(
            id=seller_id,
            name="Batch Test Shop",
            wb_api_token_encrypted=encrypt("token"),
            cz_token_encrypted=encrypt("cz_token"),
            cz_inn="190207495060",
            is_active=True,
        )
        session.add(seller)

        # KIZ 1: Sale -> Return (Candidate)
        cis1 = "0104630199251844215kiz_returned_1"
        row1_sale = WbSalesReportRow(
            id=str(uuid.uuid4()),
            seller_id=seller_id,
            rrd_id=101,
            rr_date=date(2026, 7, 1),
            doc_type_name="Продажа",
            clean_cis=cis1,
            kiz=cis1,
        )
        row1_return = WbSalesReportRow(
            id=str(uuid.uuid4()),
            seller_id=seller_id,
            rrd_id=105,
            rr_date=date(2026, 7, 15),
            doc_type_name="Возврат",
            clean_cis=cis1,
            kiz=cis1,
            retail_amount=Decimal("2000.00"),
        )

        # KIZ 2: Sale -> Return -> Resold (Should be filtered out)
        cis2 = "0104630199251844215kiz_resold_2"
        row2_sale1 = WbSalesReportRow(
            id=str(uuid.uuid4()),
            seller_id=seller_id,
            rrd_id=201,
            rr_date=date(2026, 7, 2),
            doc_type_name="Продажа",
            clean_cis=cis2,
            kiz=cis2,
        )
        row2_return = WbSalesReportRow(
            id=str(uuid.uuid4()),
            seller_id=seller_id,
            rrd_id=205,
            rr_date=date(2026, 7, 10),
            doc_type_name="Возврат",
            clean_cis=cis2,
            kiz=cis2,
        )
        row2_sale2 = WbSalesReportRow(
            id=str(uuid.uuid4()),
            seller_id=seller_id,
            rrd_id=210,
            rr_date=date(2026, 7, 25),
            doc_type_name="Продажа",
            clean_cis=cis2,
            kiz=cis2,
        )

        # KIZ 3: Only Sale
        cis3 = "0104630199251844215kiz_only_sale_3"
        row3_sale = WbSalesReportRow(
            id=str(uuid.uuid4()),
            seller_id=seller_id,
            rrd_id=301,
            rr_date=date(2026, 7, 5),
            doc_type_name="Продажа",
            clean_cis=cis3,
            kiz=cis3,
        )

        import random
        random_order_id = random.randint(5000000000, 9999999999)
        # Matching FBS Order for cis1
        order = Order(
            id=random_order_id,
            seller_id=seller_id,
            status=OrderStatus.DELIVERED,
            wb_status="complete",
            kiz_code=cis1,
            kiz_status=KizStatus.WITHDRAWN,
            sticker_id="STK-999888",
            wb_created_at=datetime.now(timezone.utc),
        )

        session.add_all([row1_sale, row1_return, row2_sale1, row2_return, row2_sale2, row3_sale, order])
        await session.commit()

        # Mock True API get_cises_info
        mock_cz_info = [
            {
                "cisInfo": {
                    "requestedCis": cis1,
                    "cis": cis1,
                    "status": "RETIRED",
                    "withdrawReason": "DISTANCE",
                    "ownerInn": "9714053621",
                    "ownerName": 'ООО "РВБ"',
                    "producerInn": "190207495060",
                }
            }
        ]

        with patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_get_info:
            mock_get_info.return_value = mock_cz_info

            res = await create_finance_return_signature_batch(seller=seller, db=session, days=90)
            assert res["success"] is True
            assert res["batch_id"] is not None

            summary = res["summary"]
            assert summary["resold_after_return_count"] == 1
            assert summary["return_candidates_count"] == 1
            assert summary["wb_owned_count"] == 1
            assert summary["linked_to_fbs_orders"] == 1

            # Check KizSignatureBatch
            batch = await session.get(KizSignatureBatch, res["batch_id"])
            assert batch is not None
            assert batch.status == BatchStatus.PENDING_SIGNATURE
            assert batch.source == "wb_finance_returns"
            assert batch.returns_count == 1

            ret_item = batch.data_payload["returns"][0]
            assert ret_item["clean_cis"] == cis1
            assert ret_item["order_id"] == random_order_id
            assert ret_item["sticker_id"] == "STK-999888"
            assert ret_item["is_wb_owned"] is True
            assert ret_item["needs_cz_return"] is True
            assert ret_item["selected"] is True

