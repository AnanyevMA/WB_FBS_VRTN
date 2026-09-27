"""
Signature Batch Executor Service.
Handles document payload generation, live True API synchronization,
and submission execution for KizSignatureBatch.
"""
import copy
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.seller import Seller
from app.models.order import Order, KizStatus, OrderStatus
from app.models.kiz import KizSignatureBatch, BatchStatus, KizOperation, KizOperationType
from app.services.cz_client import CZClient, CZDocumentError
from app.services.kiz_service import (
    sync_kiz_status_record,
    batch_verify_and_sync_cises,
    is_kiz_withdrawn,
    CZ_STATUS_DESCRIPTIONS,
    parse_kiz_code,
)
from app.services.encryption import decrypt

logger = logging.getLogger(__name__)


async def sync_batch_with_cz_data(
    seller: Seller,
    batch: KizSignatureBatch,
    db: AsyncSession,
) -> Dict[str, Any]:
    """
    Принудительная живая сверка всех кодов маркировки пакета с True API Честного Знака.
    Обновляет данные в data_payload пакета и актуализирует счетчики.
    """
    payload = copy.deepcopy(batch.data_payload or {})
    withdrawals = payload.get("withdrawals", [])
    returns = payload.get("returns", [])

    all_kiz = [w["kiz_code"] for w in withdrawals if w.get("kiz_code")]
    all_kiz += [r["kiz_code"] for r in returns if r.get("kiz_code")]

    if all_kiz:
        synced_map = await batch_verify_and_sync_cises(
            seller=seller,
            kiz_codes=all_kiz,
            db=db,
            force_refresh=True,
        )

        seller_inn = (seller.cz_inn or "").strip()
        seller_sales_direct = 0
        wb_sales_count = 0
        foreign_sales_count = 0

        for w in withdrawals:
            code = w.get("kiz_code")
            parsed = parse_kiz_code(code) if code else {}
            clean = parsed.get("clean_cis")
            kinfo = synced_map.get(code) or (synced_map.get(clean) if clean else None)
            if kinfo:
                withdrawn, _ = is_kiz_withdrawn(
                    status=kinfo.cz_status,
                    status_ex=kinfo.cz_status_ex,
                    raw_payload=kinfo.raw_cz_payload or {},
                )
                raw_info = kinfo.raw_cz_payload or {}
                owner_inn = (raw_info.get("ownerInn") or w.get("cz_owner_inn") or "").strip()
                owner_name = raw_info.get("ownerName") or w.get("cz_owner_name") or ""
                producer_inn = (raw_info.get("producerInn") or raw_info.get("manufacturerInn") or w.get("cz_producer_inn") or "").strip()

                is_seller = (owner_inn == seller_inn) if seller_inn else False
                is_wb = (owner_inn == "9714053621")
                is_foreign = (owner_inn in ("100083608",) or "бел" in owner_name.lower() or "рб" in owner_name.lower())

                w["cz_status"] = kinfo.cz_status
                w["cz_status_desc"] = CZ_STATUS_DESCRIPTIONS.get(kinfo.cz_status or "", kinfo.cz_status or "Не проверен")
                w["cz_owner_inn"] = owner_inn
                w["cz_owner_name"] = owner_name
                w["cz_producer_inn"] = producer_inn
                w["is_seller_owner"] = is_seller
                w["is_wb_owned"] = is_wb
                w["is_already_withdrawn"] = withdrawn

                if withdrawn:
                    w["needs_withdrawal"] = False
                    w["selected"] = False
                    w["action_recommended"] = "✅ Уже выбыл из оборота"
                elif is_seller:
                    w["needs_withdrawal"] = True
                    w["selected"] = bool(code)
                    w["action_recommended"] = "✅ Баланс продавца (ИП). Готов к выводу из оборота!"
                    seller_sales_direct += 1
                elif is_wb:
                    w["needs_withdrawal"] = False
                    w["selected"] = False
                    w["action_recommended"] = "🏢 Баланс ООО «РВБ». Вывод из оборота осуществляет Wildberries."
                    wb_sales_count += 1
                elif is_foreign:
                    w["needs_withdrawal"] = False
                    w["selected"] = False
                    w["action_recommended"] = f"⛔ Экспорт в РБ ({owner_name or 'Белбланкавыд'}). Вывод продавцом невозможен."
                    foreign_sales_count += 1
                else:
                    w["needs_withdrawal"] = False
                    w["selected"] = False
                    w["action_recommended"] = f"⛔ Баланс стороннего владельца ({owner_name or owner_inn}). Вывод продавцом невозможен."
                    foreign_sales_count += 1

        seller_owned_direct = 0
        wb_owned_remarking = 0
        foreign_remarking = 0
        already_in_circ = 0

        for r in returns:
            code = r.get("kiz_code")
            parsed = parse_kiz_code(code) if code else {}
            clean = parsed.get("clean_cis")
            kinfo = synced_map.get(code) or (synced_map.get(clean) if clean else None)
            if kinfo:
                withdrawn, _ = is_kiz_withdrawn(
                    status=kinfo.cz_status,
                    status_ex=kinfo.cz_status_ex,
                    raw_payload=kinfo.raw_cz_payload or {},
                )
                r["cz_status"] = kinfo.cz_status
                r["db_cz_status"] = kinfo.cz_status
                r["cz_status_desc"] = CZ_STATUS_DESCRIPTIONS.get(kinfo.cz_status or "", kinfo.cz_status or "Не проверен")

                raw_info = kinfo.raw_cz_payload or {}
                owner_inn = (raw_info.get("ownerInn") or r.get("cz_owner_inn") or "").strip()
                owner_name = raw_info.get("ownerName") or r.get("cz_owner_name") or ""
                producer_inn = (raw_info.get("producerInn") or r.get("cz_producer_inn") or "").strip()

                is_seller = (owner_inn == seller_inn) if seller_inn else False
                is_wb = (owner_inn == "9714053621")
                is_foreign = (owner_inn in ("100083608",) or "бел" in owner_name.lower() or "рб" in owner_name.lower())

                r["cz_owner_inn"] = owner_inn
                r["cz_owner_name"] = owner_name
                r["cz_producer_inn"] = producer_inn
                r["is_seller_owner"] = is_seller
                r["is_wb_owned"] = is_wb

                if not withdrawn or kinfo.cz_status == "INTRODUCED":
                    r["needs_cz_return"] = False
                    r["needs_remarking"] = False
                    r["selected"] = False
                    r["action_recommended"] = "✅ Уже в обороте (готов к привязке)"
                    already_in_circ += 1
                elif is_seller:
                    r["needs_cz_return"] = True
                    r["needs_remarking"] = False
                    r["selected"] = True
                    r["action_recommended"] = "✅ Баланс продавца (ИП). Готов к возврату в оборот!"
                    seller_owned_direct += 1
                elif is_wb:
                    r["needs_cz_return"] = False
                    r["needs_remarking"] = True
                    r["selected"] = False
                    r["action_recommended"] = "⛔ Баланс ООО «РВБ». Прямой возврат невозможен (ошибка 11 ГИС МТ). Требуется Перемаркировка."
                    wb_owned_remarking += 1
                elif is_foreign:
                    r["needs_cz_return"] = False
                    r["needs_remarking"] = True
                    r["selected"] = False
                    r["action_recommended"] = f"⛔ Экспорт в РБ ({owner_name or 'Белбланкавыд'}). Требуется Перемаркировка."
                    foreign_remarking += 1
                else:
                    r["needs_cz_return"] = False
                    r["needs_remarking"] = True
                    r["selected"] = False
                    r["action_recommended"] = f"⛔ Баланс стороннего владельца ({owner_name or owner_inn}). Требуется Перемаркировка."
                    foreign_remarking += 1

        sales_needing = sum(1 for w in withdrawals if w.get("needs_withdrawal"))
        sales_withdrawn = sum(1 for w in withdrawals if not w.get("needs_withdrawal"))
        returns_needing = sum(1 for r in returns if r.get("needs_cz_return"))
        returns_in_circ = sum(1 for r in returns if not r.get("needs_cz_return"))

        summary = payload.get("summary", {})
        summary.update({
            "sales_needing_withdrawal": sales_needing,
            "sales_already_withdrawn": sales_withdrawn,
            "returns_needing_cz_return": returns_needing,
            "returns_already_in_circulation": returns_in_circ,
            "seller_owned_direct_count": seller_owned_direct,
            "wb_owned_remarking_count": wb_owned_remarking,
            "foreign_belarus_remarking_count": foreign_remarking,
        })
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


def build_batch_signing_payloads(
    seller: Seller,
    batch: KizSignatureBatch,
    selected_kiz_list: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Формирует неподписанные канонические документы (LK_RECEIPT и LP_RETURN)
    для последующего подписания через КриптоПро ЭЦП Browser Plugin.
    """
    cz_token = decrypt(seller.cz_token_encrypted) if seller.cz_token_encrypted else ""
    client = CZClient(inn=seller.cz_inn or "", token=cz_token)

    data = batch.data_payload or {}
    withdrawals = data.get("withdrawals", [])
    returns = data.get("returns", [])
    selected_set = set(selected_kiz_list) if selected_kiz_list is not None else None

    cades_payloads = []

    # 1. LK_RECEIPT (Вывод из оборота)
    for w in withdrawals:
        kiz = w.get("kiz_code")
        if w.get("needs_withdrawal") is False or w.get("is_already_withdrawn") is True:
            continue
        if w.get("is_seller_owner") is False:
            continue
        owner_inn = (w.get("cz_owner_inn") or "").strip()
        if owner_inn and seller.cz_inn and owner_inn != seller.cz_inn.strip():
            continue
        if selected_set is not None and kiz not in selected_set:
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

    # 2. LP_RETURN (Ввод в оборот)
    for r in returns:
        kiz = r.get("kiz_code")
        if not kiz or r.get("needs_cz_return") is False or r.get("is_already_in_circulation") is True:
            continue
        if r.get("is_seller_owner") is False:
            continue
        owner_inn = (r.get("cz_owner_inn") or "").strip()
        if owner_inn and seller.cz_inn and owner_inn != seller.cz_inn.strip():
            continue
        if selected_set is not None and kiz not in selected_set:
            continue

        receipt_num = str(r.get("receipt_number") or r.get("order_id") or "1").strip()
        receipt_dt = str(r.get("receipt_date") or "").strip() or None
        primary_doc_tp = r.get("primary_document_type") or ("RECEIPT" if receipt_num.isdigit() else "OTHER")

        doc = client.build_return_payload(
            kiz_codes=[kiz],
            wb_order_id=r.get("order_id"),
            receipt_number=receipt_num,
            receipt_date=receipt_dt,
            primary_document_type=primary_doc_tp,
        )
        cades_payloads.append({
            "action": "RETURN",
            "kiz_code": kiz,
            "order_id": r.get("order_id"),
            "sticker_id": r.get("sticker_id"),
            "receipt_number": receipt_num,
            "receipt_date": receipt_dt,
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


# Re-export for backward compatibility
from app.services.signature_batch_submitter import execute_signed_batch_submission  # noqa: E402

__all__ = [
    "sync_batch_with_cz_data",
    "build_batch_signing_payloads",
    "execute_signed_batch_submission",
]
