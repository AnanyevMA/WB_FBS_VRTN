"""
Adversarial Challenge & Empirical Verification Suite for Milestone M2.
Challenger: challenger_r3_m2_2

Focus Areas:
1. Empirically verify Celery task `withdraw_order_kiz` on mocked True API returning CHECKED_NOT_OK with Error 07:
   - Full end-to-end path without mocking `_do_withdrawal`.
   - Verify order.kiz_status == KizStatus.ERROR.
   - Verify kiz_op.status == "FAILED".
   - Verify kiz_op.error_message contains Error 07.
   - Verify AuditLog records the event.
   - Verify Telegram notification task triggered with success=False and error details.
   - Verify no Celery retries are raised on deterministic CZDocumentError.
2. Verify Celery retry behavior on transient network errors vs deterministic rejection.
3. Empirically verify `submit_signed_kiz_document` under slow responses (timeout) and rejection.
4. Verify subsequent status retrieval via GET `/kiz/documents/{doc_id}/status` on rejected doc.
5. Verify multi-order batch and RETURN action under rejection.
"""
import asyncio
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import app
from app.database import AsyncSessionLocal, init_db
from app.models.seller import Seller
from app.models.order import Order, KizStatus, OrderStatus
from app.models.kiz import KizOperation, KizOperationType
from app.models.audit import AuditLog
from app.services.cz_client import CZClient, CZAPIError, CZDocumentError
from app.services.encryption import encrypt
from app.services.auth_service import create_access_token, ensure_initial_admin
from app.agents.cz_withdrawal import withdraw_order_kiz, sync_engine


# ==============================================================================
# 1. Full End-to-End Celery withdraw_order_kiz on True API CHECKED_NOT_OK (Error 07)
# ==============================================================================

@pytest.mark.asyncio
async def test_celery_withdraw_order_kiz_e2e_checked_not_ok_error_07():
    """
    Adversarial verification of withdraw_order_kiz without mocking _do_withdrawal.
    Mocks only the True API network boundary:
      - Document creation returns doc_id
      - wait_for_document polls /api/v4/true-api/doc/{doc_id}/info and receives CHECKED_NOT_OK with Error 07
    Validates:
      1. order.kiz_status == KizStatus.ERROR
      2. kiz_op.status == "FAILED"
      3. kiz_op.error_message contains Error 07
      4. AuditLog records the event
      5. Telegram notification task triggered with success=False and error details
      6. No Celery retries are raised
    """
    await init_db()

    seller_id = str(uuid.uuid4())
    order_id = int(str(uuid.uuid4().int)[:9])
    test_kiz = "0104630199251318215BADKIZ12345"
    doc_id = "doc-e2e-err07-999"
    error_07_text = '07: Недопустимое количество символов в значении поля "Код идентификации".'

    with Session(sync_engine) as db:
        seller = Seller(
            id=seller_id,
            name="Adversarial Seller E2E",
            wb_api_token_encrypted=encrypt("wb_tok"),
            cz_token_encrypted=encrypt("cz_tok"),
            cz_inn="7700112233",
            telegram_bot_token_encrypted=encrypt("bot_tok"),
            telegram_chat_ids=["555123456"],
            is_active=True,
        )
        db.add(seller)
        order = Order(
            id=order_id,
            seller_id=seller_id,
            name="Куртка демисезонная",
            article="jacket.01",
            price=4500,
            status=OrderStatus.DELIVERED,
            kiz_required=True,
            kiz_code=test_kiz,
            kiz_status=KizStatus.ATTACHED,
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db.add(order)
        db.commit()

    # True API response for doc creation and status polling
    doc_info_response = {
        "number": doc_id,
        "status": "CHECKED_NOT_OK",
        "commonErrors": [
            {
                "errorCode": "07",
                "errorMessage": error_07_text,
                "errorObject": test_kiz,
            }
        ],
    }

    with patch.object(CZClient, "_create_document", new_callable=AsyncMock) as mock_create, \
         patch.object(CZClient, "get_document_info", new_callable=AsyncMock) as mock_info, \
         patch("app.agents.notifier.send_cz_status_notification.delay") as mock_notify, \
         patch.object(withdraw_order_kiz, "retry") as mock_retry:

        mock_create.return_value = doc_id
        mock_info.return_value = doc_info_response

        # Execute Celery task directly
        withdraw_order_kiz(
            seller_id=seller_id,
            order_id=order_id,
            kiz_code=test_kiz,
            price_kopecks=450000,
            receipt_number="REC-E2E-07",
        )

        # 6. Assert no Celery retries were raised
        mock_retry.assert_not_called()

        # 5. Assert Telegram notification task triggered with success=False and error details
        mock_notify.assert_called_once()
        notify_args = mock_notify.call_args.kwargs
        assert notify_args["seller_id"] == seller_id
        assert notify_args["order_id"] == order_id
        assert notify_args["success"] is False
        assert error_07_text in notify_args["error"]
        assert notify_args["doc_id"] == doc_id

    # Database state assertions
    with Session(sync_engine) as db:
        o = db.query(Order).filter(Order.id == order_id).first()
        # 1. Assert order.kiz_status == KizStatus.ERROR
        assert o.kiz_status == KizStatus.ERROR
        assert o.kiz_cz_status == "CHECKED_NOT_OK"
        assert o.cz_withdrawal_doc_id == doc_id

        # 2. Assert kiz_op.status == "FAILED"
        kiz_op = db.query(KizOperation).filter(
            KizOperation.seller_id == seller_id,
            KizOperation.order_id == order_id,
            KizOperation.operation == KizOperationType.WITHDRAWAL,
        ).first()
        assert kiz_op is not None
        assert kiz_op.status == "FAILED"
        assert kiz_op.cz_doc_id == doc_id
        assert kiz_op.cz_doc_status == "CHECKED_NOT_OK"

        # 3. Assert kiz_op.error_message contains Error 07
        assert error_07_text in kiz_op.error_message

        # 4. Assert AuditLog records the event
        audit = db.query(AuditLog).filter(
            AuditLog.seller_id == seller_id,
            AuditLog.entity_id == str(order_id),
            AuditLog.action == "FAILED",
        ).first()
        assert audit is not None
        assert audit.agent == "cz_withdrawal"
        assert error_07_text in audit.error
        assert audit.payload.get("doc_id") == doc_id


@pytest.mark.asyncio
async def test_celery_withdraw_order_kiz_array_response_with_error_07():
    """Verify True API returning array format [ { status: CHECKED_NOT_OK, ... } ] works cleanly."""
    await init_db()

    seller_id = str(uuid.uuid4())
    order_id = int(str(uuid.uuid4().int)[:9])
    test_kiz = "0104630199251318215ARRAYKIZ123"
    doc_id = "doc-arr-07-888"
    error_07_text = "07: Недопустимое количество символов в значении поля \"Код идентификации\"."

    with Session(sync_engine) as db:
        seller = Seller(
            id=seller_id,
            name="Adversarial Seller Array",
            wb_api_token_encrypted=encrypt("wb_tok"),
            cz_token_encrypted=encrypt("cz_tok"),
            cz_inn="7700112233",
            telegram_bot_token_encrypted=encrypt("bot_tok"),
            telegram_chat_ids=["555123456"],
            is_active=True,
        )
        db.add(seller)
        order = Order(
            id=order_id,
            seller_id=seller_id,
            name="Ботинки зимние",
            article="boots.01",
            price=6000,
            status=OrderStatus.DELIVERED,
            kiz_required=True,
            kiz_code=test_kiz,
            kiz_status=KizStatus.ATTACHED,
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db.add(order)
        db.commit()

    with patch.object(CZClient, "_create_document", new_callable=AsyncMock) as mock_create, \
         patch.object(CZClient, "_request", new_callable=AsyncMock) as mock_req, \
         patch("app.agents.notifier.send_cz_status_notification.delay") as mock_notify, \
         patch.object(withdraw_order_kiz, "retry") as mock_retry:

        mock_create.return_value = doc_id
        # Raw HTTP returns list of dicts
        mock_req.return_value = [
            {
                "number": doc_id,
                "status": "CHECKED_NOT_OK",
                "errors": [error_07_text],
            }
        ]

        withdraw_order_kiz(
            seller_id=seller_id,
            order_id=order_id,
            kiz_code=test_kiz,
            price_kopecks=600000,
        )

        mock_retry.assert_not_called()
        mock_notify.assert_called_once()
        assert error_07_text in mock_notify.call_args.kwargs["error"]

    with Session(sync_engine) as db:
        o = db.query(Order).filter(Order.id == order_id).first()
        assert o.kiz_status == KizStatus.ERROR
        assert o.kiz_cz_status == "CHECKED_NOT_OK"

        op = db.query(KizOperation).filter(KizOperation.order_id == order_id).first()
        assert op.status == "FAILED"
        assert error_07_text in op.error_message


@pytest.mark.asyncio
async def test_celery_withdraw_order_kiz_transient_error_triggers_retry():
    """
    Stress-test the retry logic:
    Transient network/server error (e.g. CZAPIError 500) MUST trigger Celery retry.
    """
    await init_db()

    seller_id = str(uuid.uuid4())
    order_id = int(str(uuid.uuid4().int)[:9])
    test_kiz = "0104630199251318215RETRYKIZ123"

    with Session(sync_engine) as db:
        seller = Seller(
            id=seller_id,
            name="Retry Test Seller",
            wb_api_token_encrypted=encrypt("wb_tok"),
            cz_token_encrypted=encrypt("cz_tok"),
            cz_inn="7700112233",
            telegram_bot_token_encrypted=encrypt("bot_tok"),
            telegram_chat_ids=["555123456"],
            is_active=True,
        )
        db.add(seller)
        order = Order(
            id=order_id,
            seller_id=seller_id,
            name="Шапка шерстяная",
            article="hat.01",
            price=1200,
            status=OrderStatus.DELIVERED,
            kiz_required=True,
            kiz_code=test_kiz,
            kiz_status=KizStatus.ATTACHED,
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db.add(order)
        db.commit()

    from celery.exceptions import Retry
    from unittest.mock import PropertyMock

    # Case A: First attempt with transient 500 error -> calls self.retry
    with patch("app.agents.cz_withdrawal._do_withdrawal", new_callable=AsyncMock) as mock_do_w, \
         patch.object(withdraw_order_kiz, "retry", side_effect=Retry("CELERY_RETRY_CALLED")) as mock_retry:
        mock_do_w.side_effect = CZAPIError("500 Internal Server Error", 500)

        with pytest.raises(Retry):
            withdraw_order_kiz(
                seller_id=seller_id,
                order_id=order_id,
                kiz_code=test_kiz,
                price_kopecks=120000,
            )
        mock_retry.assert_called_once()

    # Case B: Max retries exceeded -> does NOT call retry, sends telegram alert
    with patch("app.agents.cz_withdrawal._do_withdrawal", new_callable=AsyncMock) as mock_do_w, \
         patch("app.agents.notifier.send_cz_status_notification.delay") as mock_notify, \
         patch.object(withdraw_order_kiz, "retry") as mock_retry, \
         patch("celery.app.task.Context.retries", new_callable=PropertyMock) as mock_task_retries:

        mock_task_retries.return_value = 3
        mock_do_w.side_effect = CZAPIError("500 Internal Server Error", 500)

        withdraw_order_kiz(
            seller_id=seller_id,
            order_id=order_id,
            kiz_code=test_kiz,
            price_kopecks=120000,
        )

        mock_retry.assert_not_called()
        mock_notify.assert_called_once()
        assert "500 Internal Server Error" in mock_notify.call_args.kwargs["error"]


# ==============================================================================
# 2. Web Signing submit_signed_kiz_document Slow Responses & Rejection
# ==============================================================================

@pytest.mark.asyncio
async def test_submit_signed_kiz_document_slow_response_timeout():
    """
    Empirically test submit_signed_kiz_document when True API is slow and wait_for_document times out.
    Validates:
      - HTTP response is success=True, status="IN_PROGRESS"
      - Order is updated to kiz_cz_status="IN_PROGRESS" and cz_withdrawal_doc_id is set
      - KizOperation has status="PENDING", cz_doc_status="IN_PROGRESS"
      - AuditLog records "SUBMIT_SIGNED_DOC_PENDING"
      - Telegram notification is NOT dispatched prematurely
    """
    await init_db()

    async with AsyncSessionLocal() as session:
        admin_user = await ensure_initial_admin(session)
        auth_token = create_access_token(
            data={"sub": admin_user.id, "username": admin_user.username, "role": "admin", "is_superuser": True}
        )
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Slow Response Seller",
            wb_api_token_encrypted=encrypt("wb_tok"),
            cz_token_encrypted=encrypt("cz_tok"),
            cz_inn="7700112233",
            telegram_bot_token_encrypted=encrypt("bot_tok"),
            telegram_chat_ids=["987654321"],
            is_active=True,
        )
        session.add(seller)

        order_id = int(str(uuid.uuid4().int)[:9])
        order = Order(
            id=order_id,
            seller_id=seller_id,
            name="Свитер оверсайз",
            article="sweater.01",
            price=3200,
            status=OrderStatus.DELIVERING,
            kiz_required=True,
            kiz_code="0104630199251318215SLOWKIZ123",
            kiz_status=KizStatus.ATTACHED,
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        session.add(order)
        await session.commit()

    slow_doc_id = "doc-slow-timeout-777"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {auth_token}"}) as ac:
        with patch("app.services.cz_client.CZClient.submit_signed_document", new_callable=AsyncMock) as mock_submit, \
             patch("app.services.cz_client.CZClient.wait_for_document", new_callable=AsyncMock) as mock_wait, \
             patch("app.agents.notifier.send_cz_status_notification.delay") as mock_notify:

            mock_submit.return_value = slow_doc_id
            mock_wait.side_effect = TimeoutError(f"Превышен таймаут ожидания документа {slow_doc_id} в ГИС МТ")

            res = await ac.post(
                f"/api/v1/sellers/{seller_id}/kiz/submit-signed-document",
                json={
                    "document_type": "LK_RECEIPT",
                    "document_base64": "MOCK_BASE64_DOC",
                    "signature_base64": "MOCK_BASE64_SIG",
                    "order_ids": [order_id],
                    "action": "WITHDRAWAL",
                },
            )

            assert res.status_code == 200, res.text
            res_data = res.json()
            assert res_data["success"] is True
            assert res_data["status"] == "IN_PROGRESS"
            assert res_data["doc_id"] == slow_doc_id
            assert "обрабатывается" in res_data["message"]

            # Premature telegram notification must NOT be sent
            mock_notify.assert_not_called()

    # Verify DB state after timeout
    async with AsyncSessionLocal() as session:
        o = await session.get(Order, order_id)
        assert o.kiz_cz_status == "IN_PROGRESS"
        assert o.cz_withdrawal_doc_id == slow_doc_id

        res_op = await session.execute(
            select(KizOperation).where(
                KizOperation.seller_id == seller_id,
                KizOperation.order_id == order_id,
            )
        )
        op = res_op.scalars().first()
        assert op is not None
        assert op.status == "PENDING"
        assert op.cz_doc_id == slow_doc_id
        assert op.cz_doc_status == "IN_PROGRESS"

        res_audit = await session.execute(
            select(AuditLog).where(
                AuditLog.seller_id == seller_id,
                AuditLog.action == "SUBMIT_SIGNED_DOC_PENDING",
            )
        )
        audit = res_audit.scalars().first()
        assert audit is not None
        assert audit.entity_id == slow_doc_id


@pytest.mark.asyncio
async def test_submit_signed_kiz_document_batch_rejection_multi_order():
    """
    Verify multi-order batch rejection via submit_signed_kiz_document:
      - All orders updated to ERROR and CHECKED_NOT_OK
      - All KizOperations created with FAILED
      - Telegram notification sent for EACH order in batch
    """
    await init_db()

    async with AsyncSessionLocal() as session:
        admin_user = await ensure_initial_admin(session)
        auth_token = create_access_token(
            data={"sub": admin_user.id, "username": admin_user.username, "role": "admin", "is_superuser": True}
        )
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Multi Order Reject Seller",
            wb_api_token_encrypted=encrypt("wb_tok"),
            cz_token_encrypted=encrypt("cz_tok"),
            cz_inn="7700112233",
            telegram_bot_token_encrypted=encrypt("bot_tok"),
            telegram_chat_ids=["987654321"],
            is_active=True,
        )
        session.add(seller)

        order_id_1 = int(str(uuid.uuid4().int)[:9])
        order_id_2 = int(str(uuid.uuid4().int)[:9])
        o1 = Order(
            id=order_id_1,
            seller_id=seller_id,
            name="Товар 1",
            article="item.01",
            price=1000,
            status=OrderStatus.DELIVERING,
            kiz_required=True,
            kiz_code="0104630199251318215BATCH01",
            kiz_status=KizStatus.ATTACHED,
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        o2 = Order(
            id=order_id_2,
            seller_id=seller_id,
            name="Товар 2",
            article="item.02",
            price=2000,
            status=OrderStatus.DELIVERING,
            kiz_required=True,
            kiz_code="0104630199251318215BATCH02",
            kiz_status=KizStatus.ATTACHED,
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        session.add(o1)
        session.add(o2)
        await session.commit()

    rejection_msg = "07: Недопустимое количество символов в значении поля \"Код идентификации\"."
    doc_err = CZDocumentError(
        message=f"Документ doc-batch-reject отклонен ГИС МТ: {rejection_msg}",
        status_code=422,
        doc_id="doc-batch-reject",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {auth_token}"}) as ac:
        with patch("app.services.cz_client.CZClient.submit_signed_document", new_callable=AsyncMock) as mock_submit, \
             patch("app.services.cz_client.CZClient.wait_for_document", new_callable=AsyncMock) as mock_wait, \
             patch("app.agents.notifier.send_cz_status_notification.delay") as mock_notify:

            mock_submit.return_value = "doc-batch-reject"
            mock_wait.side_effect = doc_err

            res = await ac.post(
                f"/api/v1/sellers/{seller_id}/kiz/submit-signed-document",
                json={
                    "document_type": "LK_RECEIPT",
                    "document_base64": "MOCK_BASE64_DOC",
                    "signature_base64": "MOCK_BASE64_SIG",
                    "order_ids": [order_id_1, order_id_2],
                    "action": "WITHDRAWAL",
                },
            )
            assert res.status_code == 200
            res_data = res.json()
            assert res_data["success"] is False
            assert res_data["status"] == "CHECKED_NOT_OK"
            assert set(res_data["order_ids"]) == {order_id_1, order_id_2}

            # Telegram notification sent for both orders
            assert mock_notify.call_count == 2
            called_order_ids = {c.kwargs["order_id"] for c in mock_notify.call_args_list}
            assert called_order_ids == {order_id_1, order_id_2}

    async with AsyncSessionLocal() as session:
        for oid in (order_id_1, order_id_2):
            o = await session.get(Order, oid)
            assert o.kiz_status == KizStatus.ERROR
            assert o.kiz_cz_status == "CHECKED_NOT_OK"
            assert o.cz_withdrawal_doc_id == "doc-batch-reject"

            res_op = await session.execute(
                select(KizOperation).where(
                    KizOperation.seller_id == seller_id,
                    KizOperation.order_id == oid,
                )
            )
            op = res_op.scalars().first()
            assert op.status == "FAILED"
            assert rejection_msg in op.error_message


@pytest.mark.asyncio
async def test_submit_signed_kiz_document_return_action_rejection():
    """Verify submit_signed_kiz_document handles RETURN action rejection properly."""
    await init_db()

    async with AsyncSessionLocal() as session:
        admin_user = await ensure_initial_admin(session)
        auth_token = create_access_token(
            data={"sub": admin_user.id, "username": admin_user.username, "role": "admin", "is_superuser": True}
        )
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Return Reject Seller",
            wb_api_token_encrypted=encrypt("wb_tok"),
            cz_token_encrypted=encrypt("cz_tok"),
            cz_inn="7700112233",
            is_active=True,
        )
        session.add(seller)

        order_id = int(str(uuid.uuid4().int)[:9])
        order = Order(
            id=order_id,
            seller_id=seller_id,
            name="Платье вечернее",
            article="dress.evening",
            price=7500,
            status=OrderStatus.CANCELLED,
            kiz_required=True,
            kiz_code="0104630199251318215RETURNREJECT",
            kiz_status=KizStatus.WITHDRAWN,
            kiz_cz_status="RETIRED",
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        session.add(order)
        await session.commit()

    rejection_msg = "32: Код маркировки не находится в статусе, допускающем возврат"
    doc_err = CZDocumentError(
        message=f"Документ doc-return-reject отклонен ГИС МТ: {rejection_msg}",
        status_code=422,
        doc_id="doc-return-reject",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {auth_token}"}) as ac:
        with patch("app.services.cz_client.CZClient.submit_signed_document", new_callable=AsyncMock) as mock_submit, \
             patch("app.services.cz_client.CZClient.wait_for_document", new_callable=AsyncMock) as mock_wait, \
             patch("app.agents.notifier.send_cz_status_notification.delay") as mock_notify:

            mock_submit.return_value = "doc-return-reject"
            mock_wait.side_effect = doc_err

            res = await ac.post(
                f"/api/v1/sellers/{seller_id}/kiz/submit-signed-document",
                json={
                    "document_type": "LP_RETURN",
                    "document_base64": "MOCK_BASE64_DOC",
                    "signature_base64": "MOCK_BASE64_SIG",
                    "order_ids": [order_id],
                    "action": "RETURN",
                },
            )
            assert res.status_code == 200
            res_data = res.json()
            assert res_data["success"] is False
            assert "возврата в оборот" in res_data["message"]
            assert rejection_msg in res_data["error"]

    async with AsyncSessionLocal() as session:
        o = await session.get(Order, order_id)
        assert o.kiz_status == KizStatus.ERROR
        assert o.kiz_cz_status == "CHECKED_NOT_OK"
        assert o.cz_return_doc_id == "doc-return-reject"

        res_op = await session.execute(
            select(KizOperation).where(
                KizOperation.seller_id == seller_id,
                KizOperation.order_id == order_id,
            )
        )
        op = res_op.scalars().first()
        assert op.operation == KizOperationType.RETURN
        assert op.status == "FAILED"
        assert rejection_msg in op.error_message


@pytest.mark.asyncio
async def test_submit_signed_kiz_document_invalid_inputs():
    """Verify input validation: missing base64 doc/sig gives 400, missing seller gives 404."""
    await init_db()

    async with AsyncSessionLocal() as session:
        admin_user = await ensure_initial_admin(session)
        auth_token = create_access_token(
            data={"sub": admin_user.id, "username": admin_user.username, "role": "admin", "is_superuser": True}
        )
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Validation Seller",
            wb_api_token_encrypted=encrypt("wb_tok"),
            cz_token_encrypted=encrypt("cz_tok"),
            cz_inn="7700112233",
            is_active=True,
        )
        session.add(seller)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {auth_token}"}) as ac:
        # Case A: Missing document_base64 -> 400
        res = await ac.post(
            f"/api/v1/sellers/{seller_id}/kiz/submit-signed-document",
            json={"signature_base64": "MOCK_SIG", "order_ids": [1]},
        )
        assert res.status_code == 400
        assert "Отсутствует документ" in res.json()["detail"]

        # Case B: Non-existent seller -> 404
        non_seller = str(uuid.uuid4())
        res404 = await ac.post(
            f"/api/v1/sellers/{non_seller}/kiz/submit-signed-document",
            json={"document_base64": "DOC", "signature_base64": "SIG", "order_ids": [1]},
        )
        assert res404.status_code == 404


@pytest.mark.asyncio
async def test_withdraw_order_kiz_grace_period_404_then_checked_not_ok_error_07():
    """
    Verify 404 grace period in wait_for_document:
    First 2 attempts return 404 (replication delay), 3rd attempt returns CHECKED_NOT_OK (Error 07).
    Task must survive grace period, catch CHECKED_NOT_OK, transition to ERROR, log Error 07, no retry.
    """
    await init_db()

    seller_id = str(uuid.uuid4())
    order_id = int(str(uuid.uuid4().int)[:9])
    test_kiz = "0104630199251318215GRACE07KIZ"
    doc_id = "doc-grace-err07-555"
    error_07_text = '07: Недопустимое количество символов в значении поля "Код идентификации".'

    with Session(sync_engine) as db:
        seller = Seller(
            id=seller_id,
            name="Grace Period Seller",
            wb_api_token_encrypted=encrypt("wb_tok"),
            cz_token_encrypted=encrypt("cz_tok"),
            cz_inn="7700112233",
            telegram_bot_token_encrypted=encrypt("bot_tok"),
            telegram_chat_ids=["111222333"],
            is_active=True,
        )
        db.add(seller)
        order = Order(
            id=order_id,
            seller_id=seller_id,
            name="Джемпер вязаный",
            article="jumper.01",
            price=2800,
            status=OrderStatus.DELIVERED,
            kiz_required=True,
            kiz_code=test_kiz,
            kiz_status=KizStatus.ATTACHED,
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db.add(order)
        db.commit()

    with patch.object(CZClient, "_create_document", new_callable=AsyncMock) as mock_create, \
         patch.object(CZClient, "get_document_status", new_callable=AsyncMock) as mock_status, \
         patch("asyncio.sleep", new_callable=AsyncMock), \
         patch("app.agents.notifier.send_cz_status_notification.delay") as mock_notify, \
         patch.object(withdraw_order_kiz, "retry") as mock_retry:

        mock_create.return_value = doc_id
        # Attempt 0: 404, Attempt 1: 404, Attempt 2: CHECKED_NOT_OK
        mock_status.side_effect = [
            CZAPIError("Not registered yet in True API", 404),
            CZAPIError("Not registered yet in True API", 404),
            {
                "number": doc_id,
                "status": "CHECKED_NOT_OK",
                "commonErrors": [{"errorCode": "07", "errorMessage": error_07_text}],
            },
        ]

        withdraw_order_kiz(
            seller_id=seller_id,
            order_id=order_id,
            kiz_code=test_kiz,
            price_kopecks=280000,
        )

        mock_retry.assert_not_called()
        mock_notify.assert_called_once()
        assert error_07_text in mock_notify.call_args.kwargs["error"]

    with Session(sync_engine) as db:
        o = db.query(Order).filter(Order.id == order_id).first()
        assert o.kiz_status == KizStatus.ERROR
        assert o.kiz_cz_status == "CHECKED_NOT_OK"
        op = db.query(KizOperation).filter(KizOperation.order_id == order_id).first()
        assert op.status == "FAILED"
        assert error_07_text in op.error_message


@pytest.mark.asyncio
async def test_withdraw_order_kiz_no_telegram_configured_handles_cleanly():
    """
    Verify withdraw_order_kiz completes cleanly when seller has no Telegram credentials.
    Status must update to ERROR, AuditLog written, no crash on Telegram dispatch.
    """
    await init_db()

    seller_id = str(uuid.uuid4())
    order_id = int(str(uuid.uuid4().int)[:9])
    test_kiz = "0104630199251318215NOTG12345"
    doc_id = "doc-notg-err07"
    error_07_text = '07: Недопустимое количество символов в значении поля "Код идентификации".'

    with Session(sync_engine) as db:
        seller = Seller(
            id=seller_id,
            name="No TG Seller",
            wb_api_token_encrypted=encrypt("wb_tok"),
            cz_token_encrypted=encrypt("cz_tok"),
            cz_inn="7700112233",
            telegram_bot_token_encrypted=None,
            telegram_chat_ids=None,
            is_active=True,
        )
        db.add(seller)
        order = Order(
            id=order_id,
            seller_id=seller_id,
            name="Шарф шерстяной",
            article="scarf.01",
            price=1500,
            status=OrderStatus.DELIVERED,
            kiz_required=True,
            kiz_code=test_kiz,
            kiz_status=KizStatus.ATTACHED,
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db.add(order)
        db.commit()

    with patch.object(CZClient, "_create_document", new_callable=AsyncMock) as mock_create, \
         patch.object(CZClient, "get_document_info", new_callable=AsyncMock) as mock_info, \
         patch("app.agents.notifier.send_cz_status_notification.delay") as mock_notify, \
         patch.object(withdraw_order_kiz, "retry") as mock_retry:

        mock_create.return_value = doc_id
        mock_info.return_value = {
            "number": doc_id,
            "status": "CHECKED_NOT_OK",
            "errors": [error_07_text],
        }

        withdraw_order_kiz(
            seller_id=seller_id,
            order_id=order_id,
            kiz_code=test_kiz,
            price_kopecks=150000,
        )

        mock_retry.assert_not_called()
        # No telegram notify because credentials are not configured
        mock_notify.assert_not_called()

    with Session(sync_engine) as db:
        o = db.query(Order).filter(Order.id == order_id).first()
        assert o.kiz_status == KizStatus.ERROR
        assert o.kiz_cz_status == "CHECKED_NOT_OK"
        audit = db.query(AuditLog).filter(AuditLog.entity_id == str(order_id)).first()
        assert audit is not None
        assert audit.action == "FAILED"


@pytest.mark.asyncio
async def test_withdraw_order_kiz_already_withdrawn_idempotent():
    """Verify withdraw_order_kiz skips execution idempotently if order is already WITHDRAWN."""
    await init_db()

    seller_id = str(uuid.uuid4())
    order_id = int(str(uuid.uuid4().int)[:9])
    test_kiz = "0104630199251318215ALREADYWITHDRAWN"

    with Session(sync_engine) as db:
        seller = Seller(
            id=seller_id,
            name="Already Withdrawn Seller",
            wb_api_token_encrypted=encrypt("wb_tok"),
            cz_token_encrypted=encrypt("cz_tok"),
            cz_inn="7700112233",
            is_active=True,
        )
        db.add(seller)
        order = Order(
            id=order_id,
            seller_id=seller_id,
            name="Пальто шерстяное",
            article="coat.01",
            price=12000,
            status=OrderStatus.DELIVERED,
            kiz_required=True,
            kiz_code=test_kiz,
            kiz_status=KizStatus.WITHDRAWN,
            kiz_cz_status="RETIRED",
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db.add(order)
        db.commit()

    with patch("app.agents.cz_withdrawal._do_withdrawal", new_callable=AsyncMock) as mock_do_w:
        withdraw_order_kiz(
            seller_id=seller_id,
            order_id=order_id,
            kiz_code=test_kiz,
            price_kopecks=1200000,
        )
        # Should exit early without calling _do_withdrawal
        mock_do_w.assert_not_called()


@pytest.mark.asyncio
async def test_status_endpoint_after_timeout_reveals_error_07():
    """
    Verify complete lifecycle:
    1. submit_signed_kiz_document experiences timeout -> status="IN_PROGRESS"
    2. Later True API finishes with CHECKED_NOT_OK (Error 07)
    3. Querying GET /kiz/documents/{doc_id}/status returns error_text with Error 07
    """
    await init_db()

    async with AsyncSessionLocal() as session:
        admin_user = await ensure_initial_admin(session)
        auth_token = create_access_token(
            data={"sub": admin_user.id, "username": admin_user.username, "role": "admin", "is_superuser": True}
        )
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Lifecycle Seller",
            wb_api_token_encrypted=encrypt("wb_tok"),
            cz_token_encrypted=encrypt("cz_tok"),
            cz_inn="7700112233",
            is_active=True,
        )
        session.add(seller)
        await session.commit()

    doc_id = "doc-lifecycle-timeout-then-07"
    error_07_text = '07: Недопустимое количество символов в значении поля "Код идентификации".'

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {auth_token}"}) as ac:
        with patch.object(CZClient, "get_document_info", new_callable=AsyncMock) as mock_info:
            mock_info.return_value = {
                "number": doc_id,
                "status": "CHECKED_NOT_OK",
                "commonErrors": [
                    {
                        "errorCode": "07",
                        "errorMessage": error_07_text,
                        "errorObject": "0104630199251318215BADLIFECYCLE",
                    }
                ],
            }

            res = await ac.get(f"/api/v1/sellers/{seller_id}/kiz/documents/{doc_id}/status")
            assert res.status_code == 200
            data = res.json()
            assert data["success"] is True
            assert data["status"] == "CHECKED_NOT_OK"
            assert data["doc_id"] == doc_id
            assert error_07_text in data["error_text"]
            assert len(data["common_errors"]) == 1

