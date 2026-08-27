"""
FastAPI WB Archive Upload, Analysis & Sync Endpoints — WB FBS Manager
"""
import logging
from datetime import datetime, timezone
from typing import Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Body, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.seller import Seller
from app.models.order import Order, OrderStatus
from app.models.audit import AuditLog
from app.services.kiz_service import (
    batch_verify_and_sync_cises,
    is_kiz_withdrawn,
    CZ_STATUS_DESCRIPTIONS,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/archive/preview")
async def preview_wb_archive(
    seller_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Загрузка и предпросмотр архива WB (archive.xlsx).
    Парсит листы «КИЗ» и «Сборочные задания», сопоставляет с БД и формирует список к выводу и возврату.
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Формат файла должен быть .xlsx или .xls")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Файл пуст")

    from app.services.archive_service import parse_wb_archive_excel, analyze_archive_data
    try:
        parsed_sheets = parse_wb_archive_excel(content)
    except Exception as e:
        logger.error(f"Failed to parse Excel file: {e}")
        raise HTTPException(status_code=400, detail=f"Ошибка чтения Excel: {str(e)}")

    analysis = await analyze_archive_data(seller=seller, archive_data=parsed_sheets, db=db)

    # Log audit
    audit = AuditLog(
        seller_id=seller_id,
        agent="archive_upload",
        action="ARCHIVE_PREVIEW",
        entity_type="archive",
        entity_id=file.filename,
        payload=analysis["summary"],
    )
    db.add(audit)
    await db.commit()

    return {
        "success": True,
        "filename": file.filename,
        "summary": analysis["summary"],
        "withdrawals": analysis["withdrawals"],
        "returns": analysis["returns"],
    }


@router.post("/archive/sync-cz")
async def sync_archive_kiz_with_cz(
    seller_id: str,
    payload: Dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Принудительная живая пакетная сверка кодов маркировки архива с True API Честного Знака.
    Обновляет единые записи в kiz_product_info и синхронизирует все заказы.
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    kiz_codes = payload.get("kiz_codes", [])
    if not kiz_codes:
        raise HTTPException(status_code=400, detail="Список кодов КИЗ пуст")

    synced_map = await batch_verify_and_sync_cises(
        seller=seller,
        kiz_codes=kiz_codes,
        db=db,
        force_refresh=True
    )
    await db.commit()

    response_items = {}
    for code, info in synced_map.items():
        if info:
            withdrawn, w_reason = is_kiz_withdrawn(
                status=info.cz_status,
                status_ex=info.cz_status_ex,
                raw_payload=info.raw_cz_payload or {}
            )
            response_items[code] = {
                "kiz_code": code,
                "cz_status": info.cz_status,
                "cz_status_desc": CZ_STATUS_DESCRIPTIONS.get(info.cz_status or "", info.cz_status or "Не проверен"),
                "is_withdrawn": withdrawn,
                "needs_withdrawal": not withdrawn,
                "validation_message": info.validation_message,
            }

    return {
        "success": True,
        "count": len(response_items),
        "items": response_items,
    }


@router.post("/archive/process")
async def process_wb_archive(
    seller_id: str,
    payload: Dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Обработка выбранных позиций из архива WB:
    1. Запуск вывода КИЗ из оборота («Дистанционная продажа») с чеками.
    2. Запуск возврата КИЗ в оборот (или освобождение).
    Поддерживает серверную очередь Celery или генерацию пакетов для браузерной УКЭП (КриптоПро).
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    withdrawals_to_process = payload.get("withdrawals", [])
    returns_to_process = payload.get("returns", [])
    sign_mode = payload.get("sign_mode", "server")  # 'server' or 'client_cades'

    now = datetime.now(timezone.utc)
    queued_withdrawals = 0
    queued_returns = 0
    cades_payloads = []

    # 1. Process withdrawals
    if withdrawals_to_process:
        if sign_mode == "client_cades":
            from app.services.cz_client import CZClient
            from app.services.encryption import decrypt
            try:
                cz_token = decrypt(seller.cz_token_encrypted) if seller.cz_token_encrypted else None
            except Exception:
                cz_token = None
            client = CZClient(inn=seller.cz_inn or "", token=cz_token, cert_thumbprint=seller.cz_cert_path)
            
            for item in withdrawals_to_process:
                kiz_code = item.get("kiz_code")
                order_id = item.get("order_id")
                price_kopecks = item.get("price_kopecks", 0)
                receipt_number = item.get("receipt_number")
                receipt_date = item.get("receipt_date")

                if not kiz_code:
                    continue

                unsigned_doc = client.build_withdrawal_payload(
                    kiz_codes=[kiz_code],
                    price_kopecks=price_kopecks,
                    mod_fias=seller.mod_fias,
                    mod_kpp=seller.mod_kpp,
                    wb_order_id=order_id,
                    receipt_number=receipt_number,
                    receipt_date=receipt_date,
                    document_type="RECEIPT" if receipt_number else "OTHER",
                )
                cades_payloads.append({
                    "action": "WITHDRAWAL",
                    "order_id": order_id,
                    "kiz_code": kiz_code,
                    "receipt_number": receipt_number,
                    "document_base64": unsigned_doc["document_base64"],
                    "inner_json": unsigned_doc["inner_json"],
                    "type": unsigned_doc["type"],
                })
        else:
            from app.agents.cz_withdrawal import withdraw_order_kiz
            for item in withdrawals_to_process:
                kiz_code = item.get("kiz_code")
                order_id = item.get("order_id")
                price_kopecks = item.get("price_kopecks", 0)
                receipt_number = item.get("receipt_number")
                receipt_date = item.get("receipt_date")

                if not kiz_code or not order_id:
                    continue

                withdraw_order_kiz.apply_async(
                    kwargs={
                        "seller_id": seller_id,
                        "order_id": order_id,
                        "kiz_code": kiz_code,
                        "price_kopecks": price_kopecks,
                        "receipt_number": receipt_number,
                        "receipt_date": receipt_date,
                    },
                    queue="cz_operations",
                    countdown=queued_withdrawals * 2,  # Stagger requests
                )
                queued_withdrawals += 1

                # Update order in DB if found
                order = await db.get(Order, order_id)
                if order:
                    order.status = OrderStatus.DELIVERED
                    order.wb_status = "sold"
                    order.updated_at = now

    # 2. Process returns
    if returns_to_process:
        if sign_mode == "client_cades":
            from app.services.cz_client import CZClient
            from app.services.encryption import decrypt
            try:
                cz_token = decrypt(seller.cz_token_encrypted) if seller.cz_token_encrypted else None
            except Exception:
                cz_token = None
            client = CZClient(inn=seller.cz_inn or "", token=cz_token, cert_thumbprint=seller.cz_cert_path)

            for item in returns_to_process:
                kiz_code = item.get("kiz_code")
                order_id = item.get("order_id")
                needs_cz = item.get("needs_cz_return", False)

                if not kiz_code:
                    continue

                if needs_cz:
                    unsigned_doc = client.build_return_payload(
                        kiz_codes=[kiz_code],
                        wb_order_id=order_id,
                    )
                    cades_payloads.append({
                        "action": "RETURN",
                        "order_id": order_id,
                        "kiz_code": kiz_code,
                        "document_base64": unsigned_doc["document_base64"],
                        "inner_json": unsigned_doc["inner_json"],
                        "type": unsigned_doc["type"],
                    })
        else:
            from app.agents.cz_return import return_order_kiz
            for item in returns_to_process:
                kiz_code = item.get("kiz_code")
                order_id = item.get("order_id")
                needs_cz = item.get("needs_cz_return", False)

                if order_id:
                    order = await db.get(Order, order_id)
                    if order:
                        order.status = OrderStatus.CANCELLED
                        order.wb_status = "canceled_by_client"
                        order.updated_at = now

                if needs_cz and kiz_code and order_id:
                    return_order_kiz.apply_async(
                        kwargs={
                            "seller_id": seller_id,
                            "order_id": order_id,
                            "kiz_code": kiz_code,
                        },
                        queue="cz_operations",
                        countdown=queued_returns * 2,
                    )
                    queued_returns += 1

    await db.commit()

    # Log audit
    audit = AuditLog(
        seller_id=seller_id,
        agent="archive_processor",
        action="ARCHIVE_PROCESS",
        entity_type="archive",
        entity_id=str(seller_id),
        payload={
            "sign_mode": sign_mode,
            "queued_withdrawals": queued_withdrawals,
            "queued_returns": queued_returns,
            "cades_count": len(cades_payloads),
        },
    )
    db.add(audit)
    await db.commit()

    if sign_mode == "client_cades":
        return {
            "success": True,
            "sign_mode": "client_cades",
            "cades_payloads": cades_payloads,
            "count": len(cades_payloads),
            "message": f"Сформировано {len(cades_payloads)} документов для подписания в браузере",
        }

    return {
        "success": True,
        "sign_mode": "server",
        "queued_withdrawals": queued_withdrawals,
        "queued_returns": queued_returns,
        "message": f"Запущено в обработку: {queued_withdrawals} выводов (с чеками) и {queued_returns} возвратов в оборот",
    }
