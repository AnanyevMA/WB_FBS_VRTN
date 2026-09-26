"""
Unit and Integration Tests for WB Finance Sales Reports & Return KIZ Audit.
Verifies WBFinanceClient, wb_finance_service, Celery tasks, and API endpoints.
"""
import uuid
from datetime import datetime, timezone
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
