import asyncio
import base64
import logging
from typing import List, Optional
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, or_, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.seller import Seller
from app.national_catalog.models import ProductCard
from app.national_catalog.schemas import (
    ProductCardCreateRequest,
    ProductCardUpdateRequest,
    ProductCardResponse,
    PrepareSignResponse,
    SubmitSignRequest,
    SyncNKResponse,
    ProductCardBatchCreateRequest,
    ProductCardBatchResponse,
    BatchGtinResponse,
)
from app.national_catalog.client import NKClient, NKAPIError
from app.services.encryption import decrypt

logger = logging.getLogger(__name__)

router = APIRouter(tags=["National Catalog"])


async def _get_seller_or_404(seller_id: str, db: AsyncSession) -> Seller:
    stmt = select(Seller).where(Seller.id == seller_id)
    result = await db.execute(stmt)
    seller = result.scalar_one_or_none()
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")
    return seller


async def _get_seller_nk_client(seller: Seller) -> NKClient:
    if not seller.cz_inn:
        raise HTTPException(
            status_code=400,
            detail="У продавца не указан ИНН Честного Знака. Заполните ИНН в настройках продавца."
        )

    token = None
    if seller.cz_token_encrypted:
        try:
            token = decrypt(seller.cz_token_encrypted)
        except Exception as e:
            logger.error("Ошибка расшифровки токена ЧЗ для продавца %s: %s", seller.id, e)

    if not token:
        raise HTTPException(
            status_code=400,
            detail="Отсутствует токен авторизации Честного Знака. Выполните вход по УКЭП в дашборде."
        )

    return NKClient(token=token, inn=seller.cz_inn)


# ==================== CRUD и работа с карточками ====================


@router.get("/sellers/{seller_id}/national-catalog/products", response_model=List[ProductCardResponse])
async def list_products(
    seller_id: str,
    status: Optional[str] = Query(None, description="Фильтр по статусу карточки"),
    search: Optional[str] = Query(None, description="Поиск по GTIN, наименованию или бренду"),
    limit: int = Query(1000, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Получение списка карточек товаров для указанного продавца."""
    await _get_seller_or_404(seller_id, db)

    stmt = select(ProductCard).where(ProductCard.seller_id == seller_id)

    if status and status != "ALL":
        stmt = stmt.where(ProductCard.status == status)

    if search and search.strip():
        q = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                ProductCard.gtin.ilike(q),
                ProductCard.name.ilike(q),
                ProductCard.brand.ilike(q),
                ProductCard.tnved.ilike(q),
            )
        )

    stmt = stmt.order_by(desc(ProductCard.created_at)).offset(offset).limit(limit)
    result = await db.execute(stmt)
    cards = list(result.scalars().all())

    # Build Pydantic responses eagerly while inside session context
    response_items = []
    for c in cards:
        pydantic_item = ProductCardResponse.model_validate(c)
        if not pydantic_item.tnved:
            fallback = _extract_tnved_from_attrs(c.attributes)
            if fallback:
                pydantic_item.tnved = fallback
        response_items.append(pydantic_item)

    return response_items


def _extract_tnved_from_attrs(attrs: Optional[list]) -> Optional[str]:
    """Извлечение кода ТН ВЭД из атрибутов карточки (13933, 10609, 3959)."""
    if not attrs or not isinstance(attrs, list):
        return None
    for a in attrs:
        if isinstance(a, dict):
            aid = str(a.get("attr_id"))
            val = str(a.get("attr_value") or "").strip()
            if val and aid in ("13933", "10609", "3959"):
                return val[:20]
    return None


def _auto_fill_tnved_from_attrs(card: ProductCard) -> bool:
    """Извлечение кода ТН ВЭД из атрибутов карточки (13933, 10609, 3959), если поле tnved пусто."""
    if card.tnved and card.tnved.strip():
        return False
    val = _extract_tnved_from_attrs(card.attributes)
    if val:
        card.tnved = val
        return True
    return False


def _extract_feed_errors(feed_data: dict, gtin: Optional[str] = None) -> list:
    """
    Универсальное извлечение списка ошибок фида из ответа Честного Знака.
    Поддерживает форматы:
    - result: {"0": [...], "1": [...], "totalErrors": 3}
    - item / items: [{"gtin": "...", "message": "...", "status_message": "..."}]
    - error_details: {"items": [...], "commonError": {...}}
    - commonError: {"code": ..., "text": "..."}
    """
    errors = []
    if not isinstance(feed_data, dict):
        return errors

    clean_gtin = str(gtin).strip() if gtin else ""
    gtin_no_zero = clean_gtin.lstrip("0") if clean_gtin else ""

    # 1. Формат result: {"0": ["..."], "1": ["..."], "totalErrors": "3"}
    res_obj = feed_data.get("result")
    if isinstance(res_obj, dict):
        matched_for_gtin = []
        all_res_errors = []
        for k, v in res_obj.items():
            if k == "totalErrors":
                continue
            err_list = v if isinstance(v, list) else [v]
            for err in err_list:
                err_text = str(err)
                all_res_errors.append(err_text)
                if clean_gtin and (clean_gtin in err_text or (gtin_no_zero and gtin_no_zero in err_text)):
                    matched_for_gtin.append(err_text)
        if matched_for_gtin:
            return matched_for_gtin
        if not clean_gtin and all_res_errors:
            return all_res_errors

    # 2. Формат item: [...]
    item_list = feed_data.get("item") or feed_data.get("items") or []
    if isinstance(item_list, list):
        for it in item_list:
            if isinstance(it, dict):
                it_gtin = str(it.get("gtin") or "").strip()
                msg = it.get("message") or it.get("error_message") or it.get("status_message") or it.get("text")
                if not msg and it.get("status_code") and it.get("status_code") not in (0, 1, 2, 3):
                    msg = f"Код ошибки {it['status_code']}"
                if msg:
                    text = f"{it.get('attribute_name')}: {msg}" if it.get("attribute_name") else str(msg)
                    if clean_gtin:
                        if it_gtin and (it_gtin == clean_gtin or it_gtin == gtin_no_zero):
                            errors.append(text)
                    else:
                        errors.append(text)

    # 3. Формат error_details
    err_details = feed_data.get("error_details")
    if isinstance(err_details, dict):
        ce = err_details.get("commonError")
        if isinstance(ce, dict) and ce.get("text"):
            errors.append(str(ce["text"]))
        ed_items = err_details.get("items") or []
        if isinstance(ed_items, list):
            for it in ed_items:
                if isinstance(it, dict):
                    it_gtin = str(it.get("gtin") or "").strip()
                    sub_errors = it.get("errors") or []
                    if isinstance(sub_errors, dict):
                        sub_errors = [sub_errors]
                    for se in sub_errors:
                        msg = se.get("text") or se.get("message") if isinstance(se, dict) else str(se)
                        if msg:
                            if not clean_gtin or (it_gtin in (clean_gtin, gtin_no_zero)):
                                errors.append(str(msg))

    # 4. Корневой commonError
    root_ce = feed_data.get("commonError")
    if isinstance(root_ce, dict) and root_ce.get("text"):
        errors.append(str(root_ce["text"]))

    # 5. Одиночный error_message / message
    if not errors and feed_data.get("error_message"):
        errors.append(str(feed_data["error_message"]))

    # Если для конкретного GTIN ничего точечно не найдено, но в result были общие ошибки
    if not errors and isinstance(res_obj, dict):
        for k, v in res_obj.items():
            if k != "totalErrors":
                err_list = v if isinstance(v, list) else [v]
                errors.extend([str(e) for e in err_list])

    return list(dict.fromkeys(errors))


def _build_goods_item(payload: ProductCardCreateRequest) -> tuple[dict, Optional[str], Optional[str]]:
    gtin_val = payload.gtin.strip() if payload.gtin else None
    if not payload.is_tech_gtin and not gtin_val:
        raise HTTPException(
            status_code=400,
            detail=f"Для товара '{payload.name}' укажите GTIN (14 цифр) или выберите признак технической карточки (029)."
        )

    # Валидация и извлечение ТН ВЭД (из payload или из атрибутов 13933 / 10609 / 3959)
    tnved_val = payload.tnved.strip() if payload.tnved else None
    if not tnved_val and payload.attributes:
        for attr in payload.attributes:
            if attr.attr_id in (13933, 10609, 3959) and attr.attr_value:
                cand = str(attr.attr_value).strip()
                if cand:
                    tnved_val = cand
                    break

    if payload.moderation and not tnved_val:
        raise HTTPException(
            status_code=400,
            detail=f"Для товара '{payload.name}' обязательно укажите код ТН ВЭД (10 знаков, например: 6206300000)."
        )

    goods_item: dict = {
        "good_name": payload.name,
        "moderation": payload.moderation,
        "is_set": payload.is_set,
        "good_attrs": [],
        "good_images": [],
    }
    if tnved_val:
        goods_item["tnved"] = tnved_val

    if payload.brand:
        goods_item["brand"] = payload.brand
    if payload.category_id:
        goods_item["categories"] = [payload.category_id]

    if payload.is_tech_gtin:
        goods_item["is_tech_gtin"] = 1
    elif gtin_val:
        if len(gtin_val) == 13 and gtin_val.isdigit():
            gtin_val = f"0{gtin_val}"
        goods_item["gtin"] = gtin_val
        goods_item["identified_by"] = [
            {
                "value": gtin_val,
                "type": "gtin",
                "multiplier": 1,
                "level": "trade-unit",
                "unit": "шт"
            }
        ]

    has_tnved_attr = False
    for attr in payload.attributes:
        attr_dict = {
            "attr_id": attr.attr_id,
            "attr_value": attr.attr_value,
        }
        if attr.attr_id in (10609, 13933):
            has_tnved_attr = True
        if attr.attr_value_type:
            attr_dict["attr_value_type"] = attr.attr_value_type
        if attr.attr_value_id:
            attr_dict["attr_value_id"] = attr.attr_value_id
        goods_item["good_attrs"].append(attr_dict)

    if not has_tnved_attr and tnved_val:
        goods_item["good_attrs"].append({
            "attr_id": 10609,
            "attr_value": tnved_val,
        })

    for img in payload.images:
        img_dict = {
            "photo_type": img.photo_type or "default",
            "photo_url": img.photo_url,
            "photo_date": img.photo_date or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        goods_item["good_images"].append(img_dict)

    return goods_item, gtin_val, tnved_val


@router.post("/sellers/{seller_id}/national-catalog/products", response_model=ProductCardResponse)
async def create_product(
    seller_id: str,
    payload: ProductCardCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Создание новой карточки товара и отправка реального пакета (фида) в Честный Знак.
    """
    seller = await _get_seller_or_404(seller_id, db)
    client = await _get_seller_nk_client(seller)

    goods_item, gtin_val, tnved_val = _build_goods_item(payload)

    # Отправка фида в Национальный каталог
    try:
        async with client:
            feed_id = await client.create_or_update_feed([goods_item])
    except NKAPIError as e:
        logger.warning("Ошибка отправки фида в НКТ: %s", e)
        raise HTTPException(status_code=e.status_code or 400, detail=e.message)

    card = ProductCard(
        seller_id=seller.id,
        gtin=gtin_val,
        name=payload.name,
        brand=payload.brand,
        tnved=tnved_val,
        category_id=payload.category_id,
        category_name=payload.category_name,
        is_tech_gtin=payload.is_tech_gtin,
        is_set=payload.is_set,
        status="moderation" if payload.moderation else "draft",
        feed_id=feed_id,
        feed_status="Received",
        attributes=[a.model_dump() for a in payload.attributes],
        images=[i.model_dump() for i in payload.images],
    )
    db.add(card)
    await db.commit()
    await db.refresh(card)
    return card


@router.post("/sellers/{seller_id}/national-catalog/products/batch", response_model=ProductCardBatchResponse)
async def create_products_batch(
    seller_id: str,
    payload: ProductCardBatchCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Массовое создание серии карточек товаров (размерно-цветовой матрицы) в НКТ.
    Все товары передаются в True API единым пакетом (фидом) через POST /nk/feed.
    """
    if not payload.items:
        raise HTTPException(status_code=400, detail="Список создаваемых карточек не может быть пустым.")

    seller = await _get_seller_or_404(seller_id, db)
    client = await _get_seller_nk_client(seller)

    goods_items = []
    normalized_gtins = []
    normalized_tnveds = []

    for item in payload.items:
        item.moderation = payload.moderation
        g_dict, gtin_norm, tnved_norm = _build_goods_item(item)
        goods_items.append(g_dict)
        normalized_gtins.append(gtin_norm)
        normalized_tnveds.append(tnved_norm)

    try:
        async with client:
            feed_id = await client.create_or_update_feed(goods_items)
    except NKAPIError as e:
        logger.warning("Ошибка пакетной отправки фида в НКТ: %s", e)
        raise HTTPException(status_code=e.status_code or 400, detail=e.message)

    created_cards = []
    for item, gtin_val, tnved_val in zip(payload.items, normalized_gtins, normalized_tnveds):
        card = ProductCard(
            seller_id=seller.id,
            gtin=gtin_val,
            name=item.name,
            brand=item.brand,
            tnved=tnved_val,
            category_id=item.category_id,
            category_name=item.category_name,
            is_tech_gtin=item.is_tech_gtin,
            is_set=item.is_set,
            status="moderation" if payload.moderation else "draft",
            feed_id=feed_id,
            feed_status="Received",
            attributes=[a.model_dump() for a in item.attributes],
            images=[i.model_dump() for i in item.images],
        )
        db.add(card)
        created_cards.append(card)

    await db.commit()
    for card in created_cards:
        await db.refresh(card)

    return ProductCardBatchResponse(
        success=True,
        feed_id=feed_id,
        created_count=len(created_cards),
        cards=created_cards,
        message=f"Успешно создано {len(created_cards)} карточек товаров (фид #{feed_id})"
    )


@router.get("/sellers/{seller_id}/national-catalog/products/{product_id}", response_model=ProductCardResponse)
async def get_product(
    seller_id: str,
    product_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Получение одной карточки товара."""
    await _get_seller_or_404(seller_id, db)
    stmt = select(ProductCard).where(
        ProductCard.id == product_id,
        ProductCard.seller_id == seller_id
    )
    card = (await db.execute(stmt)).scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Карточка товара не найдена")
    resp = ProductCardResponse.model_validate(card)
    if not resp.tnved:
        resp.tnved = _extract_tnved_from_attrs(card.attributes)
    return resp


@router.put("/sellers/{seller_id}/national-catalog/products/{product_id}", response_model=ProductCardResponse)
async def update_product(
    seller_id: str,
    product_id: str,
    payload: ProductCardUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Редактирование карточки товара с отправкой обновления в Честный Знак.
    """
    seller = await _get_seller_or_404(seller_id, db)
    stmt = select(ProductCard).where(
        ProductCard.id == product_id,
        ProductCard.seller_id == seller_id
    )
    card = (await db.execute(stmt)).scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Карточка товара не найдена")

    client = await _get_seller_nk_client(seller)

    # Формируем тело обновления для POST /nk/feed
    update_item: dict = {
        "moderation": payload.moderation,
    }
    if card.good_id:
        update_item["good_id"] = card.good_id
    if card.gtin:
        update_item["gtin"] = card.gtin
    if payload.name:
        update_item["good_name"] = payload.name

    if payload.attributes is not None:
        attrs_payload = []
        for attr in payload.attributes:
            ad = {
                "attr_id": attr.attr_id,
                "attr_value": attr.attr_value,
            }
            if attr.attr_value_id:
                ad["attr_value_id"] = attr.attr_value_id
            if attr.attr_value_type:
                ad["attr_value_type"] = attr.attr_value_type
            if attr.delete:
                ad["delete"] = 1
            attrs_payload.append(ad)
        update_item["good_attrs"] = attrs_payload

    if payload.images is not None:
        update_item["good_images"] = [
            {
                "photo_type": img.photo_type or "default",
                "photo_url": img.photo_url,
                "photo_date": img.photo_date or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            }
            for img in payload.images
        ]

    # Отправляем реальное обновление в ЧЗ
    try:
        async with client:
            feed_id = await client.create_or_update_feed([update_item])
    except NKAPIError as e:
        logger.warning("Ошибка отправки обновления в НКТ: %s", e)
        raise HTTPException(status_code=e.status_code or 400, detail=e.message)

    # Обновляем поля в БД
    if payload.name:
        card.name = payload.name
    if payload.brand:
        card.brand = payload.brand
    if payload.tnved:
        card.tnved = payload.tnved
    if payload.category_id is not None:
        card.category_id = payload.category_id
    if payload.category_name is not None:
        card.category_name = payload.category_name
    if payload.attributes is not None:
        card.attributes = [a.model_dump() for a in payload.attributes]
    if payload.images is not None:
        card.images = [i.model_dump() for i in payload.images]

    card.feed_id = feed_id
    card.feed_status = "Processing"
    if payload.moderation:
        card.status = "moderation"
    card.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(card)
    return card


@router.delete("/sellers/{seller_id}/national-catalog/products/{product_id}")
async def delete_product(
    seller_id: str,
    product_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Удаление карточки из базы (если она не опубликована)."""
    await _get_seller_or_404(seller_id, db)
    stmt = select(ProductCard).where(
        ProductCard.id == product_id,
        ProductCard.seller_id == seller_id
    )
    card = (await db.execute(stmt)).scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Карточка товара не найдена")

    if card.status == "published":
        raise HTTPException(status_code=400, detail="Нельзя удалить опубликованную карточку товара")

    await db.delete(card)
    await db.commit()
    return {"success": True, "message": "Карточка удалена"}


# ==================== Синхронизация с НКТ ====================


@router.post("/sellers/{seller_id}/national-catalog/sync-nk", response_model=SyncNKResponse)
async def sync_products_from_nk(
    seller_id: str,
    all_pages: bool = Query(True, description="Синхронизировать все страницы товаров из НКТ"),
    max_limit: int = Query(1000, ge=1, le=5000, description="Максимальное количество товаров"),
    force_refresh: bool = Query(False, description="Принудительно перезагрузить детали всех карточек"),
    db: AsyncSession = Depends(get_db),
):
    """
    Полная синхронизация карточек товаров продавца из Национального Каталога (НКТ) True API.
    Запрашивает список зарегистрированных товаров через GET /nk/etagslist, получает детали каждой
    карточки и выполняет сохранение (upsert) в локальную базу данных.
    """
    seller = await _get_seller_or_404(seller_id, db)
    client = await _get_seller_nk_client(seller)

    # Загружаем существующие карточки продавца
    stmt = select(ProductCard).where(ProductCard.seller_id == seller_id)
    existing_cards = (await db.execute(stmt)).scalars().all()
    existing_by_good_id = {c.good_id: c for c in existing_cards if c.good_id is not None}
    existing_by_gtin = {c.gtin: c for c in existing_cards if c.gtin}

    all_remote_goods = []
    total_remote = 0

    try:
        async with client:
            offset = 0
            while True:
                etags_resp = await client.get_etags_list(offset=offset)
                goods = etags_resp.get("goods", [])
                if not goods:
                    break
                all_remote_goods.extend(goods)
                offset += len(goods)
                total_remote = etags_resp.get("total", len(all_remote_goods))
                if not all_pages or offset >= total_remote or len(all_remote_goods) >= max_limit:
                    break
                await asyncio.sleep(0.3)

            # 1. Проверяем статусы фидов для всех локальных незавершенных карточек продавца
            unfinished_cards = [c for c in existing_cards if c.feed_id and c.status not in ("published", "archived") and not c.good_id]
            unique_feed_ids = {c.feed_id for c in unfinished_cards}
            feed_results = {}
            for fid in unique_feed_ids:
                try:
                    st_data = await client.get_feed_status(fid)
                    feed_results[fid] = st_data
                except Exception as e:
                    logger.warning("Ошибка проверки фида %s при синхронизации: %s", fid, e)

            feeds_updated_count = 0
            for c in unfinished_cards:
                if c.feed_id in feed_results:
                    st_data = feed_results[c.feed_id]
                    st_str = st_data.get("status")
                    if st_str:
                        c.feed_status = st_str
                    c_errors = _extract_feed_errors(st_data, gtin=c.gtin)
                    if c_errors:
                        c.error_details = c_errors
                        c.status = "errors"
                    elif st_str in ("Rejected", "errors"):
                        c.status = "errors"
                        c.error_details = _extract_feed_errors(st_data)
                    elif st_str == "Moderated":
                        c.status = "notsigned"
                    elif st_str == "Signed":
                        c.status = "published"
                    _auto_fill_tnved_from_attrs(c)
                    c.updated_at = datetime.now(timezone.utc)
                    feeds_updated_count += 1

            if feed_results:
                await db.commit()

            if not all_remote_goods:
                return SyncNKResponse(
                    success=True,
                    total_remote=total_remote,
                    synced_count=len(existing_cards),
                    created_count=0,
                    updated_count=feeds_updated_count,
                    message="В Национальном каталоге не найдено опубликованных карточек товаров для данного ИНН. Статусы отправленных пакетов (фидов) обновлены.",
                )

            # Определяем товары для детального запроса:
            # Если force_refresh=False: запрашиваем только новые товары или товары не в статусе 'published'
            if force_refresh:
                goods_to_fetch = all_remote_goods[:150]
            else:
                goods_to_fetch = []
                for g in all_remote_goods:
                    gid = g.get("good_id")
                    if not gid:
                        continue
                    c = existing_by_good_id.get(int(gid))
                    if not c or c.status in ("draft", "moderation", "notsigned", "errors", "rejected"):
                        goods_to_fetch.append(g)

            logger.info("НКТ Синхронизация: всего в каталоге %d, требуется загрузить деталей: %d", len(all_remote_goods), len(goods_to_fetch))

            # Получаем детальные описания карточек параллельно (с семафором 5)
            sem = asyncio.Semaphore(5)

            async def fetch_detail(good_item: dict):
                gid = good_item.get("good_id")
                if not gid:
                    return None
                try:
                    async with sem:
                        res = await client.get_feed_product(good_id=gid)
                        return res[0] if res else None
                except Exception as exc:
                    logger.warning("Ошибка получения деталей товара good_id %s: %s", gid, exc)
                    return None

            details = await asyncio.gather(*(fetch_detail(g) for g in goods_to_fetch)) if goods_to_fetch else []

    except NKAPIError as e:
        logger.error("Ошибка синхронизации с НКТ для продавца %s: %s", seller.id, e)
        raise HTTPException(status_code=e.status_code or 400, detail=e.message)

    created_count = 0
    updated_count = 0

    for p in details:
        if not p or not isinstance(p, dict):
            continue

        good_id_raw = p.get("good_id")
        if not good_id_raw:
            continue
        try:
            good_id = int(good_id_raw)
        except Exception:
            continue

        # Извлечение GTIN
        gtin_val = None
        for id_item in p.get("identified_by", []):
            if isinstance(id_item, dict) and id_item.get("type") == "gtin":
                val = str(id_item.get("value", "")).strip()
                if val:
                    gtin_val = val
                    break
        if not gtin_val and p.get("gtin"):
            gtin_val = str(p["gtin"]).strip()
        if gtin_val and len(gtin_val) == 13 and gtin_val.isdigit():
            gtin_val = f"0{gtin_val}"

        # Извлечение категорий
        cat_id = None
        cat_name = None
        categories = p.get("categories", [])
        if categories and isinstance(categories, list):
            first_cat = categories[0]
            if isinstance(first_cat, dict):
                cat_id = first_cat.get("cat_id")
                cat_name = first_cat.get("cat_name")

        # Извлечение торговой марки
        brand_val = p.get("brand_name") or p.get("brand")

        # Извлечение ТН ВЭД
        tnved_val = p.get("tnved")
        attrs = p.get("good_attrs") or []
        if not tnved_val and attrs:
            for a in attrs:
                if isinstance(a, dict):
                    aid = a.get("attr_id")
                    if aid in (10609, "10609") or "ТН ВЭД" in str(a.get("attr_name", "")):
                        tnved_val = a.get("attr_value")
                        break

        # Нормализация статуса
        st_raw = str(p.get("good_status") or "draft").lower()
        valid_statuses = {"draft", "moderation", "notsigned", "published", "errors", "rejected", "archived"}
        status_val = st_raw if st_raw in valid_statuses else "draft"

        name_val = (p.get("good_name") or "Без названия")[:255]
        brand_str = str(brand_val)[:255] if brand_val else None
        tnved_str = str(tnved_val)[:20] if tnved_val else None
        cat_name_str = str(cat_name)[:255] if cat_name else None

        card = existing_by_good_id.get(good_id)
        if not card and gtin_val:
            card = existing_by_gtin.get(gtin_val)

        if card:
            card.good_id = good_id
            if gtin_val:
                card.gtin = gtin_val
            card.name = name_val
            card.brand = brand_str
            card.tnved = tnved_str
            if cat_id:
                card.category_id = cat_id
            if cat_name_str:
                card.category_name = cat_name_str
            card.status = status_val
            if p.get("good_mark_flag") is not None:
                card.good_mark_flag = bool(p["good_mark_flag"])
            if p.get("good_turn_flag") is not None:
                card.good_turn_flag = bool(p["good_turn_flag"])
            if p.get("is_tech_gtin") is not None:
                card.is_tech_gtin = bool(p["is_tech_gtin"])
            if p.get("is_set") is not None:
                card.is_set = bool(p["is_set"])
            if attrs:
                card.attributes = attrs
            if p.get("good_images"):
                card.images = p["good_images"]
            card.updated_at = datetime.now(timezone.utc)
            updated_count += 1
        else:
            card = ProductCard(
                seller_id=seller.id,
                good_id=good_id,
                gtin=gtin_val,
                name=name_val,
                brand=brand_str,
                tnved=tnved_str,
                category_id=cat_id,
                category_name=cat_name_str,
                status=status_val,
                good_mark_flag=bool(p.get("good_mark_flag", False)),
                good_turn_flag=bool(p.get("good_turn_flag", False)),
                is_tech_gtin=bool(p.get("is_tech_gtin", False)),
                is_set=bool(p.get("is_set", False)),
                attributes=attrs,
                images=p.get("good_images") or [],
            )
            db.add(card)
            created_count += 1
            existing_by_good_id[good_id] = card
            if gtin_val:
                existing_by_gtin[gtin_val] = card

    await db.commit()

    total_in_db = len(existing_by_good_id)
    return SyncNKResponse(
        success=True,
        total_remote=total_remote,
        synced_count=total_in_db,
        created_count=created_count,
        updated_count=updated_count,
        message=f"Синхронизация завершена. В базе {total_in_db} карточек (новых загружено: {created_count}, обновлено: {updated_count})"
    )


# ==================== Проверка статуса, модерация, подписание ====================


@router.post("/sellers/{seller_id}/national-catalog/products/{product_id}/check-status", response_model=ProductCardResponse)
async def check_product_status(
    seller_id: str,
    product_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Запрос актуального статуса обработки фида и карточки из Национального каталога.
    """
    seller = await _get_seller_or_404(seller_id, db)
    stmt = select(ProductCard).where(
        ProductCard.id == product_id,
        ProductCard.seller_id == seller_id
    )
    card = (await db.execute(stmt)).scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Карточка товара не найдена")

    client = await _get_seller_nk_client(seller)

    async with client:
        # 1. Проверяем статус последнего фида если он есть
        if card.feed_id:
            try:
                feed_data = await client.get_feed_status(card.feed_id)
                status_str = feed_data.get("status")

                # Находим все карточки продавца с этим же feed_id (вся серия/пакет)
                sibling_stmt = select(ProductCard).where(
                    ProductCard.seller_id == seller_id,
                    ProductCard.feed_id == card.feed_id
                )
                sibling_cards = (await db.execute(sibling_stmt)).scalars().all()

                item_list = feed_data.get("item") or feed_data.get("items") or []

                for sc in sibling_cards:
                    if status_str:
                        sc.feed_status = status_str

                    # Извлекаем ошибки конкретно для sc.gtin
                    sc_errors = _extract_feed_errors(feed_data, gtin=sc.gtin)
                    if sc_errors:
                        sc.error_details = sc_errors
                        sc.status = "errors"
                    elif status_str in ("Rejected", "errors"):
                        sc.status = "errors"
                        # Если конкретных ошибок для GTIN нет, берем общие ошибки фида
                        gen_errors = _extract_feed_errors(feed_data)
                        if gen_errors:
                            sc.error_details = gen_errors

                    if status_str in ("Moderated", "Signed"):
                        sc.status = "notsigned" if status_str == "Moderated" else "published"
                        # Извлекаем good_id строго для соответствующего GTIN
                        for it in item_list:
                            if isinstance(it, dict) and it.get("good_id"):
                                it_gtin = str(it.get("gtin") or "").strip()
                                sc_gtin = str(sc.gtin or "").strip()
                                if not it_gtin or it_gtin == sc_gtin or it_gtin.lstrip("0") == sc_gtin.lstrip("0"):
                                    try:
                                        sc.good_id = int(it["good_id"])
                                    except Exception:
                                        pass

                    _auto_fill_tnved_from_attrs(sc)
                    sc.updated_at = datetime.now(timezone.utc)

            except NKAPIError as e:
                logger.warning("Ошибка проверки статуса фида %s: %s", card.feed_id, e)

        # 2. Если есть good_id или GTIN, запрашиваем детальную информацию о карточке
        if card.good_id or card.gtin:
            try:
                prod_items = await client.get_feed_product(good_id=card.good_id, gtin=card.gtin)
                if prod_items:
                    p = prod_items[0]
                    if not card.good_id and p.get("good_id"):
                        card.good_id = int(p["good_id"])
                    st_val = p.get("good_status") or p.get("status")
                    if not st_val and p.get("good_detailed_status") and isinstance(p.get("good_detailed_status"), list):
                        st_val = p["good_detailed_status"][0]
                    if st_val:
                        st_lower = str(st_val).lower()
                        card.status = st_lower if st_lower in ("draft", "moderation", "notsigned", "published", "errors", "rejected") else str(st_val)
                    if p.get("good_mark_flag") is not None:
                        card.good_mark_flag = bool(p["good_mark_flag"])
                    if p.get("good_turn_flag") is not None:
                        card.good_turn_flag = bool(p["good_turn_flag"])
            except NKAPIError:
                pass

    _auto_fill_tnved_from_attrs(card)
    card.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(card)
    return card


@router.post("/sellers/{seller_id}/national-catalog/products/{product_id}/send-to-moderation", response_model=ProductCardResponse)
async def send_product_to_moderation(
    seller_id: str,
    product_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Отправка черновика карточки на модерацию в Честный Знак (/nk/feed-moderation).
    """
    seller = await _get_seller_or_404(seller_id, db)
    stmt = select(ProductCard).where(
        ProductCard.id == product_id,
        ProductCard.seller_id == seller_id
    )
    card = (await db.execute(stmt)).scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Карточка товара не найдена")

    client = await _get_seller_nk_client(seller)

    try:
        async with client:
            await client.send_to_moderation(good_id=card.good_id, gtin=card.gtin)
    except NKAPIError as e:
        raise HTTPException(status_code=e.status_code or 400, detail=e.message)

    card.status = "moderation"
    card.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(card)
    return card


@router.post("/sellers/{seller_id}/national-catalog/products/{product_id}/prepare-sign", response_model=PrepareSignResponse)
async def prepare_sign(
    seller_id: str,
    product_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Получение канонического XML карточки для подписания УКЭП в браузере (POST /nk/feed-product-document).
    """
    seller = await _get_seller_or_404(seller_id, db)
    stmt = select(ProductCard).where(
        ProductCard.id == product_id,
        ProductCard.seller_id == seller_id
    )
    card = (await db.execute(stmt)).scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Карточка товара не найдена")

    if not card.good_id:
        raise HTTPException(
            status_code=400,
            detail="Карточка еще не получила идентификатор good_id от ЧЗ (проверьте статус модерации)."
        )

    client = await _get_seller_nk_client(seller)

    try:
        async with client:
            xmls = await client.get_product_document_xml([card.good_id])
    except NKAPIError as e:
        raise HTTPException(status_code=e.status_code or 400, detail=e.message)

    if not xmls:
        raise HTTPException(status_code=400, detail="Честный Знак не вернул XML документа для подписания.")

    raw_xml = xmls[0].get("xml", "")
    if not raw_xml:
        raise HTTPException(status_code=400, detail="Получен пустой XML от Честного Знака.")

    card.raw_xml = raw_xml
    await db.commit()

    base64_xml = base64.b64encode(raw_xml.encode("utf-8")).decode("ascii")
    return PrepareSignResponse(
        good_id=card.good_id,
        gtin=card.gtin,
        raw_xml=raw_xml,
        base64_xml=base64_xml,
    )


@router.post("/sellers/{seller_id}/national-catalog/products/{product_id}/publish", response_model=ProductCardResponse)
async def publish_product(
    seller_id: str,
    product_id: str,
    payload: SubmitSignRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Публикация карточки товара в НКТ с отправкой открепленной CMS-подписи (POST /nk/feed-product-sign-pkcs).
    """
    seller = await _get_seller_or_404(seller_id, db)
    stmt = select(ProductCard).where(
        ProductCard.id == product_id,
        ProductCard.seller_id == seller_id
    )
    card = (await db.execute(stmt)).scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Карточка товара не найдена")

    if not card.raw_xml or not card.good_id:
        raise HTTPException(status_code=400, detail="Сначала вызовите prepare-sign для формирования документа.")

    client = await _get_seller_nk_client(seller)

    base64_xml = base64.b64encode(card.raw_xml.encode("utf-8")).decode("ascii")
    sign_payload = [
        {
            "goodId": card.good_id,
            "base64Xml": base64_xml,
            "signature": payload.signature.strip(),
        }
    ]

    try:
        async with client:
            res = await client.sign_product_pkcs(sign_payload)
            signed_ids = res.get("signed", [])
            errors = res.get("errors", [])

            if errors:
                raise NKAPIError(f"Ошибки при публикации: {errors}")
            if card.good_id not in signed_ids and signed_ids:
                logger.warning("goodId %s not explicitly in signed list %s", card.good_id, signed_ids)

    except NKAPIError as e:
        raise HTTPException(status_code=e.status_code or 400, detail=e.message)

    card.status = "published"
    card.good_mark_flag = True
    card.good_turn_flag = True
    card.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(card)
    return card


# ==================== Вспомогательные методы справочников ====================


@router.get("/sellers/{seller_id}/national-catalog/helpers/categories")
async def get_categories(
    seller_id: str,
    tnved: Optional[str] = Query(None),
    cat_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Получение списка категорий НКТ по коду ТН ВЭД."""
    seller = await _get_seller_or_404(seller_id, db)
    client = await _get_seller_nk_client(seller)
    try:
        async with client:
            return await client.get_categories(tnved=tnved, cat_id=cat_id)
    except NKAPIError as e:
        raise HTTPException(status_code=e.status_code or 400, detail=e.message)


@router.get("/sellers/{seller_id}/national-catalog/helpers/attributes")
async def get_attributes(
    seller_id: str,
    cat_id: Optional[int] = Query(None),
    tnved: Optional[str] = Query(None),
    attr_type: str = Query("m", description="m - обязательные, r - рекомендуемые, a - все"),
    db: AsyncSession = Depends(get_db),
):
    """Получение требований к атрибутам по категории или ТН ВЭД."""
    seller = await _get_seller_or_404(seller_id, db)
    client = await _get_seller_nk_client(seller)
    try:
        async with client:
            return await client.get_attributes(cat_id=cat_id, tnved=tnved, attr_type=attr_type)
    except NKAPIError as e:
        raise HTTPException(status_code=e.status_code or 400, detail=e.message)


@router.get("/sellers/{seller_id}/national-catalog/helpers/brands")
async def search_brands(
    seller_id: str,
    name: str = Query(..., min_length=2),
    db: AsyncSession = Depends(get_db),
):
    """Поиск торговых марок / брендов."""
    seller = await _get_seller_or_404(seller_id, db)
    client = await _get_seller_nk_client(seller)
    try:
        async with client:
            return await client.get_brands(name=name)
    except NKAPIError as e:
        raise HTTPException(status_code=e.status_code or 400, detail=e.message)


@router.get("/sellers/{seller_id}/national-catalog/helpers/generate-gtin")
async def generate_gtin(
    seller_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Генерация черновика GTIN по диапазону предприятия в ГС1 РУС."""
    seller = await _get_seller_or_404(seller_id, db)
    client = await _get_seller_nk_client(seller)
    try:
        async with client:
            gtins = await client.generate_gtin(quantity=1)
            if not gtins:
                raise HTTPException(
                    status_code=400,
                    detail="Не удалось сгенерировать GTIN (проверьте членство в ГС1 РУС или месячный лимит)."
                )
            return {"gtin": gtins[0]}
    except NKAPIError as e:
        raise HTTPException(status_code=e.status_code or 400, detail=e.message)


@router.get("/sellers/{seller_id}/national-catalog/helpers/generate-gtins", response_model=BatchGtinResponse)
async def generate_gtins(
    seller_id: str,
    quantity: int = Query(1, ge=1, le=100, description="Количество GTIN для генерации (от 1 до 100)"),
    db: AsyncSession = Depends(get_db),
):
    """Пакетная генерация пула черновиков GTIN в ГС1 РУС через True API."""
    seller = await _get_seller_or_404(seller_id, db)
    client = await _get_seller_nk_client(seller)
    try:
        async with client:
            gtins = await client.generate_gtin(quantity=quantity)
            return BatchGtinResponse(gtins=gtins)
    except NKAPIError as e:
        raise HTTPException(status_code=e.status_code or 400, detail=e.message)
