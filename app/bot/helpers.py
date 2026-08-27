"""
Telegram Bot Helpers & Shared State — WB FBS Manager
"""
import logging
from typing import Optional, Dict
from sqlalchemy import select, create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.models.seller import Seller

logger = logging.getLogger(__name__)
sync_engine = create_engine(settings.database_url_sync)

# User state memory for waiting KIZ code input: chat_id -> order_id
_pending_kiz_input: Dict[int, int] = {}


def _get_active_seller(db: Session, chat_id: Optional[int] = None) -> Optional[Seller]:
    """Find active seller in DB, optionally matching chat_id."""
    sellers = db.execute(select(Seller).where(Seller.is_active == True)).scalars().all()
    if not sellers:
        return None

    if chat_id:
        for s in sellers:
            if s.telegram_chat_ids:
                chat_ids_str = [str(c) for c in s.telegram_chat_ids]
                if str(chat_id) in chat_ids_str:
                    return s

    # Fallback to first active seller with token
    for s in sellers:
        if s.telegram_bot_token_encrypted:
            return s
    return sellers[0] if sellers else None
