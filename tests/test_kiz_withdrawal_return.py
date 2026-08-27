"""
Test suite for KIZ Withdrawal (LP_SHIP_GOODS) and Return (LP_RETURN_GOODS)
Target test code: 0104630199254371215LgcIxnWSgssC
"""
import json
import uuid
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.services.cz_client import CZClient
from app.models.order import KizStatus, OrderStatus
from app.models.seller import Seller
from app.models.order import Order


TEST_KIZ = "0104630199254371215LgcIxnWSgssC"
TEST_INN = "7701234567"
TEST_ORDER_ID = 5389201923
TEST_FIAS_ID = "6f827e86-1d16-43b9-a297-b6730594a112"
TEST_PRICE_KOP = 247500  # 2475.00 руб


@pytest.mark.asyncio
async def test_kiz_structure_validation():
    """Verify SGTIN structure: AI(01) 14-digit GTIN + AI(21) Serial number."""
    assert len(TEST_KIZ) >= 27
    assert TEST_KIZ.startswith("01")
    gtin = TEST_KIZ[2:16]
    assert len(gtin) == 14
    assert gtin.isdigit()
    assert gtin == "04630199254371"
    
    assert TEST_KIZ[16:18] == "21"
    serial = TEST_KIZ[18:]
    assert serial == "5LgcIxnWSgssC"


@pytest.mark.asyncio
async def test_withdrawal_document_building_with_fias():
    """Verify LK_RECEIPT document structure containing FIAS ID and per-product primary doc."""
    client = CZClient(inn=TEST_INN, token="mock-cz-token", cert_thumbprint="mock_thumbprint")

    doc = client._build_withdrawal_document(
        kiz_codes=[TEST_KIZ],
        price_kopecks=TEST_PRICE_KOP,
        mod_fias=TEST_FIAS_ID,
        mod_kpp="770101001",
        primary_document_number=str(TEST_ORDER_ID),
        document_type="OTHER",
    )

    assert doc["documentFormat"] == "MANUAL"
    assert doc["type"] == "LK_RECEIPT"

    payload = json.loads(doc["productDocument"])
    assert payload["inn"] == TEST_INN
    assert payload["action"] == "DISTANCE"
    assert payload["fias_id"] == TEST_FIAS_ID
    assert payload["kpp"] == "770101001"

    assert len(payload["products"]) == 1
    assert payload["products"][0]["cis"] == TEST_KIZ
    assert payload["products"][0]["product_cost"] == TEST_PRICE_KOP
    assert payload["products"][0]["primary_document_type"] == "OTHER"
    assert payload["products"][0]["primary_document_number"] == str(TEST_ORDER_ID)
    assert payload["products"][0]["primary_document_custom_name"] == "Продажа через Wildberries FBS"


@pytest.mark.asyncio
async def test_golden_schema_withdrawal_and_return_with_receipts():
    """Verify exact match with real Честный Знак exported JSON."""
    client = CZClient(inn="190207495060", token="mock-token")

    # 1. Golden Withdrawal
    w_doc = client._build_withdrawal_document(
        kiz_codes=["0104630199251318215QTSRh>4sVc+."],
        price_kopecks=308200,
        mod_fias="1f06b72d-5b8d-4f0c-a3ee-e0479498b901",
        document_date="2026-08-26",
        primary_document_number="131749",
        document_type="RECEIPT",
    )
    w_payload = json.loads(w_doc["productDocument"])
    assert w_payload["inn"] == "190207495060"
    assert w_payload["action"] == "DISTANCE"
    assert w_payload["action_date"] == "2026-08-26"
    assert w_payload["fias_id"] == "1f06b72d-5b8d-4f0c-a3ee-e0479498b901"
    assert "document_type" not in w_payload
    assert w_payload["products"] == [
        {
            "cis": "0104630199251318215QTSRh>4sVc+.",
            "product_cost": 308200,
            "primary_document_type": "RECEIPT",
            "primary_document_number": "131749",
            "primary_document_date": "2026-08-26",
        }
    ]

    # 2. Golden Return
    r_doc = client._build_return_document(
        kiz_codes=["0104630199251332215ZEdKVTnFahrt"],
        document_date="27.08.2026",
        primary_document_number="217837",
        primary_document_type="RECEIPT",
        certificate_type="CONFORMITY_DECLARATION",
        certificate_number="ЕАЭС N RU Д-RU.РА05.В.88154/22",
        certificate_date="29.08.2022",
    )
    r_payload = json.loads(r_doc["productDocument"])
    assert r_payload["trade_participant_inn"] == "190207495060"
    assert r_payload["return_type"] == "REMOTE_SALE_RETURN"
    assert r_payload["paid"] is True
    assert "primary_document_type" not in r_payload
    assert r_payload["products_list"] == [
        {
            "ki": "0104630199251332215ZEdKVTnFahrt",
            "primary_document_type": "RECEIPT",
            "primary_document_number": "217837",
            "primary_document_date": "27.08.2026",
            "certificate_type": "CONFORMITY_DECLARATION",
            "certificate_number": "ЕАЭС N RU Д-RU.РА05.В.88154/22",
            "certificate_date": "29.08.2022",
        }
    ]


@pytest.mark.asyncio
async def test_return_document_building():
    """Verify LP_RETURN document structure for customer return."""
    client = CZClient(inn=TEST_INN, token="mock-cz-token", cert_thumbprint="mock_thumbprint")

    doc = client._build_return_document(kiz_codes=[TEST_KIZ])

    assert doc["documentFormat"] == "MANUAL"
    assert doc["type"] == "LP_RETURN"

    payload = json.loads(doc["productDocument"])
    assert payload["trade_participant_inn"] == TEST_INN
    assert payload["return_type"] == "REMOTE_SALE_RETURN"
    assert payload["paid"] is True
    assert len(payload["products_list"]) == 1
    assert payload["products_list"][0]["ki"] == TEST_KIZ


@pytest.mark.asyncio
async def test_cz_client_withdraw_and_return_execution():
    """Test full async execution of withdrawal and return through True API mock."""
    client = CZClient(inn=TEST_INN, token="mock-token", cert_thumbprint="mock_thumbprint")

    mock_doc_id_withdraw = "cz-doc-withdraw-9999"
    mock_doc_id_return = "cz-doc-return-8888"

    with patch("app.services.cz_client.sign_document", new_callable=AsyncMock) as mock_sign, \
         patch.object(client, "_request", new_callable=AsyncMock) as mock_req, \
         patch.object(client, "get_document_status", new_callable=AsyncMock) as mock_status:

        mock_sign.return_value = "mock_cms_signature_base64"
        mock_req.side_effect = [
            {"documentId": mock_doc_id_withdraw},  # withdraw create
            {"documentId": mock_doc_id_return},    # return create
        ]
        mock_status.return_value = {"status": "CHECKED_OK"}

        # 1. Execute Withdrawal
        res_withdraw_id = await client.withdraw_from_circulation(
            kiz_codes=[TEST_KIZ],
            price_kopecks=TEST_PRICE_KOP,
            mod_fias=TEST_FIAS_ID,
            wb_order_id=TEST_ORDER_ID,
            wait_for_result=True,
        )
        assert res_withdraw_id == mock_doc_id_withdraw

        # 2. Execute Return
        res_return_id = await client.return_to_circulation(
            kiz_codes=[TEST_KIZ],
            wb_order_id=TEST_ORDER_ID,
            wait_for_result=True,
        )
        assert res_return_id == mock_doc_id_return


def test_celery_withdrawal_and_return_agent_tasks():
    """Verify Celery task execution with DB state transitions and audit logging."""
    import random
    from sqlalchemy.orm import Session
    from app.agents.cz_withdrawal import withdraw_order_kiz, sync_engine
    from app.agents.cz_return import return_order_kiz
    from app.services.encryption import encrypt
    from datetime import datetime, timezone

    seller_id = str(uuid.uuid4())
    order_id = random.randint(1000000000, 9999999999)

    with Session(sync_engine) as db:
        # Create test seller
        seller = Seller(
            id=seller_id,
            name="Test Seller KIZ",
            wb_api_token_encrypted=encrypt("wb-mock-token"),
            cz_inn=TEST_INN,
            cz_token_encrypted=encrypt("cz-mock-token"),
            mod_fias=TEST_FIAS_ID,
            mod_kpp="770101001",
        )
        db.add(seller)

        # Create test order with KIZ attached
        order = Order(
            id=order_id,
            seller_id=seller_id,
            status=OrderStatus.DELIVERED,
            wb_created_at=datetime.now(timezone.utc),
            price=2475.00,
            kiz_required=True,
            kiz_code=TEST_KIZ,
            kiz_status=KizStatus.ATTACHED,
        )
        db.add(order)
        db.commit()

    with patch("app.agents.cz_withdrawal._do_withdrawal", new_callable=AsyncMock) as mock_with_do, \
         patch("app.agents.cz_return._do_return", new_callable=AsyncMock) as mock_ret_do, \
         patch("app.agents.notifier.send_cz_status_notification.delay") as mock_notify:

        mock_with_do.return_value = "doc-cz-withdraw-e2e-123"
        mock_ret_do.return_value = "doc-cz-return-e2e-456"

        # Step 1: Run Celery task for withdrawal
        withdraw_order_kiz(
            seller_id=seller_id,
            order_id=order_id,
            kiz_code=TEST_KIZ,
            price_kopecks=TEST_PRICE_KOP,
        )

        with Session(sync_engine) as db:
            updated_order = db.query(Order).filter(Order.id == order_id).first()
            assert updated_order.kiz_status == KizStatus.WITHDRAWN
            assert updated_order.kiz_cz_status == "RETIRED"
            assert updated_order.cz_withdrawal_doc_id == "doc-cz-withdraw-e2e-123"

        # Step 2: Run Celery task for return
        return_order_kiz(
            seller_id=seller_id,
            order_id=order_id,
            kiz_code=TEST_KIZ,
        )

        with Session(sync_engine) as db:
            returned_order = db.query(Order).filter(Order.id == order_id).first()
            assert returned_order.kiz_status == KizStatus.RETURNED
            assert returned_order.kiz_cz_status == "INTRODUCED"
            assert returned_order.cz_return_doc_id == "doc-cz-return-e2e-456"


@pytest.mark.asyncio
async def test_full_lifecycle_return_then_withdrawal_distance_sale():
    """
    Эмуляция полного бизнес-процесса для КИЗ 0104630199254371215LgcIxnWSgssC:
    1. Товар возвращен покупателем на ПВЗ -> формируется документ возврата LP_RETURN_GOODS (REMOTE_SALE_RETURN).
    2. КИЗ возвращается в оборот (INTRODUCED / RETURNED).
    3. КИЗ привязывается к новому заказу WB FBS.
    4. Товар доставляется покупателю -> формируется документ вывода из оборота LP_SHIP_GOODS по причине Дистанционная продажа (DISTANCE).
    5. КИЗ выбывает из оборота (WITHDRAWN / RETIRED).
    """
    import random
    from sqlalchemy.orm import Session
    from app.agents.cz_withdrawal import withdraw_order_kiz, sync_engine
    from app.agents.cz_return import return_order_kiz
    from app.services.encryption import encrypt
    from datetime import datetime, timezone
    from app.models.kiz import KizOperation, KizOperationType
    from app.models.audit import AuditLog

    seller_id = str(uuid.uuid4())
    return_order_id = random.randint(100000000, 499999999)
    new_sale_order_id = random.randint(500000000, 999999999)

    with Session(sync_engine) as db:
        seller = Seller(
            id=seller_id,
            name="ИП Иванов (Маркетплейс FBS)",
            wb_api_token_encrypted=encrypt("wb-real-token-mock"),
            cz_inn=TEST_INN,
            cz_token_encrypted=encrypt("cz-session-jwt-mock"),
            mod_fias=TEST_FIAS_ID,
            mod_kpp="770101001",
        )
        db.add(seller)

        # 1. Исходный возвращенный заказ
        ret_order = Order(
            id=return_order_id,
            seller_id=seller_id,
            status=OrderStatus.CANCELLED,
            wb_created_at=datetime.now(timezone.utc),
            price=2475.00,
            kiz_required=True,
            kiz_code=TEST_KIZ,
            kiz_status=KizStatus.ATTACHED,
        )
        db.add(ret_order)

        # 2. Новый заказ WB, ожидающий этот же КИЗ
        new_order = Order(
            id=new_sale_order_id,
            seller_id=seller_id,
            status=OrderStatus.ASSEMBLING,
            wb_created_at=datetime.now(timezone.utc),
            price=2475.00,
            article="hood.brown.100",
            tech_size="ONE SIZE",
            kiz_required=True,
            kiz_status=KizStatus.PENDING,
        )
        db.add(new_order)
        db.commit()

    captured_docs = []

    async def fake_create_document(doc, sign=True, pg="lp"):
        captured_docs.append(doc)
        doc_type = doc.get("type")
        return f"GISMT-DOC-{doc_type}-{uuid.uuid4().hex[:6].upper()}"

    # ================= ЭТАП 1: ВОЗВРАТ В ОБОРОТ =================
    with patch("app.services.cz_client.CZClient._create_document", side_effect=fake_create_document), \
         patch("app.services.cz_client.CZClient.get_document_status", return_value={"status": "CHECKED_OK"}), \
         patch("app.agents.notifier.send_cz_status_notification.delay"):

        return_order_kiz(
            seller_id=seller_id,
            order_id=return_order_id,
            kiz_code=TEST_KIZ,
        )

    # Проверка формирования документа LP_RETURN
    assert len(captured_docs) == 1
    return_doc = captured_docs[0]
    assert return_doc["type"] == "LP_RETURN"
    ret_payload = json.loads(return_doc["productDocument"])
    assert ret_payload["trade_participant_inn"] == TEST_INN
    assert ret_payload["return_type"] == "REMOTE_SALE_RETURN"
    assert ret_payload["products_list"][0]["ki"] == TEST_KIZ

    with Session(sync_engine) as db:
        db_ret_order = db.query(Order).filter(Order.id == return_order_id).first()
        assert db_ret_order.kiz_status == KizStatus.RETURNED
        assert db_ret_order.kiz_cz_status == "INTRODUCED"
        assert db_ret_order.cz_return_doc_id is not None

        # Проверка операции в kiz_operations
        op_ret = db.query(KizOperation).filter(
            KizOperation.seller_id == seller_id,
            KizOperation.order_id == return_order_id,
            KizOperation.operation == KizOperationType.RETURN,
        ).first()
        assert op_ret is not None
        assert op_ret.status == "SUCCESS"

    # ================= ЭТАП 2: ПРИВЯЗКА К НОВОМУ ЗАКАЗУ =================
    with Session(sync_engine) as db:
        db_new_order = db.query(Order).filter(Order.id == new_sale_order_id).first()
        db_new_order.kiz_code = TEST_KIZ
        db_new_order.kiz_status = KizStatus.ATTACHED
        db_new_order.kiz_cz_status = "INTRODUCED"
        db_new_order.status = OrderStatus.DELIVERED
        db.commit()

    # ================= ЭТАП 3: ВЫВОД ИЗ ОБОРОТА (ДИСТАНЦИОННАЯ ПРОДАЖА) =================
    with patch("app.services.cz_client.CZClient._create_document", side_effect=fake_create_document), \
         patch("app.services.cz_client.CZClient.get_document_status", return_value={"status": "CHECKED_OK"}), \
         patch("app.agents.notifier.send_cz_status_notification.delay"):

        withdraw_order_kiz(
            seller_id=seller_id,
            order_id=new_sale_order_id,
            kiz_code=TEST_KIZ,
            price_kopecks=TEST_PRICE_KOP,
        )

    # Проверка формирования документа LK_RECEIPT
    assert len(captured_docs) == 2
    withdraw_doc = captured_docs[1]
    assert withdraw_doc["type"] == "LK_RECEIPT"
    with_payload = json.loads(withdraw_doc["productDocument"])
    assert with_payload["inn"] == TEST_INN
    assert with_payload["action"] == "DISTANCE"  # Дистанционная продажа
    assert with_payload["fias_id"] == TEST_FIAS_ID
    assert with_payload["products"][0]["cis"] == TEST_KIZ
    assert with_payload["products"][0]["product_cost"] == TEST_PRICE_KOP
    assert with_payload["products"][0]["primary_document_number"] == str(new_sale_order_id)

    with Session(sync_engine) as db:
        db_sale_order = db.query(Order).filter(Order.id == new_sale_order_id).first()
        assert db_sale_order.kiz_status == KizStatus.WITHDRAWN
        assert db_sale_order.kiz_cz_status == "RETIRED"
        assert db_sale_order.cz_withdrawal_doc_id is not None

        # Проверка операции в kiz_operations
        op_with = db.query(KizOperation).filter(
            KizOperation.seller_id == seller_id,
            KizOperation.order_id == new_sale_order_id,
            KizOperation.operation == KizOperationType.WITHDRAWAL,
        ).first()
        assert op_with is not None
        assert op_with.status == "SUCCESS"

        # Проверка логов аудита
        audit_records = db.query(AuditLog).filter(AuditLog.seller_id == seller_id).all()
        agents = [a.agent for a in audit_records]
        assert "cz_return" in agents
        assert "cz_withdrawal" in agents
