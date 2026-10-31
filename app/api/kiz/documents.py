"""
FastAPI CZ Document Status Endpoint — WB FBS Manager
"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.seller import Seller

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/kiz/documents/{doc_id}/status")
async def get_cz_document_status(
    seller_id: str,
    doc_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Проверка статуса обработки документа в ГИС МТ (True API v4).
    """
    seller = await db.get(Seller, seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Продавец не найден")
    if not seller.cz_token_encrypted:
        raise HTTPException(status_code=400, detail="Токен Честного Знака не настроен")

    from app.services.encryption import decrypt
    from app.services.cz_client import CZClient

    token = decrypt(seller.cz_token_encrypted)
    inn = seller.cz_inn

    async with CZClient(inn=inn, token=token) as client:
        try:
            status_data = await client.get_document_info(doc_id, pg="lp")
            status = str(status_data.get("status") or "")
            error_text = client.extract_document_error_text(status_data) if status.upper() in ("CHECKED_NOT_OK", "FAILED", "PARSE_ERROR") else None
            return {
                "success": True,
                "doc_id": doc_id,
                "status": status,
                "errors": status_data.get("errors", []),
                "common_errors": status_data.get("commonErrors", []),
                "error_text": error_text,
                "data": status_data,
            }
        except Exception as exc:
            logger.error(f"Error querying CZ document status for {doc_id}: {exc}")
            raise HTTPException(status_code=502, detail=f"Ошибка запроса к Честному Знаку: {str(exc)}")

