from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
import uuid

from app.database import get_db
from app.models.seller import Seller
from app.models.order import Order, KizStatus, OrderStatus
from app.models.kiz import KizOperation, KizOperationType
from app.models.audit import AuditLog
from app.schemas.order import KIZAttachRequest, KIZValidationResponse

router = APIRouter(prefix="/sellers/{seller_id}", tags=["kiz"])

@router.post("/orders/{order_id}/kiz")
@router.post("/kiz/attach")
async def attach_kiz(
    seller_id: str,
    order_id: Optional[int] = None,
    req: KIZAttachRequest = Body(...),
    db: AsyncSession = Depends(get_db)
):
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")
        
    kiz_code = req.kiz_code.strip()
    if len(kiz_code) < 10:
        raise HTTPException(status_code=400, detail="Неверный формат КИЗ / SGTIN")
        
    # If order_id not specified or 0, auto-find active order needing KIZ
    target_order = None
    if order_id and order_id > 0:
        target_order = await db.get(Order, order_id)
    
    if not target_order:
        res = await db.execute(
            select(Order)
            .where(
                Order.seller_id == seller_id,
                Order.kiz_status == KizStatus.PENDING,
                Order.status.in_([OrderStatus.NEW, OrderStatus.ASSEMBLING])
            )
            .order_by(Order.created_at.asc())
        )
        target_order = res.scalars().first()

    if not target_order:
        # If still no order, look for any order by seller
        res = await db.execute(
            select(Order)
            .where(Order.seller_id == seller_id)
            .order_by(Order.created_at.desc())
        )
        target_order = res.scalars().first()

    if not target_order:
        raise HTTPException(status_code=404, detail="Не найден подходящий заказ для привязки КИЗ")

    # Update order
    target_order.kiz_code = kiz_code
    target_order.kiz_status = KizStatus.ATTACHED
    target_order.kiz_attached_at = datetime.now(timezone.utc)
    
    # Resolve product info and cross-check
    from app.services.kiz_service import resolve_kiz_product_info
    kiz_info = await resolve_kiz_product_info(
        kiz_code=kiz_code,
        seller=seller,
        order=target_order,
        db=db,
        force_refresh=True
    )

    # Synchronize CZ status and error checking
    target_order.kiz_cz_status = kiz_info.cz_status
    target_order.kiz_cz_status_updated_at = datetime.now(timezone.utc)
    if not kiz_info.is_valid:
        target_order.kiz_status = KizStatus.ERROR
    else:
        target_order.kiz_status = KizStatus.ATTACHED

    # Audit log
    audit = AuditLog(
        seller_id=seller_id,
        agent="kiz_scanner",
        action="ATTACH_KIZ",
        entity_type="order",
        entity_id=str(target_order.id),
        payload={
            "kiz_code": kiz_code,
            "order_id": target_order.id,
            "gtin": kiz_info.gtin,
            "product_name": kiz_info.product_name,
            "cz_status": kiz_info.cz_status,
            "is_valid": kiz_info.is_valid,
            "validation_message": kiz_info.validation_message,
        }
    )
    db.add(audit)
    
    await db.commit()
    
    raw_cz = kiz_info.raw_cz_payload or {}
    ogvs = raw_cz.get("ogvs") or []

    return {
        "success": True,
        "message": f"КИЗ успешно прикреплен к заказу #{target_order.id}",
        "order_id": target_order.id,
        "kiz_code": kiz_code,
        "kiz_status": target_order.kiz_status.value,
        "product_info": {
            "gtin": kiz_info.gtin,
            "product_name": kiz_info.product_name,
            "article": kiz_info.article,
            "tech_size": kiz_info.tech_size,
            "wb_size": kiz_info.wb_size,
            "cz_status": kiz_info.cz_status,
            "ogvs": ogvs,
            "blocked_by_ogv": len(ogvs) > 0,
            "is_valid": kiz_info.is_valid,
            "validation_message": kiz_info.validation_message,
        }
    }


@router.post("/kiz/lookup")
async def lookup_kiz(
    seller_id: str,
    req: KIZAttachRequest = Body(...),
    db: AsyncSession = Depends(get_db)
):
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")
        
    raw_kiz = req.kiz_code.strip()
    if not raw_kiz:
        raise HTTPException(status_code=400, detail="Не указан код КИЗ")

    from app.services.kiz_service import resolve_kiz_product_info
    kiz_info = await resolve_kiz_product_info(
        kiz_code=raw_kiz,
        seller=seller,
        db=db,
        force_refresh=True
    )
    
    raw_cz = kiz_info.raw_cz_payload or {}
    ogvs = raw_cz.get("ogvs") or []

    return {
        "kiz_code": kiz_info.kiz_code,
        "gtin": kiz_info.gtin,
        "serial_number": kiz_info.serial_number,
        "clean_cis": kiz_info.clean_cis,
        "product_name": kiz_info.product_name,
        "brand": kiz_info.brand,
        "article": kiz_info.article,
        "tech_size": kiz_info.tech_size,
        "wb_size": kiz_info.wb_size,
        "tnved": kiz_info.tnved,
        "cz_status": kiz_info.cz_status,
        "cz_status_ex": kiz_info.cz_status_ex,
        "cz_owner_inn": kiz_info.cz_owner_inn,
        "cz_owner_name": kiz_info.cz_owner_name,
        "ogvs": ogvs,
        "blocked_by_ogv": len(ogvs) > 0,
        "is_valid": kiz_info.is_valid,
        "validation_message": kiz_info.validation_message,
        "checked_at": kiz_info.checked_at.isoformat() if kiz_info.checked_at else None
    }

@router.delete("/orders/{order_id}/kiz")
async def detach_kiz(seller_id: str, order_id: int, db: AsyncSession = Depends(get_db)):
    order = await db.get(Order, order_id)
    if not order or str(order.seller_id) != str(seller_id):
        raise HTTPException(status_code=404, detail="Заказ не найден")
        
    order.kiz_code = None
    order.kiz_status = KizStatus.PENDING
    order.kiz_cz_status = None
    order.kiz_cz_status_updated_at = None
    order.kiz_attached_at = None
    
    audit = AuditLog(
        seller_id=seller_id,
        agent="kiz_api",
        action="DETACH_KIZ",
        entity_type="order",
        entity_id=str(order_id),
        payload={"order_id": order_id}
    )
    db.add(audit)
    await db.commit()
    return {"message": "КИЗ откреплен", "order_id": order_id}

@router.get("/orders/{order_id}/kiz/validate")
async def validate_kiz(seller_id: str, order_id: int, db: AsyncSession = Depends(get_db)):
    order = await db.get(Order, order_id)
    if not order or str(order.seller_id) != str(seller_id):
        raise HTTPException(status_code=404, detail="Заказ не найден")
        
    code = order.kiz_code or ""
    is_valid = len(code) >= 25 and (code.startswith("01") or code.startswith("046"))
    return {
        "valid": is_valid,
        "details": {
            "kiz_code": code,
            "length": len(code),
            "format": "GS1 DataMatrix (SGTIN)" if is_valid else "Неизвестный формат"
        }
    }

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
    import logging
    logger = logging.getLogger(__name__)

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


@router.get("/kiz/operations")
async def list_kiz_operations(seller_id: str, db: AsyncSession = Depends(get_db)):
    res = await db.execute(
        select(AuditLog)
        .where(AuditLog.seller_id == seller_id, AuditLog.entity_type.in_(["kiz", "order"]))
        .order_by(AuditLog.created_at.desc())
        .limit(50)
    )
    logs = res.scalars().all()
    
    items = []
    for l in logs:
        items.append({
            "id": l.id,
            "action": l.action,
            "entity_id": l.entity_id,
            "payload": l.payload,
            "created_at": l.created_at.isoformat() if l.created_at else None
        })
    return {"items": items}
