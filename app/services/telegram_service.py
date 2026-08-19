"""
Telegram Notification Service — отправка Push-уведомлений менеджерам
Использует aiogram 3.x для отправки сообщений с inline-кнопками
"""
import asyncio
import logging
from typing import Optional

from aiogram import Bot
try:
    from aiogram.client.default import DefaultBotProperties
except (ImportError, ModuleNotFoundError):
    DefaultBotProperties = None

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)


class TelegramService:
    """
    Сервис отправки уведомлений в Telegram.
    Каждый продавец имеет свой бот-токен и список chat_ids.
    """

    def __init__(self, bot_token: str):
        self.bot_token = bot_token
        self._bot: Optional[Bot] = None

    async def _get_bot(self) -> Bot:
        if self._bot is None:
            if DefaultBotProperties is not None:
                self._bot = Bot(token=self.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            else:
                self._bot = Bot(token=self.bot_token, parse_mode=ParseMode.HTML)
        return self._bot

    async def close(self):
        if self._bot:
            await self._bot.session.close()
            self._bot = None

    async def send_new_order_notification(
        self,
        chat_ids: list[str | int],
        order_id: int,
        order_data: dict,
    ) -> bool:
        """
        Отправляет уведомление о новом сборочном задании с inline-кнопками.

        Пример уведомления:
        🆕 НОВЫЙ ЗАКАЗ FBS #12345678
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━
        📦 Товар: Футболка синяя M
        🏷️ КИЗ: ТРЕБУЕТСЯ ⚠️
        ⏰ Срок: до 13:00 завтра
        💰 Цена: 1 490 ₽
        """
        kiz_required = order_data.get("kiz_required", False)
        kiz_icon = "⚠️ ТРЕБУЕТСЯ" if kiz_required else "✅ не нужен"

        price = order_data.get("price", 0)
        price_str = f"{price / 100:.0f} ₽" if isinstance(price, int) else f"{price} ₽"

        article = order_data.get("article", "—")
        name = order_data.get("name", "—")
        subject = order_data.get("subject", "—")
        brand = order_data.get("brand", "—")

        text = (
            f"🆕 <b>НОВЫЙ ЗАКАЗ FBS #{order_id}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>Товар:</b> {name}\n"
            f"🔖 <b>Бренд:</b> {brand}\n"
            f"📁 <b>Категория:</b> {subject}\n"
            f"📝 <b>Артикул:</b> {article}\n"
            f"🏷️ <b>КИЗ:</b> {kiz_icon}\n"
            f"💰 <b>Цена:</b> {price_str}"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📋 Детали",
                        callback_data=f"order:detail:{order_id}"
                    ),
                    InlineKeyboardButton(
                        text="✅ В сборку",
                        callback_data=f"order:assemble:{order_id}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Отклонить",
                        callback_data=f"order:cancel:{order_id}"
                    ),
                ],
            ]
        )

        return await self._broadcast(chat_ids, text, keyboard)

    async def send_kiz_required_alert(
        self,
        chat_ids: list[str | int],
        order_id: int,
        order_name: str,
    ) -> bool:
        """Напоминание: к заказу не привязан КИЗ."""
        text = (
            f"⚠️ <b>ТРЕБУЕТСЯ КИЗ</b>\n"
            f"Заказ #{order_id} ({order_name})\n"
            f"Товар подлежит маркировке. Отсканируйте КИЗ."
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="📷 Сканировать КИЗ",
                    callback_data=f"kiz:scan:{order_id}"
                )
            ]]
        )
        return await self._broadcast(chat_ids, text, keyboard)

    async def send_cz_withdrawal_status(
        self,
        chat_ids: list[str | int],
        order_id: int,
        success: bool,
        doc_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> bool:
        """Статус вывода из оборота."""
        if success:
            text = (
                f"✅ <b>Вывод из оборота выполнен</b>\n"
                f"Заказ #{order_id}\n"
                f"Документ ГИС МТ: <code>{doc_id}</code>"
            )
        else:
            text = (
                f"❌ <b>Ошибка вывода из оборота</b>\n"
                f"Заказ #{order_id}\n"
                f"Ошибка: {error or 'неизвестная ошибка'}"
            )
        return await self._broadcast(chat_ids, text)

    async def send_supply_delivered(
        self,
        chat_ids: list[str | int],
        supply_id: str,
        orders_count: int,
    ) -> bool:
        """Уведомление об успешной передаче поставки в доставку."""
        text = (
            f"🚚 <b>Поставка передана в доставку</b>\n"
            f"ID поставки: <code>{supply_id}</code>\n"
            f"Заказов в поставке: {orders_count}"
        )
        return await self._broadcast(chat_ids, text)

    async def send_error_alert(
        self,
        chat_ids: list[str | int],
        agent: str,
        message: str,
    ) -> bool:
        """Алерт об ошибке агента (эскалация)."""
        text = (
            f"🚨 <b>ОШИБКА АГЕНТА</b>\n"
            f"Агент: {agent}\n"
            f"Сообщение: {message}\n"
            f"Требуется ручное вмешательство!"
        )
        return await self._broadcast(chat_ids, text)

    async def send_batch_orders_notification(
        self,
        chat_ids: list[str | int],
        seller_id: str,
        orders: list[dict],
    ) -> bool:
        """
        Пакетное уведомление о нескольких новых заказах за один цикл опроса.

        orders: список словарей с ключами:
            id, name, price (коп.), kiz_required, article
        """
        count = len(orders)
        kiz_count = sum(1 for o in orders if o.get("kiz_required"))
        total_price = sum(o.get("price", 0) for o in orders)
        total_rub = total_price / 100 if isinstance(total_price, int) else total_price

        lines = []
        for i, o in enumerate(orders[:10], start=1):  # показываем максимум 10
            name = o.get("name") or o.get("article") or f"Заказ #{o.get('id')}"
            price = o.get("price", 0)
            price_rub = price / 100 if isinstance(price, int) else price
            kiz_tag = " ⚠️<b>КИЗ</b>" if o.get("kiz_required") else ""
            lines.append(f"  {i}. <code>#{o.get('id')}</code> · {name}\n     💰 {price_rub:.0f} ₽{kiz_tag}")

        if count > 10:
            lines.append(f"  <i>... и ещё {count - 10} заказов</i>")

        kiz_line = (
            f"\n⚠️ <b>Требуют КИЗ:</b> {kiz_count} из {count}"
            if kiz_count > 0
            else ""
        )

        text = (
            f"📦 <b>НОВЫЕ ЗАКАЗЫ FBS — {count} шт.</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(lines)
            + f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Итого:</b> {total_rub:.0f} ₽"
            + kiz_line
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📋 Все необработанные",
                        callback_data=f"orders:pending:{seller_id}",
                    ),
                    InlineKeyboardButton(
                        text="📦 Сформировать поставку",
                        callback_data=f"supply:create_all:{seller_id}",
                    ),
                ]
            ]
        )
        return await self._broadcast(chat_ids, text, keyboard)

    async def send_morning_digest(
        self,
        chat_ids: list[str | int],
        seller_id: str,
        pending_orders: list[dict],
        digest_time_str: str,
    ) -> bool:
        """
        Утренний дайджест: сводка всех необработанных заказов.

        Отправляется ежедневно в настроенное продавцом время.
        Если заказов нет — отправляет «Заказов нет» и возвращает True.

        pending_orders: список словарей id, name, article, price (коп.),
                        kiz_required, wb_created_at (ISO-строка)
        digest_time_str: человекочитаемое время, например «08:00 Europe/Moscow»
        """
        from datetime import datetime, timezone as tz

        count = len(pending_orders)

        # --- Нет заказов ---
        if count == 0:
            text = (
                f"🌅 <b>Доброе утро!</b> ({digest_time_str})\n\n"
                f"✅ Необработанных заказов нет. Хорошего дня!"
            )
            return await self._broadcast(chat_ids, text)

        # --- Есть заказы ---
        kiz_count = sum(1 for o in pending_orders if o.get("kiz_required"))
        total_price = sum(o.get("price", 0) for o in pending_orders)
        total_rub = total_price / 100 if isinstance(total_price, int) else float(total_price)

        # Oldest order age
        oldest_age_str = ""
        try:
            dates = []
            for o in pending_orders:
                raw = o.get("wb_created_at")
                if raw:
                    if isinstance(raw, str):
                        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    else:
                        dt = raw
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=tz.utc)
                    dates.append(dt)
            if dates:
                oldest = min(dates)
                age_h = (datetime.now(tz.utc) - oldest).total_seconds() / 3600
                oldest_age_str = f"\n🕐 <b>Самый старый:</b> {age_h:.0f} ч. назад"
        except Exception:
            pass

        # Build order lines (up to 10)
        lines = []
        for i, o in enumerate(pending_orders[:10], start=1):
            name = o.get("name") or o.get("article") or f"Заказ #{o.get('id')}"
            price = o.get("price", 0)
            price_rub = price / 100 if isinstance(price, int) else float(price)
            kiz_tag = " ⚠️" if o.get("kiz_required") else ""

            age_tag = ""
            try:
                raw = o.get("wb_created_at")
                if raw:
                    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=tz.utc)
                    age_h = (datetime.now(tz.utc) - dt).total_seconds() / 3600
                    age_tag = f" ({age_h:.0f}ч)"
            except Exception:
                pass

            lines.append(
                f"  {i}. <code>#{o.get('id')}</code> · {name}\n"
                f"     💰 {price_rub:.0f} ₽{kiz_tag}{age_tag}"
            )

        if count > 10:
            lines.append(f"  <i>... и ещё {count - 10} заказов</i>")

        kiz_line = (
            f"\n⚠️ <b>Требуют КИЗ:</b> {kiz_count} из {count}"
            if kiz_count > 0
            else ""
        )

        text = (
            f"🌅 <b>Доброе утро!</b> ({digest_time_str})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Ожидает сборки:</b> {count} заказов\n"
            f"💰 <b>Сумма:</b> {total_rub:.0f} ₽"
            + kiz_line
            + oldest_age_str
            + f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(lines)
            + "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"📦 Сформировать поставку ({count} шт.)",
                        callback_data=f"supply:create_all:{seller_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="📋 Показать полный список",
                        callback_data=f"orders:pending:{seller_id}",
                    ),
                ],
            ]
        )
        return await self._broadcast(chat_ids, text, keyboard)

    async def send_text(
        self,
        chat_ids: list[str | int],
        text: str,
    ) -> bool:
        """Отправить произвольное сообщение."""
        return await self._broadcast(chat_ids, text)

    async def _broadcast(
        self,
        chat_ids: list[str | int],
        text: str,
        keyboard: Optional[InlineKeyboardMarkup] = None,
    ) -> bool:
        """Отправить сообщение всем указанным chat_ids."""
        bot = await self._get_bot()
        success_count = 0

        for chat_id in chat_ids:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=keyboard,
                )
                success_count += 1
                # Anti-flood delay
                await asyncio.sleep(0.05)
            except TelegramAPIError as e:
                logger.error(f"Telegram send error to {chat_id}: {e}")
            except Exception as e:
                logger.error(f"Unexpected error sending to {chat_id}: {e}")

        return success_count > 0


def get_telegram_service(bot_token: str) -> TelegramService:
    """Factory function."""
    return TelegramService(bot_token)
