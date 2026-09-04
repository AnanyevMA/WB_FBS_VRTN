import pytest
import uuid
import json
import asyncio
import httpx
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

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
# Suite 1: Stress-testing extract_document_error_text
# ==============================================================================

def test_extract_error_corrupted_and_non_dict_inputs():
    """Verify extract_document_error_text handles non-dict and malformed root inputs safely."""
    # None input
    assert CZClient.extract_document_error_text(None) == "Неизвестная ошибка ГИС МТ"

    # String input
    assert CZClient.extract_document_error_text("Bad Gateway") == "Неизвестная ошибка ГИС МТ"

    # List input
    assert CZClient.extract_document_error_text(["error 1", "error 2"]) == "Неизвестная ошибка ГИС МТ"

    # Number input
    assert CZClient.extract_document_error_text(500) == "Неизвестная ошибка ГИС МТ"

    # Empty dict
    assert CZClient.extract_document_error_text({}) == "Неизвестная ошибка ГИС МТ"

    # Dict with only status
    assert CZClient.extract_document_error_text({"status": "REJECTED"}) == "Отказ ГИС МТ со статусом REJECTED"
    assert CZClient.extract_document_error_text({"status": "CHECKED_NOT_OK"}) == "Отказ ГИС МТ со статусом CHECKED_NOT_OK"


def test_extract_error_deeply_nested_and_heterogeneous_errors_field():
    """Stress-test extraction from errors array with nested, missing, and unexpected types."""
    payload = {
        "status": "CHECKED_NOT_OK",
        "errors": [
            None,
            "",
            "   ",
            12345,  # ignored non-str/dict
            True,   # ignored boolean
            {"errorCode": "07", "errorMessage": "07: Код маркировки не найден"},  # code already in msg
            {"errorCode": "14", "errorMessage": "Некорректный статус КИ"},        # code not in msg -> "14: Некорректный статус КИ"
            {"code": 99, "message": "Срок действия кода истек"},                 # alternate keys "code" and "message", int code
            {"text": "Простая текстовая ошибка"},                                 # "text" key
            {"error": "Ошибка валидации подписи"},                                # "error" key
            {"errorCode": "E500"},                                                # code only
            {"errorMessage": "Только сообщение без кода"},                        # msg only
            {"nested": {"unrecognized": "structure"}},                            # unrecognized dict keys
            "Строка ошибки напрямую в массиве",
        ],
    }
    extracted = CZClient.extract_document_error_text(payload)

    # Assertions on format and contents
    assert "07: Код маркировки не найден" in extracted
    assert "14: Некорректный статус КИ" in extracted
    assert "99: Срок действия кода истек" in extracted
    assert "Простая текстовая ошибка" in extracted
    assert "Ошибка валидации подписи" in extracted
    assert "E500" in extracted
    assert "Только сообщение без кода" in extracted
    assert "Строка ошибки напрямую в массиве" in extracted
    assert "07: 07:" not in extracted  # Ensure no double-prefixed error code


def test_extract_error_complex_common_errors_structures():
    """Stress-test commonErrors with missing keys, complex errorObjects, and object deduplication."""
    payload = {
        "status": "FAILED",
        "commonErrors": [
            None,
            "",
            # Case 1: errorCode already prefixed in errorMessage
            {
                "errorCode": "07",
                "errorMessage": "07: Недопустимое количество символов в значении поля 'Код идентификации'",
                "errorObject": "0104630199251318215QTSRh>4sVc+.",
            },
            # Case 2: errorCode not prefixed in errorMessage
            {
                "errorCode": "12",
                "errorMessage": "Код маркировки не принадлежит участнику оборота",
                "errorObject": "0104630199251318215ABC12345678",
            },
            # Case 3: errorObject already included in errorMessage (should not duplicate object suffix)
            {
                "errorCode": "15",
                "errorMessage": "Товар с КИ 0104630199251318215EMBEDDED заблокирован",
                "errorObject": "0104630199251318215EMBEDDED",
            },
            # Case 4: errorObject as a dictionary
            {
                "errorCode": "90",
                "errorMessage": "Ошибка структуры реквизитов",
                "errorObject": {"field": "document_date", "reason": "in_future"},
            },
            # Case 5: only errorObject present (no code, no msg)
            {
                "errorObject": "0104630199251318215ONLYOBJECT",
            },
            # Case 6: plain string in commonErrors array
            "Неожиданная строка ошибки в commonErrors",
        ],
    }
    extracted = CZClient.extract_document_error_text(payload)

    assert "07: Недопустимое количество символов" in extracted
    assert "(объект: 0104630199251318215QTSRh>4sVc+.)" in extracted
    assert "12: Код маркировки не принадлежит участнику оборота (объект: 0104630199251318215ABC12345678)" in extracted
    assert "Товар с КИ 0104630199251318215EMBEDDED заблокирован" in extracted
    assert "(объект: 0104630199251318215EMBEDDED)" not in extracted  # Deduplicated from suffix!
    assert "90: Ошибка структуры реквизитов" in extracted
    assert "(объект: 0104630199251318215ONLYOBJECT)" in extracted
    assert "Неожиданная строка ошибки в commonErrors" in extracted


def test_extract_error_unicode_emojis_and_control_chars():
    """Verify unicode characters, control symbols, and non-ASCII text don't corrupt extraction."""
    payload = {
        "status": "CHECKED_NOT_OK",
        "errors": [
            "⚠️ Ошибка валидации: обнаружен неразрывный пробел\u00a0и спецсимвол GS \x1d внутри строки \x1d",
            "Тест на греческий алфавит: α, β, γ и эмодзи: 🏷️ ❌ 📦",
        ],
        "commonErrors": [
            {
                "errorCode": "ERR_ЮНИКОД",
                "errorMessage": "Символы перевода строк \n и табуляции \t обработаны корректно",
                "errorObject": "КМ_№12345_ПРОДУКТ",
            }
        ],
    }
    extracted = CZClient.extract_document_error_text(payload)

    assert "⚠️ Ошибка валидации" in extracted
    assert "\x1d" in extracted
    assert "α, β, γ" in extracted
    assert "🏷️ ❌ 📦" in extracted
    assert "ERR_ЮНИКОД: Символы перевода строк" in extracted
    assert "(объект: КМ_№12345_ПРОДУКТ)" in extracted


def test_extract_error_deduplication_and_order_preservation():
    """Verify deduplication works across identical errors in errors and commonErrors without losing order."""
    payload = {
        "status": "CHECKED_NOT_OK",
        "errors": [
            "07: Недопустимое количество символов",
            "12: Код не принадлежит участнику",
            "07: Недопустимое количество символов",  # duplicate in errors
        ],
        "commonErrors": [
            {
                "errorCode": "07",
                "errorMessage": "07: Недопустимое количество символов",  # duplicate across both
            },
            {
                "errorCode": "99",
                "errorMessage": "Новая ошибка 99",
            },
        ],
    }
    extracted = CZClient.extract_document_error_text(payload)
    items = extracted.split("; ")

    assert len(items) == 3
    assert items[0] == "07: Недопустимое количество символов"
    assert items[1] == "12: Код не принадлежит участнику"
    assert items[2] == "99: Новая ошибка 99"


def test_extract_error_fallback_hierarchy():
    """Verify fallback sequence: statusComment -> comment -> error -> errorMessage -> message -> default status."""
    # 1. statusComment
    assert CZClient.extract_document_error_text({"status": "FAILED", "statusComment": "Отклонено шлюзом"}) == "Отклонено шлюзом"

    # 2. comment (when statusComment missing)
    assert CZClient.extract_document_error_text({"status": "FAILED", "comment": "Ручная отмена"}) == "Ручная отмена"

    # 3. error string
    assert CZClient.extract_document_error_text({"status": "FAILED", "error": "Signature invalid"}) == "Signature invalid"

    # 4. errorMessage string
    assert CZClient.extract_document_error_text({"status": "FAILED", "errorMessage": "Token expired"}) == "Token expired"

    # 5. message string
    assert CZClient.extract_document_error_text({"status": "FAILED", "message": "General failure"}) == "General failure"

    # 6. None of the above -> default status string
    assert CZClient.extract_document_error_text({"status": "CANCELLED"}) == "Отказ ГИС МТ со статусом CANCELLED"
    assert CZClient.extract_document_error_text({}) == "Неизвестная ошибка ГИС МТ"


def test_extract_error_raw_strings_and_empty_elements():
    """Verify errors and commonErrors as raw strings or containing empty/blank values."""
    # errors as a plain string
    assert CZClient.extract_document_error_text({"errors": "Прямая ошибка строкой"}) == "Прямая ошибка строкой"

    # commonErrors as a plain string
    assert CZClient.extract_document_error_text({"commonErrors": "Общая ошибка строкой"}) == "Общая ошибка строкой"

    # both as plain strings
    assert CZClient.extract_document_error_text({"errors": "Ошибка 1", "commonErrors": "Ошибка 2"}) == "Ошибка 1; Ошибка 2"

    # lists containing only whitespace or None
    assert CZClient.extract_document_error_text({"status": "FAILED", "errors": ["  ", None, ""]}) == "Отказ ГИС МТ со статусом FAILED"
    assert CZClient.extract_document_error_text({"status": "FAILED", "commonErrors": ["  ", None, {}]}) == "Отказ ГИС МТ со статусом FAILED"


def test_extract_error_high_volume_stress():
    """Verify high-volume error arrays (10,000 items) do not cause O(n^2) degradation or crashes."""
    import time
    large_errors = [f"Error code {i % 50}: Description {i % 10}" for i in range(10000)]
    payload = {"status": "CHECKED_NOT_OK", "errors": large_errors}

    start = time.perf_counter()
    extracted = CZClient.extract_document_error_text(payload)
    elapsed = time.perf_counter() - start

    # 50 unique error codes generated
    items = extracted.split("; ")
    assert len(items) == 50
    assert items[0] == "Error code 0: Description 0"
    assert elapsed < 0.2  # Should complete in well under 200ms


# ==============================================================================
# Suite 2: Stress-testing wait_for_document Polling & Flapping
# ==============================================================================

@pytest.mark.asyncio
async def test_wait_for_document_flapping_404_to_in_progress_to_checked_not_ok():
    """
    Stress-test realistic True API flapping sequence:
    Attempt 0: 404 Not Found (replication latency)
    Attempt 1: 404 Not Found (replication latency)
    Attempt 2: 200 IN_PROGRESS (registered and processing)
    Attempt 3: 200 CHECKED_NOT_OK with rejection payload
    """
    client = CZClient(inn="7700112233", token="mock-token")
    doc_id = "doc-flap-reject-123"

    rejection_payload = {
        "status": "CHECKED_NOT_OK",
        "commonErrors": [
            {
                "errorCode": "07",
                "errorMessage": "07: Недопустимое количество символов в значении поля 'Код идентификации'.",
                "errorObject": "0104630199251318215BADKIZ",
            }
        ],
    }

    with patch.object(client, "get_document_status", new_callable=AsyncMock) as mock_status, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        mock_status.side_effect = [
            CZAPIError("Document not found yet", 404),
            CZAPIError("Document not found yet", 404),
            {"status": "IN_PROGRESS"},
            rejection_payload,
        ]

        with pytest.raises(CZDocumentError) as exc_info:
            await client.wait_for_document(doc_id, max_attempts=6, interval_seconds=0.01)

        err = exc_info.value
        assert err.status_code == 422
        assert err.doc_id == doc_id
        assert "07: Недопустимое количество символов" in str(err)
        assert "0104630199251318215BADKIZ" in str(err)
        assert len(err.common_errors) == 1
        assert mock_status.call_count == 4
        assert mock_sleep.call_count == 3


@pytest.mark.asyncio
async def test_wait_for_document_flapping_404_to_in_progress_to_checked_ok():
    """
    Stress-test True API flapping sequence ending in success:
    Attempt 0: 404
    Attempt 1: 200 IN_PROGRESS
    Attempt 2: 200 WAIT_FOR_CONTINUATION
    Attempt 3: 200 CHECKED_OK
    """
    client = CZClient(inn="7700112233", token="mock-token")
    doc_id = "doc-flap-success-456"

    with patch.object(client, "get_document_status", new_callable=AsyncMock) as mock_status, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        mock_status.side_effect = [
            CZAPIError("Document not found", 404),
            {"status": "IN_PROGRESS"},
            {"status": "WAIT_FOR_CONTINUATION"},
            {"status": "CHECKED_OK", "number": doc_id},
        ]

        res = await client.wait_for_document(doc_id, max_attempts=5, interval_seconds=0.01)

        assert res["status"] == "CHECKED_OK"
        assert res["number"] == doc_id
        assert mock_status.call_count == 4
        assert mock_sleep.call_count == 3


@pytest.mark.asyncio
async def test_wait_for_document_persistent_404_raises_after_grace_period():
    """
    Verify persistent 404 errors beyond the 3-attempt grace period raise CZAPIError(404),
    preventing infinite polling and never falsely returning CHECKED_OK.
    """
    client = CZClient(inn="7700112233", token="mock-token")
    doc_id = "doc-missing-forever"

    with patch.object(client, "get_document_status", new_callable=AsyncMock) as mock_status, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        mock_status.side_effect = CZAPIError("Not Found", 404)

        with pytest.raises(CZAPIError) as exc_info:
            await client.wait_for_document(doc_id, max_attempts=10, interval_seconds=0.01)

        assert exc_info.value.status_code == 404
        # Exactly 4 calls: attempt 0, 1, 2 tolerated by grace period, attempt 3 raises!
        assert mock_status.call_count == 4
        assert mock_sleep.call_count == 3


@pytest.mark.asyncio
async def test_wait_for_document_network_failure_bubbles_immediately():
    """Verify low-level network failures (ConnectTimeout, ReadTimeout) are not masked."""
    client = CZClient(inn="7700112233", token="mock-token")
    doc_id = "doc-net-fail"

    with patch.object(client, "get_document_status", new_callable=AsyncMock) as mock_status:
        mock_status.side_effect = httpx.ConnectTimeout("Connection to true-api timed out")

        with pytest.raises(httpx.ConnectTimeout):
            await client.wait_for_document(doc_id, max_attempts=5, interval_seconds=0.01)

        assert mock_status.call_count == 1


@pytest.mark.asyncio
async def test_wait_for_document_server_error_raises_immediately():
    """Verify 5xx server errors do not consume grace period and fail immediately."""
    client = CZClient(inn="7700112233", token="mock-token")
    doc_id = "doc-server-500"

    with patch.object(client, "get_document_status", new_callable=AsyncMock) as mock_status:
        mock_status.side_effect = CZAPIError("500 Internal Server Error", status_code=500)

        with pytest.raises(CZAPIError) as exc_info:
            await client.wait_for_document(doc_id, max_attempts=5, interval_seconds=0.01)

        assert exc_info.value.status_code == 500
        assert mock_status.call_count == 1


@pytest.mark.asyncio
async def test_wait_for_document_all_rejection_terminal_statuses():
    """Verify all rejection statuses ('CHECKED_NOT_OK', 'PROCESSING_ERROR', 'PARSE_ERROR', 'FAILED', 'CANCELLED') raise CZDocumentError."""
    client = CZClient(inn="7700112233", token="mock-token")
    statuses = ["CHECKED_NOT_OK", "PROCESSING_ERROR", "PARSE_ERROR", "FAILED", "CANCELLED", "checked_not_ok"]

    for st in statuses:
        with patch.object(client, "get_document_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = {
                "status": st,
                "errorMessage": f"Terminal failure with {st}",
            }
            with pytest.raises(CZDocumentError) as exc_info:
                await client.wait_for_document("doc-rej-status", max_attempts=2, interval_seconds=0.01)

            assert exc_info.value.status_code == 422
            assert f"Terminal failure with {st}" in str(exc_info.value)


@pytest.mark.asyncio
async def test_wait_for_document_all_success_terminal_statuses():
    """Verify all success statuses ('CHECKED_OK', 'ACCEPTED', 'SUCCESS', 'COMPLETED', case-insensitive) return data."""
    client = CZClient(inn="7700112233", token="mock-token")
    statuses = ["CHECKED_OK", "ACCEPTED", "SUCCESS", "COMPLETED", "checked_ok", "accepted"]

    for st in statuses:
        with patch.object(client, "get_document_status", new_callable=AsyncMock) as mock_status:
            mock_status.return_value = {
                "status": st,
                "number": "doc-ok",
            }
            res = await client.wait_for_document("doc-ok", max_attempts=2, interval_seconds=0.01)
            assert res["number"] == "doc-ok"


@pytest.mark.asyncio
async def test_wait_for_document_empty_response_handling():
    """Verify get_document_info handling when True API returns empty list or empty dict."""
    client = CZClient(inn="7700112233", token="mock-token")

    # If _request returns an empty list [] -> doc_info gets documentId, status is "" (pending)
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req, \
         patch("asyncio.sleep", new_callable=AsyncMock):
        mock_req.side_effect = [
            [],  # empty list
            {"status": "CHECKED_OK", "number": "doc-empty-handled"},
        ]

        res = await client.wait_for_document("doc-empty-handled", max_attempts=3, interval_seconds=0.01)
        assert res["status"] == "CHECKED_OK"


# ==============================================================================
# Suite 3: End-to-End Stress on Rejection Reason Propagation
# ==============================================================================

@pytest.mark.asyncio
async def test_e2e_celery_task_deterministic_error_preserves_exact_rejection_reason():
    """Verify withdraw_order_kiz propagates exact GIS MT rejection reason to DB, Audit, and Telegram without retry."""
    await init_db()

    seller_id = str(uuid.uuid4())
    order_id = int(str(uuid.uuid4().int)[:9])
    test_kiz = "0104630199251318215QTSRH>4sVc+."
    doc_id = "doc-exact-reason-999"

    exact_rejection = "07: Недопустимое количество символов в значении поля \"Код идентификации\" (объект: 0104630199251318215BAD)"

    with Session(sync_engine) as db:
        seller = Seller(
            id=seller_id,
            name="Stress Rejection Seller",
            wb_api_token_encrypted=encrypt("wb_tok"),
            cz_token_encrypted=encrypt("cz_tok"),
            cz_inn="7700112233",
            telegram_bot_token_encrypted=encrypt("bot_tok"),
            telegram_chat_ids=["987654321"],
            is_active=True,
        )
        db.add(seller)
        order = Order(
            id=order_id,
            seller_id=seller_id,
            name="Платье вечернее",
            article="dress.exact",
            price=5000,
            status=OrderStatus.DELIVERED,
            kiz_required=True,
            kiz_code=test_kiz,
            kiz_status=KizStatus.ATTACHED,
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db.add(order)
        db.commit()

    cz_doc_error = CZDocumentError(
        message=f"Документ {doc_id} отклонен ГИС МТ: {exact_rejection}",
        status_code=422,
        doc_id=doc_id,
        errors=[],
        common_errors=[{"errorCode": "07", "errorMessage": exact_rejection}],
    )

    with patch("app.agents.cz_withdrawal._do_withdrawal", new_callable=AsyncMock) as mock_do_w, \
         patch("app.agents.notifier.send_cz_status_notification.delay") as mock_notify, \
         patch.object(withdraw_order_kiz, "retry") as mock_retry:

        mock_do_w.side_effect = cz_doc_error

        withdraw_order_kiz(
            seller_id=seller_id,
            order_id=order_id,
            kiz_code=test_kiz,
            price_kopecks=500000,
        )

        # 1. Telegram notification dispatched with exact error and success=False
        mock_notify.assert_called_once_with(
            seller_id=seller_id,
            order_id=order_id,
            success=False,
            error=str(cz_doc_error),
            doc_id=doc_id,
        )

        # 2. Celery retry was NOT called
        mock_retry.assert_not_called()

    # 3. Verify Database records
    with Session(sync_engine) as db:
        order_db = db.query(Order).filter(Order.id == order_id).first()
        assert order_db.kiz_status == KizStatus.ERROR
        assert order_db.kiz_cz_status == "CHECKED_NOT_OK"
        assert order_db.cz_withdrawal_doc_id == doc_id

        op_db = db.query(KizOperation).filter(
            KizOperation.seller_id == seller_id,
            KizOperation.order_id == order_id,
        ).first()
        assert op_db.status == "FAILED"
        assert op_db.cz_doc_status == "CHECKED_NOT_OK"
        assert exact_rejection in op_db.error_message
        assert op_db.cz_doc_id == doc_id

        audit_db = db.query(AuditLog).filter(
            AuditLog.seller_id == seller_id,
            AuditLog.action == "FAILED",
        ).first()
        assert audit_db is not None
        assert exact_rejection in audit_db.error


@pytest.mark.asyncio
async def test_e2e_web_signing_rejection_reason_preserved_in_audit_and_response():
    """Verify submit-signed-document endpoint preserves exact GIS MT rejection reason in audit and response."""
    await init_db()

    async with AsyncSessionLocal() as session:
        admin_user = await ensure_initial_admin(session)
        auth_token = create_access_token(
            data={"sub": admin_user.id, "username": admin_user.username, "role": "admin", "is_superuser": True}
        )
        seller_id = str(uuid.uuid4())
        seller = Seller(
            id=seller_id,
            name="Web Adversarial Seller",
            wb_api_token_encrypted=encrypt("wb_tok"),
            cz_token_encrypted=encrypt("cz_tok"),
            cz_inn="7700112233",
            telegram_bot_token_encrypted=encrypt("bot_tok"),
            telegram_chat_ids=["1122334455"],
            is_active=True,
        )
        session.add(seller)

        order_id = int(str(uuid.uuid4().int)[:9])
        order = Order(
            id=order_id,
            seller_id=seller_id,
            name="Пальто шерстяное",
            article="coat.exact",
            price=12000,
            status=OrderStatus.DELIVERING,
            kiz_required=True,
            kiz_code="0104630199251318215BADCOAT",
            kiz_status=KizStatus.ATTACHED,
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        session.add(order)
        await session.commit()

    complex_rejection = "07: Недопустимое количество символов в значении поля 'Код идентификации' (объект: 0104630199251318215BADCOAT)"
    doc_err = CZDocumentError(
        message=f"Документ doc-web-adv-reject отклонен ГИС МТ: {complex_rejection}",
        status_code=422,
        doc_id="doc-web-adv-reject",
        common_errors=[{"errorCode": "07", "errorMessage": complex_rejection}],
    )

    from httpx import ASGITransport, AsyncClient
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {auth_token}"}) as ac:
        with patch("app.services.cz_client.CZClient.submit_signed_document", new_callable=AsyncMock) as mock_submit, \
             patch("app.services.cz_client.CZClient.wait_for_document", new_callable=AsyncMock) as mock_wait, \
             patch("app.agents.notifier.send_cz_status_notification.delay") as mock_notify:
            mock_submit.return_value = "doc-web-adv-reject"
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
            assert res.status_code == 200
            data = res.json()
            assert data["success"] is False
            assert data["status"] == "CHECKED_NOT_OK"
            assert data["doc_id"] == "doc-web-adv-reject"
            assert complex_rejection in data["error"]

            mock_notify.assert_called_once_with(
                seller_id=seller_id,
                order_id=order_id,
                success=False,
                error=doc_err.message,
                doc_id="doc-web-adv-reject",
            )

    async with AsyncSessionLocal() as session:
        o = await session.get(Order, order_id)
        assert o.kiz_status == KizStatus.ERROR
        assert o.kiz_cz_status == "CHECKED_NOT_OK"
        assert o.cz_withdrawal_doc_id == "doc-web-adv-reject"
