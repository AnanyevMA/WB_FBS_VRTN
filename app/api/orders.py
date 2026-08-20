import logging
import uuid
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_

from app.database import get_db
from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus
from app.models.supply import Supply, SupplyStatus
from app.models.audit import AuditLog
from app.schemas.order import OrderResponse, OrderListItem
from app.services.encryption import decrypt
from app.services.wb_client import WBClient, is_kiz_required

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sellers/{seller_id}/orders", tags=["orders"])


@router.get("/stats")
async def get_dashboard_stats(seller_id: str, db: AsyncSession = Depends(get_db)):
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # Orders created today or updated today
    orders_today = await db.scalar(
        select(func.count(Order.id)).where(
            and_(Order.seller_id == seller_id, Order.created_at >= today_start)
        )
    ) or 0

    # Total orders
    total_orders = await db.scalar(
        select(func.count(Order.id)).where(Order.seller_id == seller_id)
    ) or 0

    # Pending assembly (NEW or ASSEMBLING)
    pending_count = await db.scalar(
        select(func.count(Order.id)).where(
            and_(
                Order.seller_id == seller_id,
                Order.status.in_([OrderStatus.NEW, OrderStatus.ASSEMBLING])
            )
        )
    ) or 0

    # CZ Withdrawals (WITHDRAWN)
    withdrawals_count = await db.scalar(
        select(func.count(Order.id)).where(
            and_(
                Order.seller_id == seller_id,
                Order.kiz_status == KizStatus.WITHDRAWN
            )
        )
    ) or 0

    # KIZ Issues (ERROR)
    issues_count = await db.scalar(
        select(func.count(Order.id)).where(
            and_(
                Order.seller_id == seller_id,
                Order.kiz_status == KizStatus.ERROR
            )
        )
    ) or 0

    return {
        "orders_today": orders_today if orders_today > 0 else total_orders,
        "total_orders": total_orders,
        "pending_assembly": pending_count,
        "withdrawals_success": withdrawals_count,
        "kiz_issues": issues_count
    }


@router.get("", response_model=dict)
async def list_orders(
    seller_id: str,
    status: Optional[str] = None,
    kiz_status: Optional[str] = None,
    q: Optional[str] = Query(None, description="Search term for order ID, article, name or KIZ"),
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=1000),
    db: AsyncSession = Depends(get_db)
):
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")
        
    query = select(Order).where(Order.seller_id == seller_id)
    if status and status != "ALL":
        try:
            query = query.where(Order.status == OrderStatus[status.upper()])
        except KeyError:
            pass
            
    if kiz_status and kiz_status != "ALL":
        try:
            query = query.where(Order.kiz_status == KizStatus[kiz_status.upper()])
        except KeyError:
            pass

    if q:
        q_clean = q.strip()
        conditions = [
            Order.article.ilike(f"%{q_clean}%"),
            Order.name.ilike(f"%{q_clean}%"),
            Order.kiz_code.ilike(f"%{q_clean}%")
        ]
        if q_clean.isdigit():
            conditions.append(Order.id == int(q_clean))
        query = query.where(or_(*conditions))

    if date_from:
        query = query.where(Order.created_at >= date_from)
    if date_to:
        query = query.where(Order.created_at <= date_to)
        
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    
    query = query.offset((page - 1) * page_size).limit(page_size).order_by(Order.created_at.desc())
    result = await db.execute(query)
    orders = result.scalars().all()

    items = []
    for o in orders:
        items.append({
            "id": o.id,
            "seller_id": str(o.seller_id),
            "status": o.status.value,
            "wb_status": o.wb_status,
            "supplier_status": o.supplier_status,
            "wb_created_at": o.wb_created_at.isoformat() if o.wb_created_at else None,
            "article": o.article or f"ART-{o.id}",
            "brand": o.brand or "WB",
            "subject": o.subject or "Товар",
            "name": o.name or f"Заказ #{o.id}",
            "tech_size": o.tech_size,
            "wb_size": o.wb_size,
            "price": str(o.price) if o.price is not None else "0.00",
            "sticker_id": o.sticker_id,
            "kiz_required": o.kiz_required,
            "kiz_code": o.kiz_code,
            "kiz_status": o.kiz_status.value,
            "kiz_cz_status": o.kiz_cz_status,
            "kiz_cz_status_updated_at": o.kiz_cz_status_updated_at.isoformat() if o.kiz_cz_status_updated_at else None,
            "kiz_attached_at": o.kiz_attached_at.isoformat() if o.kiz_attached_at else None,
            "created_at": o.created_at.isoformat() if o.created_at else None,
        })
    
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@router.get("/{order_id}")
async def get_order(seller_id: str, order_id: int, db: AsyncSession = Depends(get_db)):
    order = await db.get(Order, order_id)
    if not order or str(order.seller_id) != str(seller_id):
        raise HTTPException(status_code=404, detail="Order not found")
        
    return {
        "id": order.id,
        "seller_id": str(order.seller_id),
        "status": order.status.value,
        "wb_status": order.wb_status,
        "supplier_status": order.supplier_status,
        "wb_created_at": order.wb_created_at.isoformat() if order.wb_created_at else None,
        "article": order.article or f"ART-{order.id}",
        "brand": order.brand or "WB",
        "subject": order.subject or "Товар",
        "name": order.name or f"Заказ #{order.id}",
        "tech_size": order.tech_size,
        "wb_size": order.wb_size,
        "price": str(order.price) if order.price is not None else "0.00",
        "sticker_id": order.sticker_id,
        "sticker_base64": order.sticker_base64,
        "kiz_required": order.kiz_required,
        "kiz_code": order.kiz_code,
        "kiz_status": order.kiz_status.value,
        "kiz_cz_status": order.kiz_cz_status,
        "kiz_cz_status_updated_at": order.kiz_cz_status_updated_at.isoformat() if order.kiz_cz_status_updated_at else None,
        "kiz_attached_at": order.kiz_attached_at.isoformat() if order.kiz_attached_at else None,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


@router.post("/{order_id}/kiz-check")
async def check_order_kiz_status(seller_id: str, order_id: int, db: AsyncSession = Depends(get_db)):
    """Live verification of KIZ status in ГИС МТ (Честный Знак)."""
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")
    order = await db.get(Order, order_id)
    if not order or str(order.seller_id) != str(seller_id):
        raise HTTPException(status_code=404, detail="Order not found")
    if not order.kiz_code:
        raise HTTPException(status_code=400, detail="У заказа отсутствует прикрепленный КИЗ")
    
    from app.services.cz_client import CZClient
    token = decrypt(seller.cz_token_encrypted) if seller.cz_token_encrypted else None
    
    async with CZClient(inn=seller.cz_inn, token=token, cert_thumbprint=seller.cryptopro_cert_thumbprint or seller.cz_cert_path) as cz:
        try:
            cises_info = await cz.get_cises_info([order.kiz_code])
        except Exception:
            await cz.authenticate()
            cises_info = await cz.get_cises_info([order.kiz_code])
            
        cis_data = cises_info[0].get("cisInfo", {}) if cises_info else {}
        cz_status = cis_data.get("status")
        if cz_status:
            order.kiz_cz_status = cz_status
            order.kiz_cz_status_updated_at = datetime.now(timezone.utc)

            # Check for conflict conditions between order state and CZ mark status
            ogvs = cis_data.get("ogvs") or []
            if ogvs:
                order.kiz_status = KizStatus.ERROR
            elif cz_status in ["RETIRED", "WRITTEN_OFF", "DISAGGREGATED", "KILLED"]:
                if order.kiz_status != KizStatus.WITHDRAWN and order.status != OrderStatus.DELIVERED:
                    order.kiz_status = KizStatus.ERROR
            elif cz_status == "INTRODUCED":
                if order.kiz_status == KizStatus.ATTACHED:
                    order.kiz_status = KizStatus.VALIDATED

            await db.commit()
            
        return {
            "order_id": order.id,
            "kiz_code": order.kiz_code,
            "kiz_status": order.kiz_status.value,
            "kiz_cz_status": cz_status,
            "cis_info": cis_data
        }


@router.post("/{order_id}/cancel")
async def cancel_order(seller_id: str, order_id: int, db: AsyncSession = Depends(get_db)):
    order = await db.get(Order, order_id)
    if not order or str(order.seller_id) != str(seller_id):
        raise HTTPException(status_code=404, detail="Order not found")
        
    order.status = OrderStatus.CANCELLED
    
    audit = AuditLog(
        seller_id=seller_id,
        agent="orders_api",
        action="CANCEL_ORDER",
        entity_type="order",
        entity_id=str(order_id),
        payload={"status": OrderStatus.CANCELLED.value}
    )
    db.add(audit)
    await db.commit()
    return {"message": "Заказ успешно отменен", "order_id": order_id, "status": order.status.value}


@router.post("/{order_id}/mark-assembling")
async def mark_assembling(seller_id: str, order_id: int, db: AsyncSession = Depends(get_db)):
    order = await db.get(Order, order_id)
    if not order or str(order.seller_id) != str(seller_id):
        raise HTTPException(status_code=404, detail="Order not found")
        
    order.status = OrderStatus.ASSEMBLING
    
    audit = AuditLog(
        seller_id=seller_id,
        agent="orders_api",
        action="MARK_ASSEMBLING",
        entity_type="order",
        entity_id=str(order_id),
        payload={"status": OrderStatus.ASSEMBLING.value}
    )
    db.add(audit)
    await db.commit()
    return {"message": "Заказ переведен на сборку", "order_id": order_id, "status": order.status.value}


@router.get("/{order_id}/sticker")
async def get_sticker(seller_id: str, order_id: int, db: AsyncSession = Depends(get_db)):
    order = await db.get(Order, order_id)
    if not order or str(order.seller_id) != str(seller_id):
        raise HTTPException(status_code=404, detail="Order not found")
        
    sticker_id = order.sticker_id or f"{order.id}-ST"
    sample_svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200" viewBox="0 0 300 200">
        <rect width="300" height="200" fill="#ffffff" stroke="#000000" stroke-width="2"/>
        <text x="150" y="30" font-size="18" font-weight="bold" text-anchor="middle" fill="#000">WILDBERRIES FBS</text>
        <line x1="20" y1="45" x2="280" y2="45" stroke="#000" stroke-width="1"/>
        <text x="20" y="70" font-size="14" font-weight="bold" fill="#000">ID: {order.id}</text>
        <text x="20" y="95" font-size="12" fill="#333">Арт: {order.article or 'N/A'}</text>
        <text x="20" y="115" font-size="12" fill="#333">{order.name or 'Товар'}</text>
        <rect x="20" y="135" width="260" height="45" fill="#000000"/>
        <text x="150" y="163" font-size="14" fill="#ffffff" text-anchor="middle" font-family="monospace">*{sticker_id}*</text>
    </svg>'''
    
    return {
        "sticker_id": sticker_id,
        "svg_content": sample_svg,
        "order_id": order_id
    }


@router.post("/sync")
@router.post("/refresh")
async def refresh_orders(seller_id: str, db: AsyncSession = Depends(get_db)):
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")

    token = decrypt(seller.wb_api_token_encrypted)
    if not token:
        raise HTTPException(status_code=400, detail="Токен WB API не настроен для данного продавца")

    client = WBClient(token)
    new_count = 0
    updated_count = 0
    all_raw = {}
    try:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=30)

        # 1. Fetch cards catalog to enrich names and sizes
        catalog = {}
        try:
            catalog = await client.get_cards_catalog(limit=100)
        except Exception as e:
            logger.warning(f"Error fetching cards catalog for seller {seller_id}: {e}")

        # 2. Fetch new unhandled assembly tasks
        try:
            new_orders = await client.get_new_orders()
            if isinstance(new_orders, list):
                for o in new_orders:
                    oid = o.get("id") or o.get("orderId")
                    if oid:
                        all_raw[oid] = o
        except Exception as e:
            logger.warning(f"Error fetching new orders for seller {seller_id}: {e}")

        # 3. Fetch recent orders (last 30 days)
        try:
            recent_orders = await client.get_orders(start, now)
            if isinstance(recent_orders, list):
                for o in recent_orders:
                    oid = o.get("id") or o.get("orderId")
                    if oid and oid not in all_raw:
                        all_raw[oid] = o
        except Exception as e:
            logger.warning(f"Error fetching recent orders for seller {seller_id}: {e}")

        # 4. Fetch orders metadata (SGTIN / KIZ codes)
        meta_by_id = {}
        if all_raw:
            try:
                meta_resp = await client.get_orders_meta(list(all_raw.keys()))
                for om in meta_resp.get("orders", []):
                    om_id = om.get("id")
                    meta_dict = om.get("meta", {})
                    sgtin_val = None
                    if "sgtin" in meta_dict and meta_dict["sgtin"] and meta_dict["sgtin"].get("value"):
                        vals = meta_dict["sgtin"]["value"]
                        if isinstance(vals, list) and vals:
                            sgtin_val = vals[0]
                    if not sgtin_val and "metaDetails" in om:
                        for md in om.get("metaDetails", []):
                            if md.get("key") == "sgtin" and md.get("value"):
                                sgtin_val = md.get("value")
                                break
                    if om_id and sgtin_val:
                        meta_by_id[om_id] = sgtin_val
            except Exception as e:
                logger.warning(f"Error fetching orders meta for seller {seller_id}: {e}")

        # 5. Fetch detailed WB order statuses (POST /api/v3/orders/status)
        wb_statuses_by_id = {}
        if all_raw:
            try:
                all_ids = list(all_raw.keys())
                for i in range(0, len(all_ids), 1000):
                    batch_ids = all_ids[i:i+1000]
                    st_list = await client.get_orders_status(batch_ids)
                    for st in st_list:
                        s_oid = st.get("id")
                        if s_oid:
                            wb_statuses_by_id[s_oid] = {
                                "supplierStatus": st.get("supplierStatus"),
                                "wbStatus": st.get("wbStatus"),
                                "isCancellable": st.get("isCancellable")
                            }
            except Exception as e:
                logger.warning(f"Error fetching WB order statuses for seller {seller_id}: {e}")

        # 6. Fetch and sync WB supplies
        supplies_by_wb_id = {}
        try:
            sup_data = await client.get_supplies(limit=100)
            for s_raw in sup_data.get("supplies", []):
                wb_sup_id = s_raw.get("id")
                if not wb_sup_id:
                    continue
                res_s = await db.execute(
                    select(Supply).where(
                        Supply.seller_id == seller.id,
                        Supply.wb_supply_id == wb_sup_id
                    )
                )
                sup_obj = res_s.scalars().first()
                sup_name = s_raw.get("name") or f"Поставка {wb_sup_id}"
                is_done = s_raw.get("done", False)
                closed_at_str = s_raw.get("closedAt")
                created_at_str = s_raw.get("createdAt")

                c_at = None
                if created_at_str:
                    try:
                        c_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                        if c_at.tzinfo is None:
                            c_at = c_at.replace(tzinfo=timezone.utc)
                    except Exception:
                        pass
                cl_at = None
                if closed_at_str:
                    try:
                        cl_at = datetime.fromisoformat(closed_at_str.replace("Z", "+00:00"))
                        if cl_at.tzinfo is None:
                            cl_at = cl_at.replace(tzinfo=timezone.utc)
                    except Exception:
                        pass

                sup_st = SupplyStatus.DONE if is_done else (SupplyStatus.CLOSED if cl_at else SupplyStatus.CREATED)

                if not sup_obj:
                    sup_obj = Supply(
                        id=uuid.uuid4(),
                        seller_id=str(seller.id),
                        wb_supply_id=wb_sup_id,
                        name=sup_name,
                        status=sup_st,
                        created_at=c_at or now,
                        closed_at=cl_at,
                    )
                    db.add(sup_obj)
                    await db.flush()
                else:
                    sup_obj.status = sup_st
                    sup_obj.closed_at = cl_at

                supplies_by_wb_id[wb_sup_id] = sup_obj
        except Exception as e:
            await db.rollback()
            logger.warning(f"Error syncing supplies for seller {seller_id}: {e}")

        by_chrt = catalog.get("by_chrt_id", {})
        by_vendor = catalog.get("by_vendor_code", {})
        by_nm = catalog.get("by_nm_id", {})

        for oid, raw in all_raw.items():
            existing = await db.get(Order, oid)
            raw_price = raw.get("price", 0)
            price_val = Decimal(str(round(raw_price / 100.0, 2))) if raw_price >= 100 else Decimal(str(raw_price))

            dt_str = raw.get("createdAt")
            try:
                wb_dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00")) if dt_str else now
                if wb_dt.tzinfo is None:
                    wb_dt = wb_dt.replace(tzinfo=timezone.utc)
            except Exception:
                wb_dt = now

            cargo_type = raw.get("cargoType")
            req_meta = str(raw.get("requiredMeta", "")).lower()

            supply_id = raw.get("supplyId")
            status = OrderStatus.DELIVERING if supply_id else OrderStatus.NEW

            chrt_id = raw.get("chrtId")
            nm_id = raw.get("nmId")
            article = raw.get("article") or ""

            # Enrich product name, subject, brand, sizes from catalog
            prod_name = None
            prod_subj = None
            prod_brand = None
            prod_tnved = None
            tech_size = None
            wb_size = None

            if chrt_id and chrt_id in by_chrt:
                cinfo = by_chrt[chrt_id]
                prod_name = cinfo.get("title")
                prod_subj = cinfo.get("subjectName")
                prod_brand = cinfo.get("brand")
                prod_tnved = cinfo.get("tnved")
                tech_size = cinfo.get("techSize")
                wb_size = cinfo.get("wbSize")
            elif article and article in by_vendor:
                cinfo = by_vendor[article]
                prod_name = cinfo.get("title")
                prod_subj = cinfo.get("subjectName")
                prod_brand = cinfo.get("brand")
                prod_tnved = cinfo.get("tnved")
                sizes = cinfo.get("sizes", [])
                for s in sizes:
                    if s.get("chrtID") == chrt_id:
                        tech_size = s.get("techSize")
                        wb_size = s.get("wbSize")
                        break
                if not tech_size and sizes:
                    tech_size = sizes[0].get("techSize")
                    wb_size = sizes[0].get("wbSize")
            elif nm_id and nm_id in by_nm:
                cinfo = by_nm[nm_id]
                prod_name = cinfo.get("title")
                prod_subj = cinfo.get("subjectName")
                prod_brand = cinfo.get("brand")
                prod_tnved = cinfo.get("tnved")
                sizes = cinfo.get("sizes", [])
                for s in sizes:
                    if s.get("chrtID") == chrt_id:
                        tech_size = s.get("techSize")
                        wb_size = s.get("wbSize")
                        break
                if not tech_size and sizes:
                    tech_size = sizes[0].get("techSize")
                    wb_size = sizes[0].get("wbSize")

            final_name = prod_name or raw.get("name") or (f"{article} (WB #{oid})" if article else f"Заказ #{oid}")
            final_subj = prod_subj or raw.get("subject") or "Товар"
            final_brand = prod_brand or raw.get("brand") or seller.name or "WB"

            # Determine whether KIZ is required
            kiz_req = is_kiz_required(
                subject=final_subj,
                tnved=prod_tnved,
                order_raw=raw
            )

            attached_sgtin = meta_by_id.get(oid)
            final_kiz_code = attached_sgtin or (existing.kiz_code if existing else None)

            if final_kiz_code:
                kiz_req = True
                final_kiz_status = KizStatus.ATTACHED
            elif kiz_req:
                final_kiz_status = KizStatus.PENDING
            else:
                final_kiz_status = KizStatus.NOT_REQUIRED

            # Detailed WB statuses
            st_info = wb_statuses_by_id.get(oid, {})
            wb_st = st_info.get("wbStatus") or raw.get("wbStatus")
            supp_st = st_info.get("supplierStatus") or raw.get("supplierStatus")

            if wb_st == "sold":
                status = OrderStatus.DELIVERED
            elif wb_st in ["canceled", "canceled_by_client", "declined_by_client", "defect"]:
                status = OrderStatus.CANCELLED
            elif wb_st in ["sorted", "ready_for_pickup"] or supply_id:
                status = OrderStatus.DELIVERING
            elif supp_st == "confirm":
                status = OrderStatus.ASSEMBLING

            # Find matching supply record in DB if supply_id present
            matched_supply = supplies_by_wb_id.get(supply_id)
            target_supply_uuid = matched_supply.id if matched_supply else None

            if not existing:
                order = Order(
                    id=oid,
                    seller_id=seller.id,
                    status=status,
                    wb_status=wb_st,
                    supplier_status=supp_st,
                    wb_created_at=wb_dt,
                    supply_id=target_supply_uuid,
                    wb_supply_id=supply_id,
                    chrt_id=chrt_id,
                    nm_id=nm_id,
                    article=article,
                    brand=final_brand,
                    subject=final_subj,
                    name=final_name,
                    tech_size=tech_size,
                    wb_size=wb_size,
                    price=price_val,
                    sticker_id=f"{oid}-ST",
                    kiz_required=kiz_req,
                    kiz_code=final_kiz_code,
                    kiz_status=final_kiz_status,
                    kiz_attached_at=now if final_kiz_code else None,
                )
                db.add(order)
                new_count += 1
            else:
                updated = False
                if wb_st and existing.wb_status != wb_st:
                    existing.wb_status = wb_st
                    updated = True
                if supp_st and existing.supplier_status != supp_st:
                    existing.supplier_status = supp_st
                    updated = True
                if wb_st == "sold" and existing.status != OrderStatus.DELIVERED:
                    existing.status = OrderStatus.DELIVERED
                    updated = True
                    # Auto-trigger CZ withdrawal when order is confirmed sold
                    if existing.kiz_code and existing.kiz_status == KizStatus.ATTACHED:
                        try:
                            from app.agents.cz_withdrawal import withdraw_order_kiz
                            price_kop = int((existing.price or 0) * 100)
                            withdraw_order_kiz.delay(
                                seller_id=str(seller.id),
                                order_id=existing.id,
                                kiz_code=existing.kiz_code,
                                price_kopecks=price_kop,
                            )
                        except Exception as e:
                            logger.warning(f"Could not dispatch withdraw_order_kiz for {existing.id}: {e}")
                elif wb_st in ["canceled", "canceled_by_client", "declined_by_client", "defect"] and existing.status != OrderStatus.CANCELLED:
                    existing.status = OrderStatus.CANCELLED
                    updated = True
                    # Auto-trigger CZ return if mark was previously withdrawn
                    if existing.kiz_code and existing.kiz_status == KizStatus.WITHDRAWN:
                        try:
                            from app.agents.cz_return import return_order_kiz
                            return_order_kiz.delay(
                                seller_id=str(seller.id),
                                order_id=existing.id,
                                kiz_code=existing.kiz_code,
                            )
                        except Exception as e:
                            logger.warning(f"Could not dispatch return_order_kiz for {existing.id}: {e}")
                elif wb_st in ["sorted", "ready_for_pickup", "waiting"] and existing.status != OrderStatus.DELIVERING:
                    existing.status = OrderStatus.DELIVERING
                    updated = True
                if supply_id and existing.wb_supply_id != supply_id:
                    existing.wb_supply_id = supply_id
                    updated = True
                if target_supply_uuid and existing.supply_id != target_supply_uuid:
                    existing.supply_id = target_supply_uuid
                    updated = True
                if supply_id and existing.status == OrderStatus.NEW:
                    existing.status = OrderStatus.DELIVERING
                    updated = True
                if prod_name and existing.name != prod_name:
                    existing.name = prod_name
                    updated = True
                if tech_size and existing.tech_size != tech_size:
                    existing.tech_size = tech_size
                    updated = True
                if wb_size and existing.wb_size != wb_size:
                    existing.wb_size = wb_size
                    updated = True
                if prod_subj and existing.subject != prod_subj:
                    existing.subject = prod_subj
                    updated = True
                if prod_brand and existing.brand != prod_brand:
                    existing.brand = prod_brand
                    updated = True
                if final_kiz_code and existing.kiz_code != final_kiz_code:
                    existing.kiz_code = final_kiz_code
                    existing.kiz_required = True
                    existing.kiz_status = KizStatus.ATTACHED
                    existing.kiz_attached_at = existing.kiz_attached_at or now
                    updated = True
                elif existing.kiz_code and wb_st != "sold" and existing.kiz_status == KizStatus.WITHDRAWN:
                    existing.kiz_status = KizStatus.ATTACHED
                    updated = True
                elif existing.kiz_required != kiz_req:
                    existing.kiz_required = kiz_req
                    if not existing.kiz_code:
                        existing.kiz_status = KizStatus.PENDING if kiz_req else KizStatus.NOT_REQUIRED
                    updated = True
                elif not existing.kiz_code and kiz_req and existing.kiz_status == KizStatus.NOT_REQUIRED:
                    existing.kiz_status = KizStatus.PENDING
                    updated = True

                if updated:
                    updated_count += 1

        # Populate / sync KizProductInfo for all orders with attached KIZ
        from app.services.kiz_service import resolve_kiz_product_info
        for oid, raw in all_raw.items():
            kcode = meta_by_id.get(oid)
            if kcode:
                cur_order = await db.get(Order, oid)
                try:
                    kinfo = await resolve_kiz_product_info(
                        kiz_code=kcode,
                        seller=seller,
                        order=cur_order,
                        db=db,
                        force_refresh=False
                    )
                    if kinfo and kinfo.cz_status and cur_order:
                        cur_order.kiz_cz_status = kinfo.cz_status
                        cur_order.kiz_cz_status_updated_at = cur_order.kiz_cz_status_updated_at or now
                except Exception as e:
                    logger.debug(f"Error persisting kiz_product_info for order {oid}: {e}")

        audit = AuditLog(
            seller_id=seller_id,
            agent="orders_api",
            action="SYNC_ORDERS",
            entity_type="seller",
            entity_id=str(seller_id),
            payload={
                "message": f"Синхронизация завершена: +{new_count} новых, {updated_count} обновлено",
                "new_count": new_count,
                "updated_count": updated_count,
                "total_fetched": len(all_raw),
            }
        )
        db.add(audit)
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"Sync error for seller {seller_id}: {e}")
        raise HTTPException(status_code=502, detail=f"Ошибка синхронизации с WB API: {str(e)}")
    finally:
        await client.close()

    return {
        "success": True,
        "message": f"Синхронизация с WB завершена: получено {len(all_raw)} заказов (+{new_count} новых, {updated_count} обновлено)",
        "seller_id": seller_id,
        "new_count": new_count,
        "updated_count": updated_count,
        "total_fetched": len(all_raw)
    }
