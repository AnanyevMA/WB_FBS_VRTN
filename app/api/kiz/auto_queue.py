"""
FastAPI Auto KIZ Queue Endpoints — WB FBS Manager
Управление автоматической очередью вывода и возврата КИЗ:
ручной триггер, настройки суточного расписания и выбор ответственного менеджера.
"""
import logging
from typing import Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.seller import Seller
from app.services.auto_kiz_queue_service import process_auto_kiz_queue_for_seller

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/kiz/auto-batch/trigger")
async def trigger_auto_kiz_batch_now(
    seller_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Принудительный ручной запуск суточного сбора КИЗ (не дожидаясь 17:00).
    Синхронизирует статусы WB API, отбирает продажи и возвраты,
    проверяет True API и формирует пакет в очереди ЭЦП.
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    try:
        result = await process_auto_kiz_queue_for_seller(
            seller=seller,
            db=db,
            trigger_source="manual_auto_trigger",
        )
        return result
    except Exception as exc:
        logger.error(f"[Auto KIZ API] Failed to trigger auto batch for {seller_id}: {exc}")
        raise HTTPException(status_code=500, detail=f"Ошибка формирования пакета КИЗ: {str(exc)}")


@router.get("/kiz/auto-batch/settings")
async def get_auto_kiz_settings(
    seller_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Получение настроек автоматической очереди КИЗ (время, тумблер, менеджер).
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    return {
        "auto_kiz_queue_enabled": getattr(seller, "auto_kiz_queue_enabled", True),
        "auto_kiz_queue_hour": getattr(seller, "auto_kiz_queue_hour", 17),
        "auto_kiz_queue_minute": getattr(seller, "auto_kiz_queue_minute", 0),
        "auto_kiz_auto_sign_server": getattr(seller, "auto_kiz_auto_sign_server", False),
        "auto_kiz_manager_chat_id": getattr(seller, "auto_kiz_manager_chat_id", None),
        "last_auto_kiz_queue_at": seller.last_auto_kiz_queue_at.isoformat() if seller.last_auto_kiz_queue_at else None,
        "timezone": getattr(seller, "timezone", "Europe/Moscow"),
    }


@router.patch("/kiz/auto-batch/settings")
async def update_auto_kiz_settings(
    seller_id: str,
    payload: Dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Обновление настроек автоматической очереди КИЗ:
    - auto_kiz_queue_enabled: bool
    - auto_kiz_queue_hour: int (0-23)
    - auto_kiz_queue_minute: int (0-59)
    - auto_kiz_auto_sign_server: bool
    - auto_kiz_manager_chat_id: str (ID менеджера для персональных уведомлений)
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")

    if "auto_kiz_queue_enabled" in payload:
        seller.auto_kiz_queue_enabled = bool(payload["auto_kiz_queue_enabled"])

    if "auto_kiz_queue_hour" in payload:
        hour = int(payload["auto_kiz_queue_hour"])
        if not (0 <= hour <= 23):
            raise HTTPException(status_code=400, detail="Час должен быть от 0 до 23")
        seller.auto_kiz_queue_hour = hour

    if "auto_kiz_queue_minute" in payload:
        minute = int(payload["auto_kiz_queue_minute"])
        if not (0 <= minute <= 59):
            raise HTTPException(status_code=400, detail="Минуты должны быть от 0 до 59")
        seller.auto_kiz_queue_minute = minute

    if "auto_kiz_auto_sign_server" in payload:
        seller.auto_kiz_auto_sign_server = bool(payload["auto_kiz_auto_sign_server"])

    if "auto_kiz_manager_chat_id" in payload:
        raw_val = payload["auto_kiz_manager_chat_id"]
        seller.auto_kiz_manager_chat_id = str(raw_val).strip() if raw_val is not None and str(raw_val).strip() else None

    await db.commit()

    return {
        "success": True,
        "message": "Настройки автоматической очереди КИЗ успешно сохранены",
        "settings": {
            "auto_kiz_queue_enabled": seller.auto_kiz_queue_enabled,
            "auto_kiz_queue_hour": seller.auto_kiz_queue_hour,
            "auto_kiz_queue_minute": seller.auto_kiz_queue_minute,
            "auto_kiz_auto_sign_server": seller.auto_kiz_auto_sign_server,
            "auto_kiz_manager_chat_id": seller.auto_kiz_manager_chat_id,
            "timezone": getattr(seller, "timezone", "Europe/Moscow"),
        },
    }
