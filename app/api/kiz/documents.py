"""
FastAPI KIZ Document Preparation, Signing & Submission Endpoints — WB FBS Manager
"""
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.seller import Seller
from app.models.order import Order, KizStatus, OrderStatus
from app.models.kiz import KizOperation, KizOperationType
from app.models.audit import AuditLog
from app.services.kiz_service import sync_kiz_status_record

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/kiz/withdraw")
async def withdraw_kiz(
    seller_id: str,
    order_ids: Optional[List[int]] = Body(default=[]),
    price_kopecks: Optional[int] = Body(default=0),
    db: AsyncSession = Depends(get_db)
):
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")
        
    from app.agents.cz_withdrawal import withdraw_order_kiz
    updated_orders = []
    if order_ids:
        for oid in order_ids:
            order = await db.get(Order, oid)
            if order and str(order.seller_id) == str(seller_id) and order.kiz_code:
                order.status = OrderStatus.DELIVERED
                price_kop = int((order.price or 0) * 100) if not price_kopecks else price_kopecks
                withdraw_order_kiz.delay(
                    seller_id=seller_id,
                    order_id=oid,
                    kiz_code=order.kiz_code,
                    price_kopecks=price_kop,
                )
                updated_orders.append(oid)
    else:
        # Withdraw all attached orders
        res = await db.execute(
            select(Order).where(Order.seller_id == seller_id, Order.kiz_status == KizStatus.ATTACHED)
        )
        orders = res.scalars().all()
        for o in orders:
            if o.kiz_code:
                o.status = OrderStatus.DELIVERED
                price_kop = int((o.price or 0) * 100) if not price_kopecks else price_kopecks
                withdraw_order_kiz.delay(
                    seller_id=seller_id,
                    order_id=o.id,
                    kiz_code=o.kiz_code,
                    price_kopecks=price_kop,
                )
                updated_orders.append(o.id)
            
    audit = AuditLog(
        seller_id=seller_id,
        agent="cz_withdrawal_agent",
        action="WITHDRAW_CZ",
        entity_type="kiz",
        entity_id=str(seller_id),
        payload={"processed_orders": len(updated_orders), "order_ids": updated_orders}
    )
    db.add(audit)
    await db.commit()
    
    return {
        "message": "Запущено выбытие товаров в Честном Знаке (LK_RECEIPT / DISTANCE)",
        "processed_orders_count": len(updated_orders),
        "order_ids": updated_orders
    }


@router.post("/kiz/return")
async def return_kiz(
    seller_id: str,
    order_ids: Optional[List[int]] = Body(default=[]),
    db: AsyncSession = Depends(get_db)
):
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")
        
    from app.agents.cz_return import return_order_kiz
    updated_orders = []
    if order_ids:
        for oid in order_ids:
            order = await db.get(Order, oid)
            if order and str(order.seller_id) == str(seller_id) and order.kiz_code:
                return_order_kiz.delay(
                    seller_id=seller_id,
                    order_id=oid,
                    kiz_code=order.kiz_code,
                )
                updated_orders.append(oid)
                
    audit = AuditLog(
        seller_id=seller_id,
        agent="cz_return_agent",
        action="RETURN_CZ",
        entity_type="kiz",
        entity_id=str(seller_id),
        payload={"processed_orders": len(updated_orders), "order_ids": updated_orders}
    )
    db.add(audit)
    await db.commit()
    
    return {
        "message": "Запущен возврат товаров в Честный Знак (LP_RETURN / REMOTE_SALE_RETURN)",
        "processed_orders_count": len(updated_orders),
        "order_ids": updated_orders
    }


@router.post("/kiz/prepare-document")
async def prepare_kiz_document(
    seller_id: str,
    payload: Dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db)
):
    """
    Формирует канонический JSON-документ (LK_RECEIPT или LP_RETURN) для подписания
    на клиенте через КриптоПро Browser Plugin.
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    action = str(payload.get("action", "WITHDRAWAL")).upper()
    order_ids = payload.get("order_ids", [])
    if not order_ids:
        raise HTTPException(status_code=400, detail="Не указаны ID заказов")

    orders = []
    kiz_codes = []
    total_price_kop = 0

    for oid in order_ids:
        o = await db.get(Order, oid)
        if o and str(o.seller_id) == str(seller_id) and o.kiz_code:
            orders.append(o)
            kiz_codes.append(o.kiz_code)
            order_price_kop = int((o.price or 0) * 100)
            total_price_kop += order_price_kop

    if not kiz_codes:
        raise HTTPException(status_code=400, detail="У выбранных заказов нет прикрепленных КИЗ")

    from app.services.cz_client import CZClient
    from app.services.encryption import decrypt

    cz_token = decrypt(seller.cz_token_encrypted) if seller.cz_token_encrypted else ""
    client = CZClient(inn=seller.cz_inn or "", token=cz_token)

    if action == "WITHDRAWAL":
        doc_payload = client.build_withdrawal_payload(
            kiz_codes=kiz_codes,
            price_kopecks=payload.get("price_kopecks") or total_price_kop,
            mod_fias=seller.mod_fias,
            mod_kpp=seller.mod_kpp,
            wb_order_id=orders[0].id if len(orders) == 1 else None,
        )
    elif action == "RETURN":
        doc_payload = client.build_return_payload(
            kiz_codes=kiz_codes,
            wb_order_id=orders[0].id if len(orders) == 1 else None,
        )
    else:
        raise HTTPException(status_code=400, detail=f"Неизвестный тип действия: {action}")

    return {
        "success": True,
        "action": action,
        "document_type": doc_payload["type"],
        "document_json": doc_payload["inner_json"],
        "document_base64": doc_payload["document_base64"],
        "order_ids": [o.id for o in orders],
        "kiz_codes": kiz_codes,
        "seller_inn": seller.cz_inn,
        "count": len(orders),
    }


@router.post("/kiz/submit-signed-document")
async def submit_signed_kiz_document(
    seller_id: str,
    payload: Dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db)
):
    """
    Принимает подписанный в браузере документ с открепленной подписью УКЭП
    и отправляет в ГИС МТ (Честный Знак).
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    doc_type = payload.get("document_type", "LK_RECEIPT")
    doc_base64 = payload.get("document_base64")
    sig_base64 = payload.get("signature_base64")
    order_ids = payload.get("order_ids", [])
    action = str(payload.get("action", "WITHDRAWAL")).upper()

    if not doc_base64 or not sig_base64:
        raise HTTPException(status_code=400, detail="Отсутствует документ или подпись Base64")

    from app.services.cz_client import CZClient
    from app.services.encryption import decrypt

    cz_token = decrypt(seller.cz_token_encrypted) if seller.cz_token_encrypted else ""
    client = CZClient(inn=seller.cz_inn or "", token=cz_token)

    try:
        doc_id = await client.submit_signed_document(
            document_type=doc_type,
            document_base64=doc_base64,
            signature_base64=sig_base64,
            wait_for_result=False,
        )
    except Exception as e:
        logger.error(f"Error submitting signed document to ГИС МТ: {e}")
        audit = AuditLog(
            seller_id=seller_id,
            agent="web_cades_signer",
            action="SUBMIT_SIGNED_DOC_FAILED",
            entity_type="kiz",
            entity_id=str(order_ids),
            error=str(e),
            payload={"order_ids": order_ids, "action": action},
        )
        db.add(audit)
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Ошибка отправки в ГИС МТ: {e}")

    now = datetime.now(timezone.utc)
    target_cz_status = "RETIRED" if action == "WITHDRAWAL" else "INTRODUCED"

    for oid in order_ids:
        o = await db.get(Order, oid)
        if o and str(o.seller_id) == str(seller_id):
            if action == "WITHDRAWAL":
                o.kiz_status = KizStatus.WITHDRAWN
                o.kiz_cz_status = "RETIRED"
                o.cz_withdrawal_doc_id = doc_id
            elif action == "RETURN":
                o.kiz_status = KizStatus.RETURNED
                o.kiz_cz_status = "INTRODUCED"
            o.kiz_cz_status_updated_at = now
            o.updated_at = now

            kiz_op = KizOperation(
                seller_id=seller_id,
                order_id=o.id,
                kiz_code=o.kiz_code or "",
                operation=KizOperationType.WITHDRAWAL if action == "WITHDRAWAL" else KizOperationType.RETURN,
                status="SUCCESS",
                cz_doc_id=doc_id,
            )
            db.add(kiz_op)

            if o.kiz_code:
                await sync_kiz_status_record(
                    db=db,
                    kiz_code=o.kiz_code,
                    cz_status=target_cz_status,
                    seller_id=str(seller.id),
                    doc_id=doc_id if action == "WITHDRAWAL" else None,
                )

    audit = AuditLog(
        seller_id=seller_id,
        agent="web_cades_signer",
        action="SUBMIT_SIGNED_DOC_SUCCESS",
        entity_type="kiz",
        entity_id=str(doc_id),
        payload={"doc_id": doc_id, "order_ids": order_ids, "action": action, "count": len(order_ids)},
    )
    db.add(audit)
    await db.commit()

    try:
        from app.agents.notifier import send_cz_status_notification
        for oid in order_ids:
            send_cz_status_notification.delay(
                seller_id=seller_id,
                order_id=oid,
                success=True,
                doc_id=doc_id,
            )
    except Exception as exc:
        logger.warning(f"Could not dispatch telegram notification: {exc}")

    action_label = "вывода из оборота" if action == "WITHDRAWAL" else "возврата в оборот"
    return {
        "success": True,
        "doc_id": doc_id,
        "order_ids": order_ids,
        "message": f"Документ {action_label} успешно подписан и принят ГИС МТ (ID: {doc_id})",
    }
