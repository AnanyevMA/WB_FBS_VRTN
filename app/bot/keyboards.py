"""
Telegram Bot Keyboards — WB FBS Manager
"""
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from typing import List
from app.models.order import Order


def get_main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Main menu reply keyboard (for private chats only)."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📦 Заказы в сборке"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="🚚 Поставки"), KeyboardButton(text="🌅 Утренний дайджест")],
            [KeyboardButton(text="🧪 Тестовый заказ"), KeyboardButton(text="❓ Справка")],
        ],
        resize_keyboard=True,
    )


def get_orders_list_keyboard(orders: List[Order], seller_id: str) -> InlineKeyboardMarkup:
    """Inline keyboard for list of orders."""
    keyboard_buttons = []
    for o in orders:
        keyboard_buttons.append([
            InlineKeyboardButton(
                text=f"📋 #{o.id} - Детали",
                callback_data=f"order:detail:{o.id}"
            ),
            InlineKeyboardButton(
                text="✅ В сборку",
                callback_data=f"order:assemble:{o.id}"
            ),
        ])

    keyboard_buttons.append([
        InlineKeyboardButton(
            text="📦 Сформировать поставку из всех",
            callback_data=f"supply:create_all:{seller_id}"
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)


def get_order_detail_keyboard(order_id: int, seller_id: str, is_active: bool = True) -> InlineKeyboardMarkup:
    """Inline keyboard for single order detail view."""
    buttons = []
    if is_active:
        buttons.append([
            InlineKeyboardButton(text="✅ В сборку", callback_data=f"order:assemble:{order_id}"),
            InlineKeyboardButton(text="📷 Привязать КИЗ", callback_data=f"kiz:scan:{order_id}"),
        ])
        buttons.append([
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"order:cancel:{order_id}"),
        ])

    buttons.append([
        InlineKeyboardButton(text="🔙 К списку заказов", callback_data=f"orders:pending:{seller_id}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
