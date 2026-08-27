"""
Telegram Bot Text Message Handlers — WB FBS Manager
"""
import logging
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, and_
from sqlalchemy.orm import Session

from app.models.order import Order, KizStatus
from app.models.kiz import KizOperation, KizOperationType
from app.models.audit import AuditLog
from app.bot.helpers import sync_engine, _get_active_seller, _pending_kiz_input

logger = logging.getLogger(__name__)


async def handle_text_message(message: Message):
    # 1. Completely ignore ALL human conversations in groups/supergroups
    if message.chat.type in ["group", "supergroup", "channel"]:
        return

    # 2. In private chats, only handle if user is explicitly inputting a KIZ code
    chat_id = message.chat.id
    text = message.text.strip()

    # If it's a command, it's already caught by Command() handlers
    if text.startswith("/"):
        return

    # Check if we were waiting for KIZ for a specific order in DM
    order_id = _pending_kiz_input.pop(chat_id, None)

    # If not waiting for KIZ and text doesn't look like DataMatrix barcode (length >= 20), ignore silently
    if not order_id and len(text) < 20:
        return

    with Session(sync_engine) as db:
        seller = _get_active_seller(db, chat_id)
        if not seller:
            return

        if not order_id:
            # Find most recent order waiting for KIZ
            pending_order = db.scalar(
                select(Order).where(
                    and_(
                        Order.seller_id == seller.id,
                        Order.kiz_required == True,
                        Order.kiz_status.in_([KizStatus.PENDING, KizStatus.ERROR]),
                    )
                ).order_by(Order.created_at.asc())
            )
            if pending_order:
                order_id = pending_order.id

        if not order_id:
            return

        order = db.get(Order, order_id)
        if not order:
            await message.answer(f"❌ Заказ #{order_id} не найден.")
            return

        # Save KIZ to order
        order.kiz_code = text
        order.kiz_status = KizStatus.ATTACHED
        order.kiz_cz_status = None
        order.kiz_cz_status_updated_at = None
        order.kiz_attached_at = datetime.now(timezone.utc)

        kiz_op = KizOperation(
            order_id=order.id,
            seller_id=order.seller_id,
            kiz_code=text,
            operation_type=KizOperationType.ATTACH,
            created_at=datetime.now(timezone.utc),
        )
        db.add(kiz_op)

        audit = AuditLog(
            seller_id=str(order.seller_id),
            agent="telegram_bot",
            action="ATTACH_KIZ",
            entity_type="order",
            entity_id=str(order.id),
            payload={"kiz_code": text[:20] + "..."},
            created_at=datetime.now(timezone.utc),
        )
        db.add(audit)
        db.commit()

    await message.answer(
        f"✅ <b>КИЗ успешно привязан!</b>\n\n"
        f"📦 Заказ: <code>#{order_id}</code>\n"
        f"🏷️ КИЗ: <code>{text}</code>\n"
        f"📊 Статус маркировки: <b>ATTACHED</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="📋 Детали заказа", callback_data=f"order:detail:{order_id}"),
                InlineKeyboardButton(text="✅ В сборку", callback_data=f"order:assemble:{order_id}"),
            ]]
        )
    )


def register_message_handlers(router: Router):
    """Registers all text message handlers on the provided router."""
    router.message.register(handle_text_message, F.text)
