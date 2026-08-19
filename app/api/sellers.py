from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from typing import List
from datetime import datetime, timezone

from app.database import get_db
from app.models.seller import Seller
from app.models.order import Order, OrderStatus
from app.schemas.seller import SellerCreate, SellerUpdate, SellerResponse, SellerListItem
from app.services.encryption import encrypt, decrypt
from app.services.wb_client import WBClient

router = APIRouter(prefix="/sellers", tags=["sellers"])


def _apply_polling_interval(seller: Seller, data: dict) -> None:
    """Convert polling_interval_minutes → polling_interval_seconds and write to seller."""
    minutes = data.pop("polling_interval_minutes", None)
    if minutes is not None:
        seller.polling_interval_seconds = int(minutes) * 60


def _apply_digest_settings(seller: Seller, data: dict) -> None:
    """Apply digest settings from nested 'digest' object or flat fields."""
    digest_obj = data.pop("digest", None)
    if digest_obj is not None:
        # Nested object takes priority (support both dict and model)
        if isinstance(digest_obj, dict):
            seller.digest_enabled = digest_obj.get("enabled", True)
            seller.digest_hour = digest_obj.get("hour", 8)
            seller.digest_minute = digest_obj.get("minute", 0)
            seller.digest_timezone = digest_obj.get("timezone", "Europe/Moscow")
        else:
            seller.digest_enabled = getattr(digest_obj, "enabled", True)
            seller.digest_hour = getattr(digest_obj, "hour", 8)
            seller.digest_minute = getattr(digest_obj, "minute", 0)
            seller.digest_timezone = getattr(digest_obj, "timezone", "Europe/Moscow")
    else:
        if "digest_enabled" in data:
            seller.digest_enabled = data.pop("digest_enabled")
        if "digest_hour" in data:
            seller.digest_hour = data.pop("digest_hour")
        if "digest_minute" in data:
            seller.digest_minute = data.pop("digest_minute")
        if "digest_timezone" in data:
            seller.digest_timezone = data.pop("digest_timezone")


@router.post("", response_model=SellerResponse)
async def create_seller(seller_in: SellerCreate, db: AsyncSession = Depends(get_db)):
    data = seller_in.model_dump()

    # Encrypt sensitive tokens
    wb_token = data.pop("wb_api_token", "")
    cz_token = data.pop("cz_token", None)
    tg_token = data.pop("telegram_bot_token", None)

    data["wb_api_token_encrypted"] = encrypt(wb_token)
    if cz_token:
        data["cz_token_encrypted"] = encrypt(cz_token)
    if tg_token:
        data["telegram_bot_token_encrypted"] = encrypt(tg_token)

    # polling interval
    minutes = data.pop("polling_interval_minutes", None)
    if minutes is not None:
        data["polling_interval_seconds"] = int(minutes) * 60

    # digest settings
    digest_obj = data.pop("digest", None)
    if digest_obj is not None:
        if isinstance(digest_obj, dict):
            data["digest_enabled"] = digest_obj.get("enabled", True)
            data["digest_hour"] = digest_obj.get("hour", 8)
            data["digest_minute"] = digest_obj.get("minute", 0)
            data["digest_timezone"] = digest_obj.get("timezone", "Europe/Moscow")
        else:
            data["digest_enabled"] = getattr(digest_obj, "enabled", True)
            data["digest_hour"] = getattr(digest_obj, "hour", 8)
            data["digest_minute"] = getattr(digest_obj, "minute", 0)
            data["digest_timezone"] = getattr(digest_obj, "timezone", "Europe/Moscow")

    # Cert thumbprint / path sync
    if data.get("cryptopro_cert_thumbprint") and not data.get("cz_cert_path"):
        data["cz_cert_path"] = data["cryptopro_cert_thumbprint"]
    elif data.get("cz_cert_path") and not data.get("cryptopro_cert_thumbprint"):
        data["cryptopro_cert_thumbprint"] = data["cz_cert_path"]

    seller = Seller(**data)
    db.add(seller)
    await db.commit()
    await db.refresh(seller)
    return seller


@router.get("", response_model=List[SellerListItem])
async def list_sellers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Seller))
    return result.scalars().all()


@router.get("/{seller_id}", response_model=SellerResponse)
async def get_seller(seller_id: str, db: AsyncSession = Depends(get_db)):
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")
    return seller


@router.patch("/{seller_id}", response_model=SellerResponse)
async def update_seller(seller_id: str, seller_in: SellerUpdate, db: AsyncSession = Depends(get_db)):
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")

    data = seller_in.model_dump(exclude_unset=True)

    # Handle encrypted fields
    if "wb_api_token" in data:
        tok = data.pop("wb_api_token")
        if tok:
            seller.wb_api_token_encrypted = encrypt(tok)
    if "cz_token" in data:
        tok = data.pop("cz_token")
        if tok:
            seller.cz_token_encrypted = encrypt(tok)
    if "telegram_bot_token" in data:
        tok = data.pop("telegram_bot_token")
        if tok:
            seller.telegram_bot_token_encrypted = encrypt(tok)

    # Convert polling minutes → seconds
    _apply_polling_interval(seller, data)

    # Apply digest settings
    _apply_digest_settings(seller, data)

    # Apply remaining fields
    for k, v in data.items():
        if hasattr(seller, k):
            setattr(seller, k, v)

    if seller.cryptopro_cert_thumbprint and not seller.cz_cert_path:
        seller.cz_cert_path = seller.cryptopro_cert_thumbprint
    elif seller.cz_cert_path and not seller.cryptopro_cert_thumbprint:
        seller.cryptopro_cert_thumbprint = seller.cz_cert_path

    await db.commit()
    await db.refresh(seller)
    return seller


@router.delete("/{seller_id}")
async def deactivate_seller(seller_id: str, db: AsyncSession = Depends(get_db)):
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")

    seller.is_active = False
    await db.commit()
    return {"message": "Seller deactivated", "is_active": False}


@router.post("/{seller_id}/test-connection")
async def test_connection(seller_id: str, db: AsyncSession = Depends(get_db)):
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")

    token = decrypt(seller.wb_api_token_encrypted)
    client = WBClient(token)
    try:
        await client.get_new_orders()
        return {"success": True, "message": "Connection to WB API verified successfully"}
    except Exception as e:
        return {"success": False, "message": f"WB API connection check failed: {str(e)}"}
    finally:
        await client.close()


@router.post("/{seller_id}/toggle-polling")
async def toggle_polling(seller_id: str, enabled: bool, db: AsyncSession = Depends(get_db)):
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")

    seller.polling_enabled = enabled
    await db.commit()
    return {"message": f"Polling {'enabled' if enabled else 'disabled'}"}


@router.get("/{seller_id}/pending-summary")
async def get_pending_summary(seller_id: str, db: AsyncSession = Depends(get_db)):
    """
    Быстрая сводка необработанных заказов продавца.
    Используется кнопкой «Показать все» в утреннем дайджесте.
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")

    pending_statuses = [OrderStatus.NEW, OrderStatus.ASSEMBLING]

    # Count and sum
    result = await db.execute(
        select(
            func.count(Order.id).label("count"),
            func.coalesce(func.sum(Order.price), 0).label("total_price"),
            func.sum(
                func.cast(Order.kiz_required, type_=db.bind.dialect.INTEGER if hasattr(db, 'bind') else None)
            ).label("kiz_count"),
            func.min(Order.wb_created_at).label("oldest_at"),
        ).where(
            and_(
                Order.seller_id == seller_id,
                Order.status.in_(pending_statuses),
                Order.supply_id.is_(None),
            )
        )
    )
    row = result.one()
    count = row.count or 0
    total_price = float(row.total_price or 0)

    # Separate kiz count query (cross-DB safe)
    kiz_count = await db.scalar(
        select(func.count(Order.id)).where(
            and_(
                Order.seller_id == seller_id,
                Order.status.in_(pending_statuses),
                Order.supply_id.is_(None),
                Order.kiz_required.is_(True),
            )
        )
    ) or 0

    oldest_age_hours: float = 0.0
    if row.oldest_at:
        oldest_at = row.oldest_at
        if oldest_at.tzinfo is None:
            oldest_at = oldest_at.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - oldest_at
        oldest_age_hours = round(delta.total_seconds() / 3600, 1)

    return {
        "seller_id": seller_id,
        "pending_count": count,
        "total_price": total_price,
        "kiz_required_count": kiz_count,
        "oldest_order_age_hours": oldest_age_hours,
        "has_pending": count > 0,
    }
