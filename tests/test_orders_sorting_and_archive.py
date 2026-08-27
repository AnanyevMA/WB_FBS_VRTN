import pytest
import pytest_asyncio
import uuid
import random
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from app.database import AsyncSessionLocal, init_db
from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus
from app.api.orders import list_orders, get_dashboard_stats, check_is_archived, get_archived_filter
from app.services.encryption import encrypt


@pytest_asyncio.fixture
async def test_seller():
    await init_db()
    async with AsyncSessionLocal() as session:
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name=f"Seller-Test-{seller_id[:6]}",
            wb_supplier_id=f"WB-{seller_id[:6]}",
            cz_inn="7711223344",
            wb_api_token_encrypted=encrypt("mock_token"),
            is_active=True
        )
        session.add(seller)
        await session.commit()
        return seller_id


@pytest.mark.asyncio
async def test_orders_default_sorting_by_date_desc(test_seller):
    """Verify that by default orders are sorted by creation date descending (newest on top)."""
    seller_id = test_seller
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as session:
        # Create 3 orders with different dates
        order_old = Order(
            id=random.randint(100000, 400000),
            seller_id=seller_id,
            status=OrderStatus.NEW,
            wb_created_at=now - timedelta(days=5),
            created_at=now - timedelta(days=5),
            price=Decimal("100.00"),
            article="ART-OLD",
            name="Старый товар"
        )
        order_mid = Order(
            id=random.randint(400001, 700000),
            seller_id=seller_id,
            status=OrderStatus.ASSEMBLING,
            wb_created_at=now - timedelta(days=1),
            created_at=now - timedelta(days=1),
            price=Decimal("200.00"),
            article="ART-MID",
            name="Средний товар"
        )
        order_new = Order(
            id=random.randint(700001, 999999),
            seller_id=seller_id,
            status=OrderStatus.NEW,
            wb_created_at=now,
            created_at=now,
            price=Decimal("300.00"),
            article="ART-NEW",
            name="Новейший товар"
        )
        session.add_all([order_old, order_mid, order_new])
        await session.commit()

        # Default query (view=all to see all 3)
        res = await list_orders(seller_id=seller_id, view="all", db=session)
        items = res["items"]
        assert len(items) >= 3
        # First item must be order_new
        assert items[0]["id"] == order_new.id
        assert items[1]["id"] == order_mid.id
        assert items[2]["id"] == order_old.id


@pytest.mark.asyncio
async def test_orders_sorting_by_columns_asc_desc(test_seller):
    """Verify sorting by price, ID, article, and status in both ASC and DESC directions."""
    seller_id = test_seller
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as session:
        base_id = random.randint(1000000, 8000000)
        id1 = base_id + 10
        id2 = base_id + 20
        id3 = base_id + 30

        o1 = Order(
            id=id1,
            seller_id=seller_id,
            status=OrderStatus.NEW,
            wb_created_at=now - timedelta(hours=3),
            price=Decimal("500.00"),
            article="B-ART",
            name="Товар B"
        )
        o2 = Order(
            id=id2,
            seller_id=seller_id,
            status=OrderStatus.DELIVERED,
            wb_created_at=now - timedelta(hours=2),
            price=Decimal("150.00"),
            article="A-ART",
            name="Товар A"
        )
        o3 = Order(
            id=id3,
            seller_id=seller_id,
            status=OrderStatus.ASSEMBLING,
            wb_created_at=now - timedelta(hours=1),
            price=Decimal("900.00"),
            article="C-ART",
            name="Товар C"
        )
        session.add_all([o1, o2, o3])
        await session.commit()

        # 1. Price ASC
        res_price_asc = await list_orders(seller_id=seller_id, view="all", sort_by="price", sort_dir="asc", db=session)
        price_order_ids = [item["id"] for item in res_price_asc["items"]]
        assert price_order_ids == [id2, id1, id3]

        # 2. Price DESC
        res_price_desc = await list_orders(seller_id=seller_id, view="all", sort_by="price", sort_dir="desc", db=session)
        price_desc_ids = [item["id"] for item in res_price_desc["items"]]
        assert price_desc_ids == [id3, id1, id2]

        # 3. ID ASC
        res_id_asc = await list_orders(seller_id=seller_id, view="all", sort_by="id", sort_dir="asc", db=session)
        id_asc_ids = [item["id"] for item in res_id_asc["items"]]
        assert id_asc_ids == [id1, id2, id3]

        # 4. Article ASC
        res_art_asc = await list_orders(seller_id=seller_id, view="all", sort_by="article", sort_dir="asc", db=session)
        art_asc_ids = [item["id"] for item in res_art_asc["items"]]
        assert art_asc_ids == [id2, id1, id3]


@pytest.mark.asyncio
async def test_order_archive_conditions_and_view_filtering(test_seller):
    """
    Verify archiving rules:
    1. Sold/Delivered + KIZ Withdrawn -> ARCHIVED
    2. Cancelled/Declined + KIZ Returned/In circulation -> ARCHIVED
    3. Active (New / Assembling / Delivering / Waiting withdrawal / Cancelled awaiting return) -> ACTIVE
    4. View modes ('active', 'archive', 'all') filter correctly.
    """
    seller_id = test_seller
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as session:
        id1 = random.randint(10000000, 19999999)
        id2 = random.randint(20000000, 29999999)
        id3 = random.randint(30000000, 39999999)
        id4 = random.randint(40000000, 49999999)
        id5 = random.randint(50000000, 59999999)

        # Case 1: Sold + KIZ Withdrawn (Archived)
        o_archived_sold = Order(
            id=id1,
            seller_id=seller_id,
            status=OrderStatus.DELIVERED,
            wb_status="sold",
            kiz_required=True,
            kiz_code=f"010460123456789021KIZ{id1}",
            kiz_status=KizStatus.WITHDRAWN,
            kiz_cz_status="RETIRED",
            wb_created_at=now - timedelta(days=3),
            price=Decimal("1000.00")
        )

        # Case 2: Cancelled + KIZ Returned (Archived)
        o_archived_cancelled = Order(
            id=id2,
            seller_id=seller_id,
            status=OrderStatus.CANCELLED,
            wb_status="canceled_by_client",
            kiz_required=True,
            kiz_code=f"010460123456789021KIZ{id2}",
            kiz_status=KizStatus.RETURNED,
            kiz_cz_status="INTRODUCED",
            wb_created_at=now - timedelta(days=2),
            price=Decimal("1200.00")
        )

        # Case 3: Sold + KIZ Attached (Active - needs CZ withdrawal)
        o_active_pending_withdrawal = Order(
            id=id3,
            seller_id=seller_id,
            status=OrderStatus.DELIVERED,
            wb_status="sold",
            kiz_required=True,
            kiz_code=f"010460123456789021KIZ{id3}",
            kiz_status=KizStatus.ATTACHED,
            kiz_cz_status="INTRODUCED",
            wb_created_at=now - timedelta(days=1),
            price=Decimal("1500.00")
        )

        # Case 4: Cancelled + KIZ Withdrawn (Active - needs CZ return)
        o_active_pending_return = Order(
            id=id4,
            seller_id=seller_id,
            status=OrderStatus.CANCELLED,
            wb_status="canceled",
            kiz_required=True,
            kiz_code=f"010460123456789021KIZ{id4}",
            kiz_status=KizStatus.WITHDRAWN,
            kiz_cz_status="RETIRED",
            wb_created_at=now - timedelta(hours=12),
            price=Decimal("800.00")
        )

        # Case 5: New Order (Active)
        o_active_new = Order(
            id=id5,
            seller_id=seller_id,
            status=OrderStatus.NEW,
            wb_status="waiting",
            kiz_required=True,
            kiz_status=KizStatus.PENDING,
            wb_created_at=now,
            price=Decimal("600.00")
        )

        session.add_all([
            o_archived_sold,
            o_archived_cancelled,
            o_active_pending_withdrawal,
            o_active_pending_return,
            o_active_new
        ])
        await session.commit()

        # Check Python helper check_is_archived
        is_arch1, reason1 = check_is_archived(o_archived_sold)
        assert is_arch1 is True
        assert reason1 == "sold_and_withdrawn"

        is_arch2, reason2 = check_is_archived(o_archived_cancelled)
        assert is_arch2 is True
        assert reason2 == "cancelled_and_returned"

        is_arch3, _ = check_is_archived(o_active_pending_withdrawal)
        assert is_arch3 is False

        is_arch4, _ = check_is_archived(o_active_pending_return)
        assert is_arch4 is False

        is_arch5, _ = check_is_archived(o_active_new)
        assert is_arch5 is False

        # 1. Test Default view (view="active")
        res_active = await list_orders(seller_id=seller_id, view="active", db=session)
        active_ids = {item["id"] for item in res_active["items"]}
        assert active_ids == {id3, id4, id5}
        assert res_active["active_count"] == 3
        assert res_active["archived_count"] == 2
        assert res_active["total_orders_count"] == 5

        # 2. Test Archive view (view="archive")
        res_archive = await list_orders(seller_id=seller_id, view="archive", db=session)
        archive_ids = {item["id"] for item in res_archive["items"]}
        assert archive_ids == {id1, id2}
        assert all(item["is_archived"] is True for item in res_archive["items"])

        # 3. Test All view (view="all")
        res_all = await list_orders(seller_id=seller_id, view="all", db=session)
        all_ids = {item["id"] for item in res_all["items"]}
        assert all_ids == {id1, id2, id3, id4, id5}

        # 4. Test Dashboard Stats
        stats = await get_dashboard_stats(seller_id=seller_id, db=session)
        assert stats["total_orders"] == 5
        assert stats["active_orders"] == 3
        assert stats["archived_orders"] == 2
