"""
Signature Batch Submitter Service.
Handles submission of signed documents (LK_RECEIPT and LP_RETURN)
to GIS MT (True API), polls document processing status, updates order
and operation records, and sends Telegram notifications.
"""
import copy
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.seller import Seller
from app.models.order import Order, KizStatus, OrderStatus
from app.models.kiz import KizSignatureBatch, BatchStatus, KizOperation, KizOperationType
from app.services.cz_client import CZClient, CZDocumentError, CZUnauthorizedError
from app.services.kiz_service import sync_kiz_status_record
from app.services.encryption import decrypt

logger = logging.getLogger(__name__)


async def execute_signed_batch_submission(
    seller: Seller,
    batch: KizSignatureBatch,
    payload: Dict[str, Any],
    db: AsyncSession,
) -> Dict[str, Any]:
    """
    Отправляет подписанные браузером или сервером документы в ГИС МТ (Честный Знак).
    """
    signed_docs = payload.get("signed_documents", [])
    sign_mode = payload.get("sign_mode", "client_cades")
    cert_subject = payload.get("cert_subject")

    cz_token = decrypt(seller.cz_token_encrypted) if seller.cz_token_encrypted else ""
    client = CZClient(inn=seller.cz_inn or "", token=cz_token)

    now = datetime.now(timezone.utc)
    successful_submissions = 0
    failed_submissions = 0
    results = []

    if sign_mode == "client_cades":
        for item in signed_docs:
            doc_type = item.get("type", "LK_RECEIPT")
            doc_b64 = item.get("document_base64")
            sig_b64 = item.get("signature_base64")
            kiz_code = item.get("kiz_code")
            order_id = item.get("order_id")
            action = item.get("action", "WITHDRAWAL")

            if not doc_b64 or not sig_b64:
                continue

            try:
                doc_id = await client.submit_signed_document(
                    document_type=doc_type,
                    document_base64=doc_b64,
                    signature_base64=sig_b64,
                    wait_for_result=False,
                )
            except CZUnauthorizedError as ue:
                logger.warning(f"CZ session token expired during batch submission for {kiz_code}: {ue}")
                raise ue
            except Exception as e:
                err_msg = str(e).lower()
                if "401" in err_msg or "token expired" in err_msg or "token is expired" in err_msg:
                    logger.warning(f"CZ auth error during batch submission for {kiz_code}: {e}")
                    raise CZUnauthorizedError(str(e))
                failed_submissions += 1
                results.append({"kiz_code": kiz_code, "order_id": order_id, "error": str(e), "status": "FAILED"})
                logger.error(f"Error submitting batch signed doc for {kiz_code}: {e}")
                continue

            is_confirmed = False
            doc_status = "SUBMITTED"
            error_reason: Optional[str] = None

            try:
                status_data = await client.wait_for_document(doc_id, max_attempts=12, interval_seconds=1.5)
                doc_status = str(status_data.get("status") or "CHECKED_OK")
                is_confirmed = True
            except CZDocumentError as doc_err:
                doc_status = "CHECKED_NOT_OK"
                error_reason = getattr(doc_err, "message", str(doc_err)) or "Документ отклонен ГИС МТ"
                is_confirmed = False
            except (TimeoutError, Exception) as poll_err:
                logger.warning(f"Polling document {doc_id} ended with: {poll_err}")
                doc_status = "IN_PROGRESS"
                is_confirmed = None

            ord_obj = await db.get(Order, order_id) if order_id else None

            if is_confirmed is True:
                successful_submissions += 1
                results.append({"kiz_code": kiz_code, "order_id": order_id, "doc_id": doc_id, "status": "SUCCESS", "cz_status": doc_status})

                target_cz_status = "RETIRED" if action == "WITHDRAWAL" else "INTRODUCED"
                if ord_obj and str(ord_obj.seller_id) == str(seller.id):
                    if action == "WITHDRAWAL":
                        ord_obj.kiz_status = KizStatus.WITHDRAWN
                        ord_obj.kiz_cz_status = target_cz_status
                        ord_obj.status = OrderStatus.DELIVERED
                        ord_obj.cz_withdrawal_doc_id = doc_id
                    elif action == "RETURN":
                        ord_obj.kiz_status = KizStatus.RETURNED
                        ord_obj.kiz_cz_status = target_cz_status
                        ord_obj.status = OrderStatus.CANCELLED
                        ord_obj.cz_return_doc_id = doc_id

                    ord_obj.cz_doc_status = doc_status
                    ord_obj.cz_rejection_reason = None
                    ord_obj.kiz_cz_status_updated_at = now
                    ord_obj.updated_at = now

                    kiz_op = KizOperation(
                        seller_id=str(seller.id),
                        order_id=ord_obj.id,
                        kiz_code=ord_obj.kiz_code or kiz_code or "",
                        operation=KizOperationType.WITHDRAWAL if action == "WITHDRAWAL" else KizOperationType.RETURN,
                        status="SUCCESS",
                        cz_doc_id=doc_id,
                        cz_doc_status=doc_status,
                    )
                    db.add(kiz_op)

                # Всегда синхронизируем единый реестр kiz_product_info (даже если ord_obj отсутствует)
                effective_kiz = (ord_obj.kiz_code if ord_obj and ord_obj.kiz_code else kiz_code)
                if effective_kiz:
                    await sync_kiz_status_record(
                        db=db,
                        kiz_code=effective_kiz,
                        cz_status=target_cz_status,
                        seller_id=str(seller.id),
                        doc_id=doc_id if action == "WITHDRAWAL" else None,
                        target_order_id=ord_obj.id if ord_obj else None,
                    )

            elif is_confirmed is False:
                failed_submissions += 1
                results.append({"kiz_code": kiz_code, "order_id": order_id, "doc_id": doc_id, "error": error_reason, "status": "FAILED", "cz_status": doc_status})

                if ord_obj and str(ord_obj.seller_id) == str(seller.id):
                    ord_obj.kiz_status = KizStatus.ERROR
                    ord_obj.kiz_cz_status = "CHECKED_NOT_OK"
                    ord_obj.cz_doc_status = "CHECKED_NOT_OK"
                    ord_obj.cz_rejection_reason = error_reason
                    if action == "WITHDRAWAL":
                        ord_obj.cz_withdrawal_doc_id = doc_id
                    elif action == "RETURN":
                        ord_obj.cz_return_doc_id = doc_id
                    ord_obj.kiz_cz_status_updated_at = now
                    ord_obj.updated_at = now

                    kiz_op = KizOperation(
                        seller_id=str(seller.id),
                        order_id=ord_obj.id,
                        kiz_code=ord_obj.kiz_code or kiz_code or "",
                        operation=KizOperationType.WITHDRAWAL if action == "WITHDRAWAL" else KizOperationType.RETURN,
                        status="FAILED",
                        cz_doc_id=doc_id,
                        cz_doc_status="CHECKED_NOT_OK",
                        error_message=error_reason,
                    )
                    db.add(kiz_op)

                effective_kiz = (ord_obj.kiz_code if ord_obj and ord_obj.kiz_code else kiz_code)
                if effective_kiz:
                    # Если ГИС МТ отклонил возврат с ошибкой 14 (недопустимый статус), код уже в обороте!
                    if action == "RETURN" and ("недопустимый статус" in (error_reason or "").lower() or "14:" in (error_reason or "")):
                        await sync_kiz_status_record(
                            db=db,
                            kiz_code=effective_kiz,
                            cz_status="INTRODUCED",
                            seller_id=str(seller.id),
                            validation_message="Код уже находится в обороте в ГИС МТ",
                            target_order_id=ord_obj.id if ord_obj else None,
                        )
                    else:
                        await sync_kiz_status_record(
                            db=db,
                            kiz_code=effective_kiz,
                            cz_status=None,
                            seller_id=str(seller.id),
                            validation_message=error_reason,
                            target_order_id=ord_obj.id if ord_obj else None,
                        )

            else:
                successful_submissions += 1
                results.append({"kiz_code": kiz_code, "order_id": order_id, "doc_id": doc_id, "status": "IN_PROGRESS", "cz_status": doc_status})

                if ord_obj and str(ord_obj.seller_id) == str(seller.id):
                    if action == "WITHDRAWAL":
                        ord_obj.kiz_status = KizStatus.WITHDRAWN
                        ord_obj.kiz_cz_status = "RETIRED"
                        ord_obj.cz_withdrawal_doc_id = doc_id
                    elif action == "RETURN":
                        ord_obj.kiz_status = KizStatus.RETURNED
                        ord_obj.kiz_cz_status = "INTRODUCED"
                        ord_obj.cz_return_doc_id = doc_id
                    ord_obj.cz_doc_status = "IN_PROGRESS"
                    ord_obj.updated_at = now

                effective_kiz = (ord_obj.kiz_code if ord_obj and ord_obj.kiz_code else kiz_code)
                if effective_kiz:
                    await sync_kiz_status_record(
                        db=db,
                        kiz_code=effective_kiz,
                        cz_status="RETIRED" if action == "WITHDRAWAL" else "INTRODUCED",
                        seller_id=str(seller.id),
                        doc_id=doc_id if action == "WITHDRAWAL" else None,
                        target_order_id=ord_obj.id if ord_obj else None,
                    )

    elif sign_mode == "server":
        from app.agents.cz_withdrawal import withdraw_order_kiz
        from app.agents.cz_return import return_order_kiz

        withdrawals = (batch.data_payload or {}).get("withdrawals", [])
        returns = (batch.data_payload or {}).get("returns", [])

        for w in withdrawals:
            kiz = w.get("kiz_code")
            oid = w.get("order_id")
            if kiz and w.get("needs_withdrawal", True):
                if w.get("is_seller_owner") is False:
                    continue
                owner_inn = (w.get("cz_owner_inn") or "").strip()
                if owner_inn and seller.cz_inn and owner_inn != seller.cz_inn.strip():
                    continue
                withdraw_order_kiz.delay(
                    seller_id=str(seller.id),
                    order_id=oid,
                    kiz_code=kiz,
                    receipt_number=w.get("receipt_number"),
                    receipt_date=w.get("receipt_date"),
                    document_type="RECEIPT" if w.get("receipt_number") else "OTHER",
                )
                successful_submissions += 1

        for r in returns:
            kiz = r.get("kiz_code")
            oid = r.get("order_id")
            if kiz and r.get("needs_cz_return", False):
                if r.get("is_seller_owner") is False:
                    continue
                owner_inn = (r.get("cz_owner_inn") or "").strip()
                if owner_inn and seller.cz_inn and owner_inn != seller.cz_inn.strip():
                    continue
                return_order_kiz.delay(
                    seller_id=str(seller.id),
                    order_id=oid,
                    kiz_code=kiz,
                )
                successful_submissions += 1

    batch.signed_at = now
    batch.signed_by = cert_subject or "Владелец ЭЦП"

    # Update data_payload items with terminal statuses
    dp = copy.deepcopy(batch.data_payload or {})
    w_list = dp.get("withdrawals", [])
    r_list = dp.get("returns", [])
    for res_item in results:
        r_kiz = res_item.get("kiz_code")
        r_st = res_item.get("status")
        if r_st in ("SUCCESS", "IN_PROGRESS"):
            for w in w_list:
                if w.get("kiz_code") == r_kiz:
                    w["needs_withdrawal"] = False
                    w["is_already_withdrawn"] = True
                    w["cz_status"] = "RETIRED"
                    w["cz_status_desc"] = "Выбыл (выведен из оборота)"
                    w["selected"] = False
            for r in r_list:
                if r.get("kiz_code") == r_kiz:
                    r["needs_cz_return"] = False
                    r["is_already_in_circulation"] = True
                    r["cz_status"] = "INTRODUCED"
                    r["cz_status_desc"] = "В обороте"
                    r["selected"] = False
    batch.data_payload = dp
    flag_modified(batch, "data_payload")

    # Merge results with previous submissions (supports partial retry)
    existing_results = (batch.submission_results or {}).get("results", [])
    merged_results = {r.get("kiz_code"): r for r in existing_results if r.get("kiz_code")}
    for r in results:
        if r.get("kiz_code"):
            merged_results[r.get("kiz_code")] = r

    if merged_results:
        final_results = list(merged_results.values())
        total_failed = sum(1 for r in final_results if r.get("status") == "FAILED")
        total_successful = sum(1 for r in final_results if r.get("status") in ("SUCCESS", "IN_PROGRESS"))
        batch.submission_results = {
            "results": final_results,
            "successful": total_successful,
            "failed": total_failed,
        }
        if total_failed == 0:
            batch.status = BatchStatus.COMPLETED
        elif total_successful > 0:
            batch.status = BatchStatus.PARTIALLY_COMPLETED
        else:
            batch.status = BatchStatus.FAILED
    else:
        batch.submission_results = {"results": results, "successful": successful_submissions, "failed": failed_submissions}
        if failed_submissions == 0:
            batch.status = BatchStatus.COMPLETED
        elif successful_submissions > 0:
            batch.status = BatchStatus.PARTIALLY_COMPLETED
        else:
            batch.status = BatchStatus.FAILED

    await db.commit()

    # Telegram notification
    if seller.telegram_bot_token_encrypted:
        try:
            from app.services.telegram_service import TelegramService, get_personal_manager_chats
            target_chats = get_personal_manager_chats(seller)
            if target_chats:
                bot_token = decrypt(seller.telegram_bot_token_encrypted)
                tg = TelegramService(bot_token)
                status_emoji = "✅" if failed_submissions == 0 else ("⚠️" if successful_submissions > 0 else "❌")
                tg_text = (
                    f"{status_emoji} <b>Пакет отчёта №<code>{batch.id[:8]}</code> обработан!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🏪 <b>Магазин:</b> {seller.name}\n"
                    f"👤 <b>Подписал:</b> {batch.signed_by}\n"
                )
                if successful_submissions > 0:
                    tg_text += f"✅ <b>Успешно подтверждено ГИС МТ:</b> {successful_submissions} документов\n"
                if failed_submissions > 0:
                    tg_text += f"❌ <b>Отклонено ГИС МТ:</b> {failed_submissions} документов\n"
                    failed_items = [r for r in results if r.get("status") == "FAILED"]
                    for fi in failed_items[:3]:
                        f_kiz = fi.get('kiz_code') or 'Без КИЗ'
                        f_err = fi.get('error') or 'Ошибка валидации ГИС МТ'
                        tg_text += f"• <code>{f_kiz}</code>: {f_err}\n"
                    if len(failed_items) > 3:
                        tg_text += f"• ... и ещё {len(failed_items) - 3} ошибок\n"

                await tg.send_text(target_chats, tg_text.strip())
                await tg.close()
        except Exception as tg_err:
            logger.error(f"Failed to send telegram confirmation for batch {batch.id}: {tg_err}")

    return {
        "success": True,
        "batch_id": batch.id,
        "status": batch.status.value,
        "successful_submissions": successful_submissions,
        "failed_submissions": failed_submissions,
    }
