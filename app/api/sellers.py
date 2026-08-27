from fastapi import APIRouter, Depends, HTTPException, status, Body
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

    # Handle encrypted fields (never overwrite with empty or whitespace-only values)
    if "wb_api_token" in data:
        tok = data.pop("wb_api_token")
        if tok and str(tok).strip():
            seller.wb_api_token_encrypted = encrypt(str(tok).strip())
    if "cz_token" in data:
        tok = data.pop("cz_token")
        if tok and str(tok).strip():
            seller.cz_token_encrypted = encrypt(str(tok).strip())
    if "telegram_bot_token" in data:
        tok = data.pop("telegram_bot_token")
        if tok and str(tok).strip():
            seller.telegram_bot_token_encrypted = encrypt(str(tok).strip())

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

    results = {
        "wb": {"status": "pending", "message": ""},
        "telegram": {"status": "skipped", "message": "Не настроен"},
        "chestny_znak": {"status": "skipped", "message": "Не настроен"}
    }
    overall_success = True

    # 1. WB API test
    try:
        token = decrypt(seller.wb_api_token_encrypted)
        client = WBClient(token)
        try:
            await client.get_new_orders()
            results["wb"] = {"status": "ok", "message": "Подключение к WB API успешно проверено"}
        except Exception as e:
            overall_success = False
            results["wb"] = {"status": "error", "message": f"Ошибка WB API: {str(e)}"}
        finally:
            await client.close()
    except Exception as e:
        overall_success = False
        results["wb"] = {"status": "error", "message": f"Не удалось расшифровать токен WB: {str(e)}"}

    # 2. Telegram Bot & Recipients test
    if seller.telegram_bot_token_encrypted:
        try:
            import httpx
            tg_token = decrypt(seller.telegram_bot_token_encrypted)
            async with httpx.AsyncClient(timeout=10.0) as http_client:
                me_resp = await http_client.get(f"https://api.telegram.org/bot{tg_token}/getMe")
                if me_resp.status_code == 200:
                    bot_data = me_resp.json().get("result", {})
                    bot_user = bot_data.get("username", "bot")
                    chat_ids = [str(c) for c in (seller.telegram_chat_ids or []) if str(c).strip()]
                    if chat_ids:
                        results["telegram"] = {
                            "status": "ok",
                            "message": f"Бот @{bot_user} активен. Получатели: {len(chat_ids)} чат(ов) [{', '.join(chat_ids)}]"
                        }
                    else:
                        results["telegram"] = {
                            "status": "warning",
                            "message": f"Бот @{bot_user} активен, но ID получателей (чатов) не указаны"
                        }
                else:
                    overall_success = False
                    results["telegram"] = {"status": "error", "message": f"Неверный токен бота Telegram (HTTP {me_resp.status_code})"}
        except Exception as e:
            overall_success = False
            results["telegram"] = {"status": "error", "message": f"Ошибка проверки Telegram: {str(e)}"}

    # 3. Chestny Znak test
    if seller.cz_token_encrypted or seller.cryptopro_cert_thumbprint or seller.cz_cert_path or seller.cz_inn:
        cz_details = []
        if seller.cz_inn:
            cz_details.append(f"ИНН: {seller.cz_inn}")
        if seller.cryptopro_cert_thumbprint or seller.cz_cert_path:
            cz_details.append(f"ЭЦП: {(seller.cryptopro_cert_thumbprint or seller.cz_cert_path)[:16]}...")
        if seller.cz_token_encrypted:
            cz_details.append("True API токен")
        results["chestny_znak"] = {
            "status": "ok",
            "message": f"Параметры ЧЗ настроены ({', '.join(cz_details) if cz_details else 'указаны'})"
        }

    # Summary messages
    messages = []
    if results["wb"]["status"] == "ok":
        messages.append("✅ WB API подключен")
    else:
        messages.append(f"❌ WB API: {results['wb']['message']}")

    if results["telegram"]["status"] == "ok":
        messages.append(f"✅ Telegram: {results['telegram']['message']}")
    elif results["telegram"]["status"] == "warning":
        messages.append(f"⚠️ Telegram: {results['telegram']['message']}")
    elif results["telegram"]["status"] == "error":
        messages.append(f"❌ Telegram: {results['telegram']['message']}")

    if results["chestny_znak"]["status"] == "ok":
        messages.append(f"✅ ЧЗ: {results['chestny_znak']['message']}")

    return {
        "success": overall_success,
        "message": " \n".join(messages),
        "details": results
    }


@router.post("/{seller_id}/toggle-polling")
async def toggle_polling(seller_id: str, enabled: bool, db: AsyncSession = Depends(get_db)):
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")

    seller.polling_enabled = enabled
    await db.commit()
    return {"message": f"Polling {'enabled' if enabled else 'disabled'}"}


@router.get("/{seller_id}/time")
async def get_seller_time(seller_id: str, db: AsyncSession = Depends(get_db)):
    """Получить системное время сервера и текущее локальное время магазина."""
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")

    from app.services.time_service import (
        get_server_time_info,
        get_seller_local_time,
        format_seller_digest_time,
    )

    server_info = get_server_time_info()
    local_now = get_seller_local_time(seller)

    return {
        "seller_id": seller_id,
        "seller_name": seller.name,
        "digest_timezone": seller.digest_timezone or "Europe/Moscow",
        "digest_hour": seller.digest_hour,
        "digest_minute": seller.digest_minute,
        "seller_local_now": local_now.strftime("%Y-%m-%d %H:%M:%S"),
        "seller_local_formatted": format_seller_digest_time(seller),
        "server_time": server_info,
    }


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


@router.get("/{seller_id}/cz-challenge")
async def get_cz_auth_challenge(seller_id: str, db: AsyncSession = Depends(get_db)):
    """Request dynamic auth challenge from GIS MT True API for browser signing."""
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")
    if not seller.cz_inn:
        raise HTTPException(status_code=400, detail="У продавца не указан ИНН для Честного Знака")

    from app.services.cz_client import CZClient, CZAPIError
    try:
        async with CZClient(inn=seller.cz_inn) as client:
            return await client.get_auth_challenge()
    except CZAPIError as err:
        raise HTTPException(status_code=err.status_code or 502, detail=str(err))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось связаться с ГИС МТ: {str(exc)}")


@router.post("/{seller_id}/cz-signin")
async def cz_signin_with_signature(
    seller_id: str,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db)
):
    """Authenticate with GIS MT True API using signature created in browser by CryptoPro Plugin."""
    from app.models.audit import AuditLog
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    auth_uuid = payload.get("uuid")
    signed_data = payload.get("data")
    if not auth_uuid or not signed_data:
        raise HTTPException(status_code=400, detail="Необходимо передать uuid и подписанные данные data")

    from app.services.cz_client import CZClient, CZAPIError
    try:
        async with CZClient(inn=seller.cz_inn or "") as client:
            token = await client.signin_with_signature(auth_uuid=auth_uuid, signed_data=signed_data)

            seller.cz_token_encrypted = encrypt(token)
            seller.updated_at = datetime.now(timezone.utc)

            # Audit log
            audit = AuditLog(
                seller_id=seller_id,
                agent="cz_auth_ui",
                action="BROWSER_AUTH_SUCCESS",
                entity_type="seller",
                entity_id=seller_id,
                payload={"inn": seller.cz_inn},
                created_at=datetime.now(timezone.utc),
            )
            db.add(audit)
            await db.commit()

            return {
                "success": True,
                "message": "Токен Честного Знака успешно получен и сохранен в системе!",
                "token_preview": f"{token[:8]}...{token[-6:]}" if len(token) > 14 else "***",
            }
    except CZAPIError as err:
        raise HTTPException(status_code=err.status_code or 400, detail=str(err))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Ошибка аутентификации: {str(exc)}")
