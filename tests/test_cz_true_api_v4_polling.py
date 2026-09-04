import pytest
import uuid
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock
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
# 1. CZClient True API v4 Endpoint & Error Extraction Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_cz_client_get_document_info_v4_endpoint():
    """Verify CZClient.get_document_info queries /api/v4/true-api/doc/{doc_id}/info?pg=lp."""
    client = CZClient(inn="7700112233", token="mock-token")

    # Case A: Response is a dict
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {"number": "doc-uuid-111", "status": "CHECKED_OK"}
        info = await client.get_document_info("doc-uuid-111", pg="lp")

        mock_req.assert_called_once_with(
            "GET",
            "/api/v4/true-api/doc/doc-uuid-111/info",
            params={"pg": "lp"},
            sign_request=False,
        )
        assert info["status"] == "CHECKED_OK"
        assert info["documentId"] == "doc-uuid-111"

    # Case B: Response is a list of dicts [ {...} ]
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = [{"number": "doc-uuid-222", "status": "CHECKED_NOT_OK"}]
        info = await client.get_document_info("doc-uuid-222", pg="lp")
        assert info["status"] == "CHECKED_NOT_OK"
        assert info["documentId"] == "doc-uuid-222"

    # Case C: get_document_status backward compatible alias
    with patch.object(client, "get_document_info", new_callable=AsyncMock) as mock_info:
        mock_info.return_value = {"status": "CHECKED_OK", "documentId": "doc-333"}
        res = await client.get_document_status("doc-333", pg="lp")
        mock_info.assert_called_once_with("doc-333", pg="lp")
        assert res["status"] == "CHECKED_OK"


def test_cz_client_extract_document_error_text():
    """Verify error parsing from both errors (strings) and commonErrors (objects)."""
    # 1. String in errors
    doc1 = {
        "status": "CHECKED_NOT_OK",
        "errors": ["07: Недопустимое количество символов в значении поля \"Код идентификации\"."],
    }
    err1 = CZClient.extract_document_error_text(doc1)
    assert "07: Недопустимое количество символов в значении поля \"Код идентификации\"." in err1

    # 2. Object in commonErrors with errorCode, errorMessage, errorObject
    doc2 = {
        "status": "CHECKED_NOT_OK",
        "commonErrors": [
            {
                "errorCode": "07",
                "errorMessage": "07: Недопустимое количество символов в значении поля \"Код идентификации\".",
                "errorObject": "0104630199251318215QTSRh>4sVc+. 91EE12 92...",
            }
        ],
    }
    err2 = CZClient.extract_document_error_text(doc2)
    assert "07: Недопустимое количество символов" in err2
    assert "0104630199251318215QTSRh>4sVc+." in err2

    # 3. commonErrors without code in errorMessage
    doc3 = {
        "status": "FAILED",
        "commonErrors": [
            {
                "errorCode": "12",
                "errorMessage": "Код маркировки не принадлежит участнику оборота",
            }
        ],
    }
    err3 = CZClient.extract_document_error_text(doc3)
    assert err3 == "12: Код маркировки не принадлежит участнику оборота"

    # 4. Deduplication between errors and commonErrors
    doc4 = {
        "status": "CHECKED_NOT_OK",
        "errors": ["07: Недопустимое количество символов в значении поля \"Код идентификации\"."],
        "commonErrors": [
            {
                "errorCode": "07",
                "errorMessage": "07: Недопустимое количество символов в значении поля \"Код идентификации\".",
            }
        ],
    }
    err4 = CZClient.extract_document_error_text(doc4)
    assert err4 == "07: Недопустимое количество символов в значении поля \"Код идентификации\"."

    # 5. Fallback if empty
    doc5 = {"status": "FAILED"}
    err5 = CZClient.extract_document_error_text(doc5)
    assert err5 == "Отказ ГИС МТ со статусом FAILED"

    doc6 = {"status": "CANCELLED", "statusComment": "Документ аннулирован пользователем"}
    err6 = CZClient.extract_document_error_text(doc6)
    assert err6 == "Документ аннулирован пользователем"


# ==============================================================================
# 2. wait_for_document Polling, 404 Unmasking & Terminal States Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_wait_for_document_polling_progression_to_checked_ok():
    """Verify wait_for_document loops through IN_PROGRESS and completes on CHECKED_OK."""
    client = CZClient(inn="7700112233", token="mock-token")
    responses = [
        {"status": "IN_PROGRESS"},
        {"status": "WAIT_FOR_CONTINUATION"},
        {"status": "CHECKED_OK", "number": "doc-prog-1"},
    ]

    with patch.object(client, "get_document_status", new_callable=AsyncMock) as mock_status, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_status.side_effect = responses

        final_data = await client.wait_for_document("doc-prog-1", max_attempts=5, interval_seconds=0.01)
        assert final_data["status"] == "CHECKED_OK"
        assert mock_status.call_count == 3
        assert mock_sleep.call_count == 2


@pytest.mark.asyncio
async def test_wait_for_document_checked_not_ok_raises_cz_document_error():
    """Verify wait_for_document raises CZDocumentError on CHECKED_NOT_OK with extracted message."""
    client = CZClient(inn="7700112233", token="mock-token")
    error_response = {
        "status": "CHECKED_NOT_OK",
        "errors": ["07: Недопустимое количество символов в значении поля \"Код идентификации\"."],
        "commonErrors": [
            {
                "errorCode": "07",
                "errorMessage": "07: Недопустимое количество символов в значении поля \"Код идентификации\".",
                "errorObject": "0104630199251318215BADKIZ",
            }
        ],
    }

    with patch.object(client, "get_document_status", new_callable=AsyncMock) as mock_status:
        mock_status.return_value = error_response

        with pytest.raises(CZDocumentError) as exc_info:
            await client.wait_for_document("doc-bad-kiz", max_attempts=3, interval_seconds=0.01)

        err = exc_info.value
        assert err.status_code == 422
        assert "07: Недопустимое количество символов" in str(err)
        assert err.doc_id == "doc-bad-kiz"
        assert len(err.errors) == 1
        assert len(err.common_errors) == 1


@pytest.mark.asyncio
async def test_wait_for_document_timeout_raises_timeout_error():
    """Verify wait_for_document raises TimeoutError when max_attempts is exceeded."""
    client = CZClient(inn="7700112233", token="mock-token")

    with patch.object(client, "get_document_status", new_callable=AsyncMock) as mock_status, \
         patch("asyncio.sleep", new_callable=AsyncMock):
        mock_status.return_value = {"status": "IN_PROGRESS"}

        with pytest.raises(TimeoutError) as exc_info:
            await client.wait_for_document("doc-timeout", max_attempts=3, interval_seconds=0.01)

        assert "Превышен таймаут" in str(exc_info.value)


@pytest.mark.asyncio
async def test_wait_for_document_no_404_masking_and_grace_period():
    """Verify 404 is not masked to CHECKED_OK, and raises CZAPIError if persistent."""
    client = CZClient(inn="7700112233", token="mock-token")

    # Case A: 404 on attempt 0 and 1, then CHECKED_OK on attempt 2 -> succeeds via grace period
    with patch.object(client, "get_document_status", new_callable=AsyncMock) as mock_status, \
         patch("asyncio.sleep", new_callable=AsyncMock):
        mock_status.side_effect = [
            CZAPIError("Not found", 404),
            CZAPIError("Not found", 404),
            {"status": "CHECKED_OK", "number": "doc-grace-ok"},
        ]
        res = await client.wait_for_document("doc-grace-ok", max_attempts=5, interval_seconds=0.01)
        assert res["status"] == "CHECKED_OK"

    # Case B: Persistent 404 raises CZAPIError (previously masked to CHECKED_OK!)
    with patch.object(client, "get_document_status", new_callable=AsyncMock) as mock_status, \
         patch("asyncio.sleep", new_callable=AsyncMock):
        mock_status.side_effect = CZAPIError("Endpoint Not Found", 404)

        with pytest.raises(CZAPIError) as exc_info:
            await client.wait_for_document("doc-missing-404", max_attempts=5, interval_seconds=0.01)

        assert exc_info.value.status_code == 404


# ==============================================================================
# 3. Celery Agent (cz_withdrawal.py) Status Handling & Notification Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_celery_withdraw_order_kiz_checked_ok():
    """Verify Celery task handles CHECKED_OK: WITHDRAWN, RETIRED, SUCCESS, Telegram notify."""
    await init_db()

    seller_id = str(uuid.uuid4())
    order_id = int(str(uuid.uuid4().int)[:9])
    test_kiz = "0104630199251318215QTSRH>4sVc+."

    with Session(sync_engine) as db:
        seller = Seller(
            id=seller_id,
            name="Celery Success Seller",
            wb_api_token_encrypted=encrypt("wb_tok"),
            cz_token_encrypted=encrypt("cz_tok"),
            cz_inn="7700112233",
            telegram_bot_token_encrypted=encrypt("bot_tok"),
            telegram_chat_ids=["123456789"],
            is_active=True,
        )
        db.add(seller)
        order = Order(
            id=order_id,
            seller_id=seller_id,
            name="Платье летнее",
            article="dress.01",
            price=2500,
            status=OrderStatus.DELIVERED,
            kiz_required=True,
            kiz_code=test_kiz,
            kiz_status=KizStatus.ATTACHED,
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db.add(order)
        db.commit()

    with patch("app.agents.cz_withdrawal._do_withdrawal", new_callable=AsyncMock) as mock_do_w, \
         patch("app.agents.notifier.send_cz_status_notification.delay") as mock_notify:
        mock_do_w.return_value = "doc-celery-success-uuid"

        withdraw_order_kiz(
            seller_id=seller_id,
            order_id=order_id,
            kiz_code=test_kiz,
            price_kopecks=250000,
            receipt_number="REC-1001",
        )

        mock_notify.assert_called_once_with(
            seller_id=seller_id,
            order_id=order_id,
            success=True,
            doc_id="doc-celery-success-uuid",
        )

    with Session(sync_engine) as db:
        o = db.query(Order).filter(Order.id == order_id).first()
        assert o.kiz_status == KizStatus.WITHDRAWN
        assert o.kiz_cz_status == "RETIRED"
        assert o.cz_withdrawal_doc_id == "doc-celery-success-uuid"

        op = db.query(KizOperation).filter(
            KizOperation.seller_id == seller_id,
            KizOperation.order_id == order_id,
            KizOperation.operation == KizOperationType.WITHDRAWAL,
        ).first()
        assert op is not None
        assert op.status == "SUCCESS"
        assert op.cz_doc_id == "doc-celery-success-uuid"
        assert op.cz_doc_status == "CHECKED_OK"


@pytest.mark.asyncio
async def test_celery_withdraw_order_kiz_checked_not_ok_no_futile_retries():
    """Verify Celery task handles CZDocumentError: ERROR, CHECKED_NOT_OK, FAILED, immediate alert, no retry."""
    await init_db()

    seller_id = str(uuid.uuid4())
    order_id = int(str(uuid.uuid4().int)[:9])
    test_kiz = "0104630199251318215QTSRH>4sVc+."
    rejection_msg = "07: Недопустимое количество символов в значении поля \"Код идентификации\"."

    with Session(sync_engine) as db:
        seller = Seller(
            id=seller_id,
            name="Celery Reject Seller",
            wb_api_token_encrypted=encrypt("wb_tok"),
            cz_token_encrypted=encrypt("cz_tok"),
            cz_inn="7700112233",
            telegram_bot_token_encrypted=encrypt("bot_tok"),
            telegram_chat_ids=["123456789"],
            is_active=True,
        )
        db.add(seller)
        order = Order(
            id=order_id,
            seller_id=seller_id,
            name="Брюки спортивные",
            article="pants.01",
            price=3000,
            status=OrderStatus.DELIVERED,
            kiz_required=True,
            kiz_code=test_kiz,
            kiz_status=KizStatus.ATTACHED,
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db.add(order)
        db.commit()

    doc_err = CZDocumentError(
        message=f"Документ doc-celery-rejected отклонен ГИС МТ: {rejection_msg}",
        status_code=422,
        doc_id="doc-celery-rejected",
    )

    with patch("app.agents.cz_withdrawal._do_withdrawal", new_callable=AsyncMock) as mock_do_w, \
         patch("app.agents.notifier.send_cz_status_notification.delay") as mock_notify, \
         patch.object(withdraw_order_kiz, "retry") as mock_retry:
        mock_do_w.side_effect = doc_err

        withdraw_order_kiz(
            seller_id=seller_id,
            order_id=order_id,
            kiz_code=test_kiz,
            price_kopecks=300000,
        )

        # Futile retry must NOT be triggered on deterministic CZDocumentError
        mock_retry.assert_not_called()

        # Immediate failure notification with exact GIS MT reason
        mock_notify.assert_called_once()
        k_args = mock_notify.call_args.kwargs
        assert k_args["seller_id"] == seller_id
        assert k_args["order_id"] == order_id
        assert k_args["success"] is False
        assert rejection_msg in k_args["error"]
        assert k_args["doc_id"] == "doc-celery-rejected"

    with Session(sync_engine) as db:
        o = db.query(Order).filter(Order.id == order_id).first()
        assert o.kiz_status == KizStatus.ERROR
        assert o.kiz_cz_status == "CHECKED_NOT_OK"
        assert o.cz_withdrawal_doc_id == "doc-celery-rejected"

        op = db.query(KizOperation).filter(
            KizOperation.seller_id == seller_id,
            KizOperation.order_id == order_id,
            KizOperation.operation == KizOperationType.WITHDRAWAL,
        ).first()
        assert op is not None
        assert op.status == "FAILED"
        assert op.cz_doc_id == "doc-celery-rejected"
        assert op.cz_doc_status == "CHECKED_NOT_OK"
        assert rejection_msg in op.error_message

        # AuditLog entry verification
        audit = db.query(AuditLog).filter(
            AuditLog.seller_id == seller_id,
            AuditLog.entity_id == str(order_id),
            AuditLog.action == "FAILED",
        ).first()
        assert audit is not None
        assert rejection_msg in audit.error


# ==============================================================================
# 4. Web Signing (documents.py) Rejection & Status Polling Endpoint Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_web_signing_submit_signed_document_rejection():
    """Verify submit_signed_kiz_document handles GIS MT rejection correctly."""
    await init_db()

    async with AsyncSessionLocal() as session:
        admin_user = await ensure_initial_admin(session)
        auth_token = create_access_token(
            data={"sub": admin_user.id, "username": admin_user.username, "role": "admin", "is_superuser": True}
        )
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Web Signing Reject Seller",
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
            name="Юбка плиссированная",
            article="skirt.01",
            price=1800,
            status=OrderStatus.DELIVERING,
            kiz_required=True,
            kiz_code="0104630199251318215BADKIZ",
            kiz_status=KizStatus.ATTACHED,
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        session.add(order)
        await session.commit()

    rejection_msg = "07: Недопустимое количество символов в значении поля \"Код идентификации\"."
    doc_err = CZDocumentError(
        message=f"Документ doc-web-reject отклонен ГИС МТ: {rejection_msg}",
        status_code=422,
        doc_id="doc-web-reject",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {auth_token}"}) as ac:
        with patch("app.services.cz_client.CZClient.submit_signed_document", new_callable=AsyncMock) as mock_submit, \
             patch("app.services.cz_client.CZClient.wait_for_document", new_callable=AsyncMock) as mock_wait, \
             patch("app.agents.notifier.send_cz_status_notification.delay") as mock_notify:
            mock_submit.return_value = "doc-web-reject"
            mock_wait.side_effect = doc_err

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
            assert res_data["success"] is False
            assert res_data["status"] == "CHECKED_NOT_OK"
            assert res_data["doc_id"] == "doc-web-reject"
            assert rejection_msg in res_data["error"]

            # Telegram failure notification sent
            mock_notify.assert_called_once_with(
                seller_id=seller_id,
                order_id=order_id,
                success=False,
                error=doc_err.message,
                doc_id="doc-web-reject",
            )

    async with AsyncSessionLocal() as session:
        o = await session.get(Order, order_id)
        assert o.kiz_status == KizStatus.ERROR
        assert o.kiz_cz_status == "CHECKED_NOT_OK"
        assert o.cz_withdrawal_doc_id == "doc-web-reject"

        res_op = await session.execute(
            select(KizOperation).where(
                KizOperation.seller_id == seller_id,
                KizOperation.order_id == order_id,
            )
        )
        op = res_op.scalars().first()
        assert op.status == "FAILED"
        assert op.cz_doc_status == "CHECKED_NOT_OK"
        assert rejection_msg in op.error_message


@pytest.mark.asyncio
async def test_get_cz_document_status_endpoint_v4_info():
    """Verify GET /api/v1/sellers/{id}/kiz/documents/{doc_id}/status queries True API v4 and returns error_text."""
    await init_db()

    async with AsyncSessionLocal() as session:
        admin_user = await ensure_initial_admin(session)
        auth_token = create_access_token(
            data={"sub": admin_user.id, "username": admin_user.username, "role": "admin", "is_superuser": True}
        )
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Status Check Seller",
            wb_api_token_encrypted=encrypt("wb_tok"),
            cz_token_encrypted=encrypt("cz_tok"),
            cz_inn="7700112233",
            is_active=True,
        )
        session.add(seller)
        await session.commit()

    v4_mock_response = {
        "number": "doc-v4-info-check",
        "status": "CHECKED_NOT_OK",
        "errors": [
            "07: Недопустимое количество символов в значении поля \"Код идентификации\"."
        ],
        "commonErrors": [
            {
                "errorCode": "07",
                "errorMessage": "07: Недопустимое количество символов в значении поля \"Код идентификации\".",
                "errorObject": "0104630199251318215BADKIZ",
            }
        ],
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {auth_token}"}) as ac:
        with patch("app.services.cz_client.CZClient.get_document_info", new_callable=AsyncMock) as mock_info:
            mock_info.return_value = v4_mock_response

            res = await ac.get(f"/api/v1/sellers/{seller_id}/kiz/documents/doc-v4-info-check/status")
            assert res.status_code == 200, res.text
            data = res.json()
            assert data["success"] is True
            assert data["doc_id"] == "doc-v4-info-check"
            assert data["status"] == "CHECKED_NOT_OK"
            assert len(data["errors"]) == 1
            assert len(data["common_errors"]) == 1
            assert "07: Недопустимое количество символов" in data["error_text"]
            mock_info.assert_called_once_with("doc-v4-info-check", pg="lp")