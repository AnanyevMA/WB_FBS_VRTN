"""
Telegram Bot Package — WB FBS Manager
Интерактивный бот для управления сборочными заданиями WB FBS и Честным Знаком.
"""
from aiogram import Router
from app.bot.helpers import (
    sync_engine,
    _pending_kiz_input,
    _get_active_seller,
)
from app.bot.keyboards import (
    get_main_reply_keyboard,
    get_orders_list_keyboard,
    get_order_detail_keyboard,
)
from app.bot.handlers import register_all_handlers


def create_bot_router() -> Router:
    """Creates aiogram router with all command, document, callback, and message handlers."""
    router = Router(name="main_bot_router")
    register_all_handlers(router)
    return router


__all__ = [
    "create_bot_router",
    "get_main_reply_keyboard",
    "get_orders_list_keyboard",
    "get_order_detail_keyboard",
    "_get_active_seller",
    "_pending_kiz_input",
    "sync_engine",
]
