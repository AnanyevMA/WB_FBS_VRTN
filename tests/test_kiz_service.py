import pytest
import uuid
import random
from datetime import datetime, timezone

from app.database import AsyncSessionLocal, init_db
from app.services.kiz_service import parse_kiz_code, resolve_kiz_product_info
from app.models.kiz import KizProductInfo
from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus


def test_parse_kiz_code_standard():
    raw = "0104630199251318215QTSRh>4sVc+."
    res = parse_kiz_code(raw)
    assert res["gtin"] == "04630199251318"
    assert res["serial_number"] == "5QTSRh>4sVc+."
    assert res["clean_cis"] == "0104630199251318215QTSRh>4sVc+."


def test_parse_kiz_code_with_parentheses():
    raw = "(01)04630199251332(21)5/izn(LpLf\'YP"
    res = parse_kiz_code(raw)
    assert res["gtin"] == "04630199251332"
    assert res["serial_number"] == "5/izn(LpLf\'YP"
    assert res["clean_cis"] == "0104630199251332215/izn(LpLf\'YP"


def test_parse_kiz_code_with_crypto_tail():
    raw = "0104630199251318215QTSRh\x1d91ABCD92xyz123456789"
    res = parse_kiz_code(raw)
    assert res["gtin"] == "04630199251318"
    assert res["serial_number"] == "5QTSRh"
    assert res["crypto_key"] == "ABCD"
    assert res["crypto_tail"] == "xyz123456789"
    assert res["clean_cis"] == "0104630199251318215QTSRh"


@pytest.mark.asyncio
async def test_kiz_product_info_model_and_validation():
    await init_db()
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Test Seller KIZ",
            wb_supplier_id=f"WB-{seller_id[:6]}",
            cz_inn="7700123456",
            wb_api_token_encrypted="enc_test",
            cz_token_encrypted="enc_test",
            is_active=True
        )
        session.add(seller)

        order_id = random.randint(800000000, 899999999)
        order = Order(
            id=order_id,
            seller_id=seller.id,
            status=OrderStatus.ASSEMBLING,
            wb_created_at=datetime.now(timezone.utc),
            article="hood.brown.100",
            tech_size="ONE SIZE",
            wb_size="55-58",
            name="Капор утепленный",
            kiz_required=True,
            kiz_status=KizStatus.PENDING
        )
        session.add(order)
        await session.commit()

        kiz_code = f"0104630199251332215{uuid.uuid4().hex[:10]}"
        kiz_info = await resolve_kiz_product_info(
            kiz_code=kiz_code,
            seller=seller,
            order=order,
            db=session,
            force_refresh=True
        )

        assert kiz_info.gtin == "04630199251332"
        assert kiz_info.article == "hood.brown.100"
        assert kiz_info.tech_size == "ONE SIZE"
        assert kiz_info.cz_status == "INTRODUCED"
        assert kiz_info.cz_owner_inn == "7700123456"
        assert kiz_info.is_valid is True

        # Test size mismatch check
        mismatch_order_id = random.randint(900000000, 999999999)
        mismatch_order = Order(
            id=mismatch_order_id,
            seller_id=seller.id,
            status=OrderStatus.ASSEMBLING,
            wb_created_at=datetime.now(timezone.utc),
            article="hood.brown.100",
            tech_size="XXL",
            wb_size="60",
            name="Капор утепленный",
            kiz_required=True
        )
        session.add(mismatch_order)
        await session.commit()

        kiz_code_2 = f"0104630199251332215{uuid.uuid4().hex[:10]}"
        kiz_info_2 = KizProductInfo(
            id=str(uuid.uuid4()),
            kiz_code=kiz_code_2,
            gtin="04630199251332",
            serial_number="5L8SPou",
            tech_size="ONE SIZE",
            article="hood.brown.100",
            cz_owner_inn="7700123456",
            cz_status="INTRODUCED",
            seller_id=str(seller.id)
        )
        session.add(kiz_info_2)
        await session.commit()

        resolved_mismatch = await resolve_kiz_product_info(
            kiz_code=kiz_code_2,
            seller=seller,
            order=mismatch_order,
            db=session,
            force_refresh=True
        )
        assert resolved_mismatch.is_valid is False
        assert "Размер КИЗ" in resolved_mismatch.validation_message


@pytest.mark.asyncio
async def test_kiz_ogv_blocking_validation():
    """Verify that True API v719.0 OGV blocker codes (RD, FSSP, MZ, VETRF) invalidate KIZ."""
    await init_db()
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Test Seller OGV",
            wb_supplier_id=f"WB-{seller_id[:6]}",
            cz_inn="7700998877",
            wb_api_token_encrypted="enc_test",
            cz_token_encrypted="enc_test",
            is_active=True
        )
        session.add(seller)

        order = Order(
            id=random.randint(700000000, 799999999),
            seller_id=seller.id,
            status=OrderStatus.ASSEMBLING,
            wb_created_at=datetime.now(timezone.utc),
            article="dress.silk.red",
            tech_size="M",
            name="Платье шелковое",
            kiz_required=True,
            kiz_status=KizStatus.PENDING
        )
        session.add(order)
        await session.commit()

        # Preset KIZ with OGV blockers: RD (Росаккредитация) and FSSP (ФССП)
        kiz_code = f"0104630199259999215{uuid.uuid4().hex[:10]}"
        kiz_info = KizProductInfo(
            id=str(uuid.uuid4()),
            kiz_code=kiz_code,
            gtin="04630199259999",
            serial_number="5OGVTest",
            tech_size="M",
            article="dress.silk.red",
            cz_owner_inn="7700998877",
            cz_status="INTRODUCED",
            raw_cz_payload={
                "cis": kiz_code,
                "status": "INTRODUCED",
                "ogvs": ["RD", "FSSP"],
            },
            seller_id=str(seller.id)
        )
        session.add(kiz_info)
        await session.commit()

        resolved = await resolve_kiz_product_info(
            kiz_code=kiz_code,
            seller=seller,
            order=order,
            db=session,
            force_refresh=True
        )

        assert resolved.is_valid is False
        assert "заблокирован госорганами" in resolved.validation_message
        assert "Росаккредитация" in resolved.validation_message
        assert "ФССП" in resolved.validation_message


@pytest.mark.asyncio
async def test_attach_kiz_endpoint_syncs_kiz_cz_status():
    """Verify that attach_kiz API endpoint populates order.kiz_cz_status and validates state."""
    from app.api.kiz import attach_kiz
    from app.schemas.order import KIZAttachRequest
    from app.services.encryption import encrypt

    await init_db()
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Test Seller Attach Sync",
            wb_supplier_id=f"WB-{seller_id[:6]}",
            cz_inn="7700123456",
            wb_api_token_encrypted=encrypt("wb_test"),
            cz_token_encrypted=encrypt("cz_test"),
            is_active=True
        )
        session.add(seller)

        order_id = random.randint(600000000, 699999999)
        order = Order(
            id=order_id,
            seller_id=seller.id,
            status=OrderStatus.NEW,
            wb_created_at=datetime.now(timezone.utc),
            article="hood.brown.100",
            tech_size="ONE SIZE",
            name="Капор утепленный",
            kiz_required=True,
            kiz_status=KizStatus.PENDING,
        )
        session.add(order)
        await session.commit()

        kiz_code = f"0104630199251332215{uuid.uuid4().hex[:10]}"
        req = KIZAttachRequest(kiz_code=kiz_code)

        res = await attach_kiz(seller_id=seller_id, order_id=order_id, req=req, db=session)
        assert res["success"] is True
        assert res["kiz_status"] == "ATTACHED"
        assert res["product_info"]["cz_status"] == "INTRODUCED"

        # Check DB state
        updated_order = await session.get(Order, order_id)
        assert updated_order.kiz_status == KizStatus.ATTACHED
        assert updated_order.kiz_cz_status == "INTRODUCED"
        assert updated_order.kiz_cz_status_updated_at is not None


@pytest.mark.asyncio
async def test_check_order_kiz_status_detects_conflict():
    """Verify that check_order_kiz_status sets KizStatus.ERROR when mark is RETIRED on active order."""
    from app.api.orders import check_order_kiz_status
    from app.services.encryption import encrypt
    from unittest.mock import patch, AsyncMock

    await init_db()
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Test Seller Conflict",
            wb_supplier_id=f"WB-{seller_id[:6]}",
            cz_inn="7700123456",
            wb_api_token_encrypted=encrypt("wb_test"),
            cz_token_encrypted=encrypt("cz_test"),
            is_active=True
        )
        session.add(seller)

        order_id = random.randint(500000000, 599999999)
        kiz_code = f"0104630199251332215{uuid.uuid4().hex[:10]}"
        order = Order(
            id=order_id,
            seller_id=seller.id,
            status=OrderStatus.ASSEMBLING,
            wb_created_at=datetime.now(timezone.utc),
            article="hood.brown.100",
            tech_size="ONE SIZE",
            name="Капор утепленный",
            kiz_required=True,
            kiz_code=kiz_code,
            kiz_status=KizStatus.ATTACHED,
        )
        session.add(order)
        await session.commit()

        # Mock True API returning RETIRED status for this code
        mock_cises_info = [{"cisInfo": {"status": "RETIRED", "ownerInn": "7700123456"}}]
        with patch("app.services.cz_client.CZClient.get_cises_info", new_callable=AsyncMock) as mock_get_info:
            mock_get_info.return_value = mock_cises_info
            res = await check_order_kiz_status(seller_id=seller_id, order_id=order_id, db=session)

            assert res["kiz_cz_status"] == "RETIRED"
            assert res["kiz_status"] == "ERROR"

            updated_order = await session.get(Order, order_id)
            assert updated_order.kiz_status == KizStatus.ERROR
            assert updated_order.kiz_cz_status == "RETIRED"


@pytest.mark.asyncio
async def test_check_order_kiz_status_handles_cz_exceptions_without_500():
    """Verify that check_order_kiz_status handles CZ API errors gracefully without crashing."""
    from app.api.orders import check_order_kiz_status
    from app.services.encryption import encrypt
    from app.services.cz_client import CZAPIError
    from unittest.mock import patch, AsyncMock

    await init_db()
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Test Seller Safe KIZ Check",
            cz_inn="7700123456",
            wb_api_token_encrypted=encrypt("wb_test"),
            cz_token_encrypted=encrypt("cz_test"),
            is_active=True
        )
        session.add(seller)

        order_id = random.randint(700000000, 799999999)
        kiz_code = f"0104630199251332215{uuid.uuid4().hex[:10]}91ffd92qwe"
        order = Order(
            id=order_id,
            seller_id=seller.id,
            status=OrderStatus.ASSEMBLING,
            wb_created_at=datetime.now(timezone.utc),
            article="hood.brown.100",
            tech_size="ONE SIZE",
            name="Капор утепленный",
            kiz_required=True,
            kiz_code=kiz_code,
            kiz_status=KizStatus.ATTACHED,
        )
        session.add(order)
        await session.commit()

        # Simulate CZ API error / signature failure
        with patch("app.services.cz_client.CZClient.get_cises_info", side_effect=CZAPIError("CZ Service Unavailable", 503)):
            res = await check_order_kiz_status(seller_id=seller_id, order_id=order_id, db=session)
            assert res is not None
            assert res["order_id"] == order_id
            assert "kiz_status" in res
            assert "kiz_cz_status" in res
            assert "clean_cis" in res

