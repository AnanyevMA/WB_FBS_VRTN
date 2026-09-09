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
    limit: int = Query(50, ge=1, le=200),
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
    return result.scalars().all()


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

    # Валидация GTIN
    gtin_val = payload.gtin.strip() if payload.gtin else None
    if not payload.is_tech_gtin and not gtin_val:
        raise HTTPException(
            status_code=400,
            detail="Укажите GTIN (14 цифр) или выберите признак технической карточки (029)."
        )

    # 1. Формируем структуру товара по спецификации True API POST /nk/feed
    goods_item: dict = {
        "good_name": payload.name,
        "moderation": payload.moderation,
        "is_set": payload.is_set,
        "good_attrs": [],
        "good_images": [],
    }

    if payload.brand:
        goods_item["brand"] = payload.brand
    if payload.tnved:
        goods_item["tnved"] = payload.tnved
    if payload.category_id:
        goods_item["categories"] = [payload.category_id]

    if payload.is_tech_gtin:
        goods_item["is_tech_gtin"] = 1
    elif gtin_val:
        # Нормализация до 14 цифр с лидирующим нулем если передано 13
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

    for attr in payload.attributes:
        attr_dict = {
            "attr_id": attr.attr_id,
            "attr_value": attr.attr_value,
        }
        if attr.attr_value_type:
            attr_dict["attr_value_type"] = attr.attr_value_type
        if attr.attr_value_id:
            attr_dict["attr_value_id"] = attr.attr_value_id
        goods_item["good_attrs"].append(attr_dict)

    for img in payload.images:
        img_dict = {
            "photo_type": img.photo_type or "default",
            "photo_url": img.photo_url,
            "photo_date": img.photo_date or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        goods_item["good_images"].append(img_dict)

    # 2. Отправка реального фида в Национальный каталог
    try:
        async with client:
            feed_id = await client.create_or_update_feed([goods_item])
    except NKAPIError as e:
        logger.warning("Ошибка отправки фида в НКТ: %s", e)
        raise HTTPException(status_code=e.status_code or 400, detail=e.message)

    # 3. Сохранение карточки в БД
    card = ProductCard(
        seller_id=seller.id,
        gtin=gtin_val,
        name=payload.name,
        brand=payload.brand,
        tnved=payload.tnved,
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
    return card


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
                card.feed_status = status_str

                # Разбираем ошибки фида если есть
                error_items = feed_data.get("item", [])
                if error_items:
                    card.error_details = error_items
                    if status_str in ("Rejected", "errors"):
                        card.status = "errors"

                if status_str in ("Moderated", "Signed"):
                    card.status = "notsigned" if status_str == "Moderated" else "published"
                    # Извлекаем присвоенный good_id
                    for it in error_items:
                        if isinstance(it, dict) and it.get("good_id"):
                            try:
                                card.good_id = int(it["good_id"])
                            except Exception:
                                pass
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
                    if st_val:
                        card.status = str(st_val).lower() if str(st_val).lower() in ("draft", "moderation", "notsigned", "published", "errors", "rejected") else str(st_val)
                    if p.get("good_mark_flag") is not None:
                        card.good_mark_flag = bool(p["good_mark_flag"])
                    if p.get("good_turn_flag") is not None:
                        card.good_turn_flag = bool(p["good_turn_flag"])
            except NKAPIError:
                pass

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
