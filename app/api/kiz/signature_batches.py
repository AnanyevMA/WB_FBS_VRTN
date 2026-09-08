"""
FastAPI KIZ Signature Batches Queue Endpoints — WB FBS Manager
"""
import copy
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_db
from app.models.seller import Seller
from app.models.order import Order, KizStatus, OrderStatus
from app.models.kiz import KizSignatureBatch, BatchStatus, KizOperation, KizOperationType
from app.services.cz_client import CZClient, CZDocumentError
from app.services.kiz_service import sync_kiz_status_record
from app.services.encryption import decrypt

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/kiz/signature-batches")
async def list_signature_batches(
    seller_id: str,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """
    Возвращает список пакетов операций с маркировкой (загруженных через Telegram или Web).
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    stmt = select(KizSignatureBatch).where(KizSignatureBatch.seller_id == seller_id)
    if status:
        try:
            enum_status = BatchStatus(status.upper())
            stmt = stmt.where(KizSignatureBatch.status == enum_status)
        except ValueError:
            pass

    stmt = stmt.order_by(KizSignatureBatch.created_at.desc()).limit(50)
    res = await db.execute(stmt)
    batches = res.scalars().all()

    return [
        {
            "id": b.id,
            "filename": b.filename,
            "source": b.source,
            "status": b.status.value,
            "sales_count": b.sales_count,
            "returns_count": b.returns_count,
            "already_withdrawn_count": b.already_withdrawn_count,
            "total_count": b.total_count,
            "error_message": b.error_message,
            "signed_at": b.signed_at.isoformat() if b.signed_at else None,
            "signed_by": b.signed_by,
            "created_at": b.created_at.isoformat() if b.created_at else None,
        }
        for b in batches
    ]


@router.get("/kiz/signature-batches/{batch_id}")
async def get_signature_batch(
    seller_id: str,
    batch_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Возвращает подробные данные пакета (списки продаж с чеками и возвратов).
    """
    batch = await db.get(KizSignatureBatch, batch_id)
    if not batch or str(batch.seller_id) != str(seller_id):
        raise HTTPException(status_code=404, detail="Пакет не найден")

    return {
        "id": batch.id,
        "filename": batch.filename,
        "source": batch.source,
        "status": batch.status.value,
        "sales_count": batch.sales_count,
        "returns_count": batch.returns_count,
        "already_withdrawn_count": batch.already_withdrawn_count,
        "total_count": batch.total_count,
        "data_payload": batch.data_payload,
        "submission_results": batch.submission_results,
        "error_message": batch.error_message,
        "signed_at": batch.signed_at.isoformat() if batch.signed_at else None,
        "signed_by": batch.signed_by,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
    }


@router.post("/kiz/signature-batches/{batch_id}/sync-cz")
async def sync_signature_batch_cz(
    seller_id: str,
    batch_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Принудительная живая сверка всех кодов маркировки пакета с True API Честного Знака.
    Обновляет данные в data_payload пакета и актуализирует счетчики.
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    batch = await db.get(KizSignatureBatch, batch_id)
    if not batch or str(batch.seller_id) != str(seller_id):
        raise HTTPException(status_code=404, detail="Пакет не найден")

    payload = copy.deepcopy(batch.data_payload or {})
    withdrawals = payload.get("withdrawals", [])
    returns = payload.get("returns", [])

    all_kiz = []
    for w in withdrawals:
        if w.get("kiz_code"):
            all_kiz.append(w["kiz_code"])
    for r in returns:
        if r.get("kiz_code"):
            all_kiz.append(r["kiz_code"])

    if all_kiz:
        from app.services.kiz_service import batch_verify_and_sync_cises, is_kiz_withdrawn, CZ_STATUS_DESCRIPTIONS, parse_kiz_code
        synced_map = await batch_verify_and_sync_cises(
            seller=seller,
            kiz_codes=all_kiz,
            db=db,
            force_refresh=True
        )

        for w in withdrawals:
            code = w.get("kiz_code")
            parsed = parse_kiz_code(code) if code else {}
            clean = parsed.get("clean_cis")
            kinfo = synced_map.get(code) or (synced_map.get(clean) if clean else None)
            if kinfo:
                withdrawn, w_reason = is_kiz_withdrawn(
                    status=kinfo.cz_status,
                    status_ex=kinfo.cz_status_ex,
                    raw_payload=kinfo.raw_cz_payload or {}
                )
                w["cz_status"] = kinfo.cz_status
                w["cz_status_desc"] = CZ_STATUS_DESCRIPTIONS.get(kinfo.cz_status or "", kinfo.cz_status or "Не проверен")
                w["is_already_withdrawn"] = withdrawn
                w["needs_withdrawal"] = not withdrawn
                w["selected"] = (not withdrawn) and bool(code)

        for r in returns:
            code = r.get("kiz_code")
            parsed = parse_kiz_code(code) if code else {}
            clean = parsed.get("clean_cis")
            kinfo = synced_map.get(code) or (synced_map.get(clean) if clean else None)
            if kinfo:
                withdrawn, w_reason = is_kiz_withdrawn(
                    status=kinfo.cz_status,
                    status_ex=kinfo.cz_status_ex,
                    raw_payload=kinfo.raw_cz_payload or {}
                )
                r["cz_status"] = kinfo.cz_status
                r["db_cz_status"] = kinfo.cz_status
                r["cz_status_desc"] = CZ_STATUS_DESCRIPTIONS.get(kinfo.cz_status or "", kinfo.cz_status or "Не проверен")
                r["needs_cz_return"] = withdrawn
                r["action_recommended"] = "⚠️ Требует возврата в оборот" if withdrawn else "✅ Уже в обороте (готов к привязке)"
                r["selected"] = withdrawn and bool(code)

        sales_needing = sum(1 for w in withdrawals if w.get("needs_withdrawal"))
        sales_withdrawn = sum(1 for w in withdrawals if not w.get("needs_withdrawal"))
        returns_needing = sum(1 for r in returns if r.get("needs_cz_return"))
        returns_in_circ = sum(1 for r in returns if not r.get("needs_cz_return"))

        summary = payload.get("summary", {})
        summary["sales_needing_withdrawal"] = sales_needing
        summary["sales_already_withdrawn"] = sales_withdrawn
        summary["returns_needing_cz_return"] = returns_needing
        summary["returns_already_in_circulation"] = returns_in_circ
        payload["summary"] = summary
        payload["withdrawals"] = withdrawals
        payload["returns"] = returns

        batch.sales_count = sales_needing
        batch.returns_count = returns_needing
        batch.already_withdrawn_count = sales_withdrawn
        batch.data_payload = payload
        flag_modified(batch, "data_payload")
        await db.commit()

    return {
        "success": True,
        "batch_id": batch.id,
        "sales_count": batch.sales_count,
        "returns_count": batch.returns_count,
        "already_withdrawn_count": batch.already_withdrawn_count,
        "data_payload": batch.data_payload,
    }


@router.post("/kiz/signature-batches/{batch_id}/prepare-documents")
async def prepare_batch_documents_for_signing(
    seller_id: str,
    batch_id: str,
    payload: Optional[Dict[str, Any]] = Body(None),
    db: AsyncSession = Depends(get_db)
):
    """
    Формирует неподписанные канонические документы (LK_RECEIPT и LP_RETURN)
    для последующего подписания через КриптоПро ЭЦП Browser Plugin.
    Включает только позиции, действительно требующие выбытия или возврата в оборот.
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    batch = await db.get(KizSignatureBatch, batch_id)
    if not batch or str(batch.seller_id) != str(seller_id):
        raise HTTPException(status_code=404, detail="Пакет не найден")

    from app.services.cz_client import CZClient
    from app.services.encryption import decrypt

    cz_token = decrypt(seller.cz_token_encrypted) if seller.cz_token_encrypted else ""
    client = CZClient(inn=seller.cz_inn or "", token=cz_token)

    data = batch.data_payload or {}
    withdrawals = data.get("withdrawals", [])
    returns = data.get("returns", [])

    selected_kiz_list = payload.get("selected_kiz_codes") if payload else None
    selected_kiz_set = set(selected_kiz_list) if selected_kiz_list is not None else None

    cades_payloads = []

    # 1. Документы вывода из оборота (LK_RECEIPT с номером кассового чека)
    # Формируем только для тех КИЗ, которые еще не выведены (needs_withdrawal == True)
    for w in withdrawals:
        kiz = w.get("kiz_code")
        if not kiz:
            continue

        if selected_kiz_set is not None:
            if kiz not in selected_kiz_set:
                continue
        elif not w.get("needs_withdrawal", True):
            continue

        price_kop = w.get("price_kopecks") or int((w.get("price") or 0) * 100)
        receipt_num = w.get("receipt_number")
        receipt_date = w.get("receipt_date")

        doc = client.build_withdrawal_payload(
            kiz_codes=[kiz],
            price_kopecks=price_kop,
            mod_fias=seller.mod_fias,
            mod_kpp=seller.mod_kpp,
            receipt_number=receipt_num,
            receipt_date=receipt_date,
            document_type="RECEIPT" if receipt_num else "OTHER",
        )
        cades_payloads.append({
            "action": "WITHDRAWAL",
            "kiz_code": kiz,
            "order_id": w.get("order_id"),
            "sticker_id": w.get("sticker_id"),
            "receipt_number": receipt_num,
            "receipt_date": receipt_date,
            "type": doc["type"],
            "inner_json": doc["inner_json"],
            "document_base64": doc["document_base64"],
        })

    # 2. Документы возврата в оборот (LP_RETURN)
    # Формируем только для тех КИЗ, которые требуют ввода в оборот (needs_cz_return == True)
    for r in returns:
        kiz = r.get("kiz_code")
        if not kiz:
            continue

        if selected_kiz_set is not None:
            if kiz not in selected_kiz_set:
                continue
        elif not r.get("needs_cz_return", False):
            continue

        doc = client.build_return_payload(
            kiz_codes=[kiz],
            wb_order_id=r.get("order_id"),
        )
        cades_payloads.append({
            "action": "RETURN",
            "kiz_code": kiz,
            "order_id": r.get("order_id"),
            "sticker_id": r.get("sticker_id"),
            "type": doc["type"],
            "inner_json": doc["inner_json"],
            "document_base64": doc["document_base64"],
        })

    return {
        "success": True,
        "batch_id": batch.id,
        "seller_inn": seller.cz_inn,
        "documents": cades_payloads,
        "total_documents": len(cades_payloads),
    }


@router.post("/kiz/signature-batches/{batch_id}/submit-signed")
async def submit_signed_batch(
    seller_id: str,
    batch_id: str,
    payload: Dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db)
):
    """
    Принимает подписанные браузерным плагином документы или запускает серверную обработку пакета.
    Отправляет документы в ГИС МТ (Честный Знак), обновляет статусы заказов и статус пакета.
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    batch = await db.get(KizSignatureBatch, batch_id)
    if not batch or str(batch.seller_id) != str(seller_id):
        raise HTTPException(status_code=404, detail="Пакет не найден")

    signed_docs = payload.get("signed_documents", [])
    sign_mode = payload.get("sign_mode", "client_cades")  # "client_cades" or "server"
    cert_subject = payload.get("cert_subject")

    from app.services.cz_client import CZClient
    from app.services.encryption import decrypt

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
            except Exception as e:
                failed_submissions += 1
                results.append({"kiz_code": kiz_code, "order_id": order_id, "error": str(e), "status": "FAILED"})
                logger.error(f"Error submitting batch signed doc for {kiz_code}: {e}")
                continue

            # Bounded polling for real GIS MT document verification
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

                if ord_obj and str(ord_obj.seller_id) == str(seller_id):
                    target_cz_status = "RETIRED" if action == "WITHDRAWAL" else "INTRODUCED"
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
                        seller_id=seller_id,
                        order_id=ord_obj.id,
                        kiz_code=ord_obj.kiz_code or kiz_code or "",
                        operation=KizOperationType.WITHDRAWAL if action == "WITHDRAWAL" else KizOperationType.RETURN,
                        status="SUCCESS",
                        cz_doc_id=doc_id,
                        cz_doc_status=doc_status,
                    )
                    db.add(kiz_op)

                    if ord_obj.kiz_code:
                        await sync_kiz_status_record(
                            db=db,
                            kiz_code=ord_obj.kiz_code,
                            cz_status=target_cz_status,
                            seller_id=str(seller.id),
                            doc_id=doc_id if action == "WITHDRAWAL" else None,
                        )

            elif is_confirmed is False:
                failed_submissions += 1
                results.append({"kiz_code": kiz_code, "order_id": order_id, "doc_id": doc_id, "error": error_reason, "status": "FAILED", "cz_status": doc_status})

                if ord_obj and str(ord_obj.seller_id) == str(seller_id):
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
                        seller_id=seller_id,
                        order_id=ord_obj.id,
                        kiz_code=ord_obj.kiz_code or kiz_code or "",
                        operation=KizOperationType.WITHDRAWAL if action == "WITHDRAWAL" else KizOperationType.RETURN,
                        status="FAILED",
                        cz_doc_id=doc_id,
                        cz_doc_status="CHECKED_NOT_OK",
                        error_message=error_reason,
                    )
                    db.add(kiz_op)

            else:
                # is_confirmed is None (still processing in GIS MT queue)
                successful_submissions += 1
                results.append({"kiz_code": kiz_code, "order_id": order_id, "doc_id": doc_id, "status": "IN_PROGRESS", "cz_status": doc_status})

                if ord_obj and str(ord_obj.seller_id) == str(seller_id):
                    if action == "WITHDRAWAL":
                        ord_obj.cz_withdrawal_doc_id = doc_id
                    elif action == "RETURN":
                        ord_obj.cz_return_doc_id = doc_id
                    ord_obj.cz_doc_status = "IN_PROGRESS"
                    ord_obj.updated_at = now

    elif sign_mode == "server":
        from app.agents.cz_withdrawal import withdraw_order_kiz
        from app.agents.cz_return import return_order_kiz

        withdrawals = (batch.data_payload or {}).get("withdrawals", [])
        returns = (batch.data_payload or {}).get("returns", [])

        for w in withdrawals:
            kiz = w.get("kiz_code")
            oid = w.get("order_id")
            if kiz and w.get("needs_withdrawal", True):
                withdraw_order_kiz.delay(
                    seller_id=seller_id,
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
                return_order_kiz.delay(
                    seller_id=seller_id,
                    order_id=oid,
                    kiz_code=kiz,
                )
                successful_submissions += 1

    batch.signed_at = now
    batch.signed_by = cert_subject or "Владелец ЭЦП"
    batch.submission_results = {"results": results, "successful": successful_submissions, "failed": failed_submissions}

    if failed_submissions == 0:
        batch.status = BatchStatus.COMPLETED
    elif successful_submissions > 0:
        batch.status = BatchStatus.PARTIALLY_COMPLETED
    else:
        batch.status = BatchStatus.FAILED

    await db.commit()

    # Отправка уведомления менеджеру в Telegram об успешном подписании
    if seller.telegram_bot_token_encrypted and seller.telegram_chat_ids:
        try:
            from app.services.telegram_service import TelegramService
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

            import asyncio
            await tg.send_text(seller.telegram_chat_ids, tg_text.strip())
            await tg.close()
        except Exception as tg_err:
            logger.error(f"Failed to send telegram confirmation for batch {batch_id}: {tg_err}")

    return {
        "success": True,
        "batch_id": batch.id,
        "status": batch.status.value,
        "successful_submissions": successful_submissions,
        "failed_submissions": failed_submissions,
    }


@router.delete("/kiz/signature-batches/{batch_id}")
async def cancel_signature_batch(
    seller_id: str,
    batch_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Отменяет пакет из очереди на подписание."""
    batch = await db.get(KizSignatureBatch, batch_id)
    if not batch or str(batch.seller_id) != str(seller_id):
        raise HTTPException(status_code=404, detail="Пакет не найден")

    batch.status = BatchStatus.CANCELLED
    await db.commit()
    return {"success": True, "message": f"Пакет {batch_id} отменен"}
