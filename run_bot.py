"""
Run Telegram Bot Locally — WB FBS Manager
Запуск Telegram-бота в режиме polling для локальной проверки и обработки команд/кнопок.

Использование:
  python run_bot.py                # Запуск polling
  python run_bot.py --test-notify  # Отправить тестовое уведомление и запуститься
  python run_bot.py --check-only   # Только проверить токен и соединение
"""
import argparse
import asyncio
import logging
import signal
import sys
from typing import List

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.seller import Seller
from app.services.encryption import decrypt
from app.services.telegram_service import TelegramService
from app.bot import create_bot_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("wb_fbs_bot")


def get_active_sellers_with_tokens() -> List[tuple[Seller, str]]:
    """Retrieve all active sellers that have a Telegram bot token configured."""
    sync_engine = create_engine(settings.database_url_sync)
    with Session(sync_engine) as db:
        sellers = db.execute(
            select(Seller).where(Seller.is_active == True)
        ).scalars().all()

        results = []
        for seller in sellers:
            if seller.telegram_bot_token_encrypted:
                try:
                    token = decrypt(seller.telegram_bot_token_encrypted)
                    results.append((seller, token))
                except Exception as e:
                    logger.error(f"Failed to decrypt token for seller {seller.name} ({seller.id}): {e}")
        return results


async def check_bot(token: str, seller_name: str) -> bool:
    """Check if bot token is valid and log bot info."""
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await bot.get_me()
        logger.info(
            f"✅ Bot connected successfully for seller '{seller_name}':\n"
            f"   • Bot ID: {me.id}\n"
            f"   • Bot Name: {me.first_name}\n"
            f"   • Username: @{me.username}\n"
            f"   • Link: https://t.me/{me.username}"
        )
        return True
    except Exception as e:
        logger.error(f"❌ Failed to connect bot for seller '{seller_name}': {e}")
        return False
    finally:
        await bot.session.close()


async def send_test_notification(token: str, chat_ids: list, seller_id: str):
    """Send a test order notification."""
    import random
    test_id = random.randint(5600000000, 5699999999)
    test_data = {
        "name": "Футболка оверсайз VRTN White L",
        "brand": "VRTN",
        "subject": "Футболки",
        "article": "vrtn-tee-wht-l",
        "kiz_required": True,
        "price": 249000,  # коп.
    }
    svc = TelegramService(token)
    try:
        logger.info(f"📤 Sending test notification to chat IDs: {chat_ids}")
        res = await svc.send_new_order_notification(chat_ids, test_id, test_data)
        if res:
            logger.info("✅ Test notification sent successfully!")
        else:
            logger.warning("⚠️ Test notification returned False (check chat_ids or bot privacy settings).")
    finally:
        await svc.close()


async def run_polling(token: str, seller: Seller):
    """Start polling loop for single bot."""
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(create_bot_router())

    me = await bot.get_me()
    logger.info(f"🚀 Starting polling for @{me.username} (Seller: {seller.name})...")
    logger.info(f"💡 You can open Telegram and test commands (/start, /orders, /status, /digest, /help) in @{me.username}")

    try:
        # Delete any leftover webhook before starting polling
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


async def main():
    parser = argparse.ArgumentParser(description="WB FBS Telegram Bot Runner")
    parser.add_argument("--test-notify", action="store_true", help="Send test notification on startup")
    parser.add_argument("--check-only", action="store_true", help="Check bot connection and exit")
    args = parser.parse_args()

    logger.info("🔍 Looking for active sellers with Telegram configuration in DB...")
    sellers = get_active_sellers_with_tokens()

    if not sellers:
        logger.error("❌ No active sellers with valid Telegram tokens found in the database!")
        sys.exit(1)

    logger.info(f"Found {len(sellers)} seller(s) with Telegram configured.")

    for seller, token in sellers:
        logger.info(f"Checking seller '{seller.name}' (ID: {seller.id})...")
        ok = await check_bot(token, seller.name)
        if not ok:
            continue

        if args.check_only:
            continue

        if args.test_notify and seller.telegram_chat_ids:
            await send_test_notification(token, seller.telegram_chat_ids, str(seller.id))

        # Start polling for the primary active seller
        await run_polling(token, seller)
        break


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Bot stopped.")
