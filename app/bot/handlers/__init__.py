"""
Telegram Bot Handlers Package — WB FBS Manager
"""
from aiogram import Router
from app.bot.handlers.commands import register_command_handlers
from app.bot.handlers.documents import register_document_handlers
from app.bot.handlers.callbacks import register_callback_handlers
from app.bot.handlers.messages import register_message_handlers


def register_all_handlers(router: Router) -> Router:
    """Registers all bot handlers in the correct sequence on the provided router."""
    register_command_handlers(router)
    register_document_handlers(router)
    register_callback_handlers(router)
    register_message_handlers(router)
    return router
