"""
Unified KIZ Payload Builder — Helper for unified_kiz_batch_service.
Constructs normalized withdrawals and returns payloads with owner validation.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from app.models.order import Order, KizStatus
from app.services.kiz_service import is_kiz_withdrawn, CZ_STATUS_DESCRIPTIONS


def build_unified_withdrawals_payload(
    sales_candidates: Dict[str, Dict[str, Any]],
    cz_info_map: Dict[str, Dict[str, Any]],
    fbs_order_lookup: Dict[str, Order],
    history_by_cis: Dict[str, List[Dict[str, Any]]],
    seller_inn: str,
    now_utc: datetime,
    withdrawn_cises: Optional[Set[str]] = None,
) -> Tuple[List[Dict[str, Any]], int, int, int, int]:
    """
    Формирует payload выбытия (WITHDRAWALS) со строгой проверкой владельца,
    многоуровневым исключением уже выбывших заказов/операций и обогащением
    реквизитами фискальных чеков из онлайн-отчета WB excise-report.
    """
    withdrawals_payload = []
    sales_needing_count = 0
    sales_already_withdrawn_count = 0
    sales_wb_owned_count = 0
    sales_foreign_count = 0

    for cis, ev in sales_candidates.items():
        cz_item = cz_info_map.get(cis, {})
        cz_status = cz_item.get("status")
        cz_withdrawn, _ = is_kiz_withdrawn(status=cz_status, status_ex=cz_item.get("statusEx"), raw_payload=cz_item) if cz_status else (False, "")

        fbs_order = ev.get("order") or fbs_order_lookup.get(cis)

        # 1. Приоритетный фильтр по состоянию заказа FBS:
        is_order_withdrawn = False
        if fbs_order:
            if fbs_order.kiz_status == KizStatus.WITHDRAWN:
                is_order_withdrawn = True
            elif fbs_order.cz_withdrawal_doc_id and fbs_order.cz_doc_status in ("IN_PROGRESS", "CHECKED_OK", "SUCCESS"):
                is_order_withdrawn = True
            elif fbs_order.cz_doc_status == "CHECKED_OK":
                is_order_withdrawn = True

        # 2. Приоритетный фильтр по операциям выбытия ГИС МТ в БД:
        is_op_withdrawn = bool(withdrawn_cises and cis in withdrawn_cises)

        withdrawn = cz_withdrawn or is_order_withdrawn or is_op_withdrawn

        owner_inn = (cz_item.get("ownerInn") or "").strip()
        owner_name = cz_item.get("ownerName") or ""
        producer_inn = (cz_item.get("producerInn") or cz_item.get("manufacturerInn") or "").strip()

        is_seller = (owner_inn == seller_inn) if seller_inn else False
        is_wb = (owner_inn == "9714053621")
        is_foreign = (owner_inn in ("100083608",) or "бел" in owner_name.lower() or "рб" in owner_name.lower())

        if withdrawn:
            needs_withdrawal, selected = False, False
            if is_order_withdrawn:
                action_rec = "✅ Уже выбыл из оборота (заказ FBS списан/в обработке)"
            elif is_op_withdrawn:
                action_rec = "✅ Уже выбыл из оборота (операция выбытия ГИС МТ)"
            else:
                action_rec = "✅ Уже выбыл из оборота"
            sales_already_withdrawn_count += 1
        elif is_seller:
            needs_withdrawal, selected = True, True
            action_rec = "✅ Баланс продавца (ИП). Готов к выводу из оборота!"
            sales_needing_count += 1
        elif is_wb:
            needs_withdrawal, selected = False, False
            action_rec = "🏢 Баланс ООО «РВБ». Вывод из оборота осуществляет Wildberries."
            sales_wb_owned_count += 1
        elif is_foreign:
            needs_withdrawal, selected = False, False
            action_rec = f"⛔ Экспорт в РБ ({owner_name or 'Белбланкавыд'}). Вывод продавцом невозможен."
            sales_foreign_count += 1
        else:
            needs_withdrawal, selected = False, False
            action_rec = f"⛔ Баланс стороннего владельца ({owner_name or owner_inn}). Вывод продавцом невозможен."
            sales_foreign_count += 1

        price_val = ev.get("price") or (float(fbs_order.price) if fbs_order and fbs_order.price else 0.0)

        # Обогащение реквизитами фискального чека из excise-report
        all_cis_events = history_by_cis.get(cis, [])
        excise_ev = next((e for e in all_cis_events if e.get("source") == "wb_excise_report"), None)

        receipt_num = str(
            (excise_ev and excise_ev.get("receipt_number"))
            or ev.get("receipt_number")
            or ev.get("rrd_id")
            or (fbs_order.id if fbs_order else "")
        )
        fn_num = str((excise_ev and excise_ev.get("fn_number")) or ev.get("fn_number") or "")
        receipt_dt = str(
            (excise_ev and excise_ev.get("receipt_date"))
            or ev.get("receipt_date")
            or ev.get("rr_date")
            or now_utc.strftime("%Y-%m-%d")
        )

        # В рабочий список выбытия включаем ТОЛЬКО позиции, реально требующие вывода
        if needs_withdrawal:
            order_id_val = fbs_order.id if fbs_order else ev.get("order_id")
            sticker_id_val = fbs_order.sticker_id if fbs_order else ev.get("sticker_id")
            withdrawals_payload.append({
                "order_id": order_id_val,
                "sticker_id": sticker_id_val,
                "kiz_code": ev.get("raw_kiz") or cis,
                "clean_cis": cis,
                "receipt_number": receipt_num,
                "fn_number": fn_num,
                "receipt_date": receipt_dt,
                "price": price_val,
                "price_kopecks": int(round(price_val * 100)),
                "article": ev.get("article") or (fbs_order.article if fbs_order else ""),
                "name": ev.get("name") or ((fbs_order.name or fbs_order.subject) if fbs_order else "Товар WB"),
                "task_status": "Продажа WB (выбытие)",
                "db_status": fbs_order.status.value if fbs_order else "Архив/FBO",
                "cz_status": cz_status or "UNKNOWN",
                "cz_status_desc": CZ_STATUS_DESCRIPTIONS.get(cz_status or "", cz_status or "Не проверен"),
                "cz_owner_inn": owner_inn,
                "cz_owner_name": owner_name,
                "cz_producer_inn": producer_inn,
                "is_seller_owner": is_seller,
                "is_wb_owned": is_wb,
                "is_already_withdrawn": withdrawn,
                "needs_withdrawal": needs_withdrawal,
                "action_recommended": action_rec,
                "selected": selected,
            })

    return (
        withdrawals_payload,
        sales_needing_count,
        sales_already_withdrawn_count,
        sales_wb_owned_count,
        sales_foreign_count,
    )


def build_unified_returns_payload(
    return_candidates: Dict[str, Dict[str, Any]],
    cz_info_map: Dict[str, Dict[str, Any]],
    fbs_order_lookup: Dict[str, Order],
    seller_inn: str,
    now_utc: datetime,
    returned_cises: Optional[Set[str]] = None,
) -> Tuple[List[Dict[str, Any]], int, int, int, int]:
    """
    Формирует payload возвратов (RETURNS) со строгой проверкой владельца
    и разделением на прямой возврат (свой ИНН) и перемаркировку (баланс WB/сторонний).
    Исключает повторные возвраты, если товар уже введен в оборот (KizOperation или Order).
    """
    returns_payload = []
    seller_owned_direct_count = 0
    wb_owned_remarking_count = 0
    foreign_remarking_count = 0
    already_in_circ_count = 0

    for cis, ev in return_candidates.items():
        cz_item = cz_info_map.get(cis, {})
        cz_status = cz_item.get("status")
        withdrawn, _ = is_kiz_withdrawn(status=cz_status, status_ex=cz_item.get("statusEx"), raw_payload=cz_item) if cz_status else (True, "")

        owner_inn = (cz_item.get("ownerInn") or "").strip()
        owner_name = cz_item.get("ownerName") or ""
        producer_inn = (cz_item.get("producerInn") or "").strip()

        is_seller = (owner_inn == seller_inn) if seller_inn else False
        is_wb = (owner_inn == "9714053621")
        is_foreign = (owner_inn in ("100083608",) or "бел" in owner_name.lower() or "рб" in owner_name.lower())

        fbs_order = ev.get("order") or fbs_order_lookup.get(cis)

        # 1. Многоуровневое исключение повторных возвратов (Order + KizOperation)
        is_order_returned = False
        if fbs_order:
            if fbs_order.kiz_status == KizStatus.RETURNED:
                is_order_returned = True
            elif fbs_order.cz_return_doc_id and fbs_order.cz_doc_status in ("IN_PROGRESS", "CHECKED_OK", "ACCEPTED", "SUCCESS"):
                is_order_returned = True
            elif fbs_order.cz_doc_status == "CHECKED_OK" and fbs_order.status == OrderStatus.CANCELLED:
                is_order_returned = True

        if returned_cises and cis in returned_cises:
            is_order_returned = True

        is_already_in_circ = (not withdrawn) or (cz_status == "INTRODUCED") or is_order_returned

        if is_order_returned:
            return_mode, needs_cz_return, needs_remarking, selected = "INTRODUCED", False, False, False
            action_rec = "✅ Уже возвращен в оборот (по операциям/заказу)"
            already_in_circ_count += 1
        elif is_already_in_circ:
            return_mode, needs_cz_return, needs_remarking, selected = "INTRODUCED", False, False, False
            action_rec = "✅ Уже в обороте (готов к привязке)"
            already_in_circ_count += 1
        elif is_seller:
            return_mode, needs_cz_return, needs_remarking, selected = "DIRECT_RETURN", True, False, True
            action_rec = "✅ Баланс продавца (ИП). Готов к возврату в оборот!"
            seller_owned_direct_count += 1
        elif is_wb:
            return_mode, needs_cz_return, needs_remarking, selected = "WB_OWNED_REMARKING", False, True, False
            action_rec = "⛔ Баланс ООО «РВБ». Прямой возврат невозможен (ошибка 11 ГИС МТ). Требуется Перемаркировка (новый КИЗ)."
            wb_owned_remarking_count += 1
        elif is_foreign:
            return_mode, needs_cz_return, needs_remarking, selected = "FOREIGN_OPERATOR", False, True, False
            action_rec = f"⛔ Экспорт в РБ ({owner_name or 'Белбланкавыд'}). Требуется Перемаркировка."
            foreign_remarking_count += 1
        else:
            return_mode, needs_cz_return, needs_remarking, selected = "OTHER_OWNED", False, True, False
            action_rec = f"⛔ Баланс стороннего владельца ({owner_name or owner_inn}). Требуется Перемаркировка."
            foreign_remarking_count += 1

        order_id_val = fbs_order.id if fbs_order else ev.get("order_id")
        sticker_id_val = fbs_order.sticker_id if fbs_order else ev.get("sticker_id")
        price_val = ev.get("price") or (float(fbs_order.price) if fbs_order and fbs_order.price else 0.0)
        receipt_num = str(ev.get("receipt_number") or ev.get("rrd_id") or (order_id_val if order_id_val else "1"))
        receipt_dt = str(ev.get("receipt_date") or ev.get("rr_date") or now_utc.strftime("%Y-%m-%d"))

        # В рабочий список возвратов включаем ТОЛЬКО позиции, реально требующие ввода в оборот
        if needs_cz_return:
            returns_payload.append({
                "order_id": order_id_val,
                "sticker_id": sticker_id_val,
                "kiz_code": ev.get("raw_kiz") or cis,
                "clean_cis": cis,
                "receipt_number": receipt_num,
                "receipt_date": receipt_dt,
                "price": price_val,
                "price_kopecks": int(round(price_val * 100)),
                "article": ev.get("article") or (fbs_order.article if fbs_order else ""),
                "name": ev.get("name") or ((fbs_order.name or fbs_order.subject) if fbs_order else "Товар WB"),
                "task_status": "Возврат WB",
                "db_status": fbs_order.status.value if fbs_order else "Архив/FBO",
                "cz_status": cz_status or "UNKNOWN",
                "cz_status_desc": CZ_STATUS_DESCRIPTIONS.get(cz_status or "", cz_status or "Не проверен"),
                "cz_owner_inn": owner_inn,
                "cz_owner_name": owner_name,
                "cz_producer_inn": producer_inn,
                "is_seller_owner": is_seller,
                "is_wb_owned": is_wb,
                "is_already_in_circulation": is_already_in_circ,
                "needs_cz_return": needs_cz_return,
                "needs_remarking": False,
                "return_mode": return_mode,
                "action_recommended": action_rec,
                "selected": selected,
            })

    return (
        returns_payload,
        seller_owned_direct_count,
        wb_owned_remarking_count,
        foreign_remarking_count,
        already_in_circ_count,
    )
