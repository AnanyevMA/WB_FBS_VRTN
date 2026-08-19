"""
Telegram Bot Handler & Dispatcher — WB FBS Manager
Интерактивный бот для управления сборочными заданиями WB FBS и Честным Знаком.
Настроен на строгую реакцию только на команды (игнорирует человеческую переписку в группах).
"""
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Dict, Any

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from sqlalchemy import select, func, and_, update
from sqlalchemy.orm import Session
from sqlalchemy import create_engine

from app.config import settings
from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus
from app.models.supply import Supply, SupplyStatus
from app.models.kiz import KizOperation, KizOperationType
from app.models.audit import AuditLog
from app.services.encryption import decrypt
from app.services.telegram_service import TelegramService

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


def create_bot_router() -> Router:
    """Creates aiogram router with all command and callback handlers."""
    router = Router(name="main_bot_router")

    # -------------------------------------------------------------------------
    # Command Handlers (triggered ONLY by /commands)
    # -------------------------------------------------------------------------

    @router.message(CommandStart())
    async def handle_start(message: Message):
        chat_id = message.chat.id
        is_private = message.chat.type == "private"

        with Session(sync_engine) as db:
            seller = _get_active_seller(db, chat_id)
            seller_name = seller.name if seller else "Не привязан"
            total_orders = (
                db.scalar(
                    select(func.count(Order.id)).where(Order.seller_id == seller.id)
                )
                if seller
                else 0
            )
            pending_orders = (
                db.scalar(
                    select(func.count(Order.id)).where(
                        and_(
                            Order.seller_id == seller.id,
                            Order.status.in_([OrderStatus.NEW, OrderStatus.ASSEMBLING]),
                        )
                    )
                )
                if seller
                else 0
            )

        text = (
            f"👋 <b>Здравствуйте!</b>\n\n"
            f"🤖 Бот управления <b>WB FBS Manager</b> активен.\n\n"
            f"🏢 <b>Магазин:</b> {seller_name}\n"
            f"🆔 <b>Chat ID:</b> <code>{chat_id}</code>\n"
            f"📦 <b>Ожидают сборки:</b> {pending_orders} шт.\n"
            f"📊 <b>Всего заказов в базе:</b> {total_orders} шт.\n\n"
            f"<b>Доступные команды:</b>\n"
            f"• /orders — Заказы на сборку\n"
            f"• /status — Статистика магазина\n"
            f"• /supplies — Список поставок\n"
            f"• /digest — Сводка за день\n"
            f"• /bind — Привязать этот чат к рассылке\n"
            f"• /unbind — Отвязать этот чат\n"
            f"• /test_order — Тестовое сборочное задание\n"
            f"• /help — Справка"
        )
        reply_markup = get_main_reply_keyboard() if is_private else None
        await message.answer(text, reply_markup=reply_markup)

    @router.message(Command("help"))
    @router.message(F.text == "❓ Справка")
    async def handle_help(message: Message):
        is_private = message.chat.type == "private"
        text = (
            "📖 <b>Справка по боту WB FBS Manager</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔹 <b>Уведомления о заказах:</b>\n"
            "При поступлении нового заказа бот присылает карточку с кнопками управления:\n"
            "• <b>📋 Детали</b> — подробная информация о товаре и КИЗ\n"
            "• <b>✅ В сборку</b> — перевод заказа в статус сборки\n"
            "• <b>📷 Сканировать КИЗ</b> — привязка DataMatrix маркировки\n"
            "• <b>❌ Отклонить</b> — отмена заказа\n\n"
            "🔹 <b>Привязка бота к группе:</b>\n"
            "1. Добавьте бота в группу Telegram\n"
            "2. В группе отправьте команду <code>/bind</code>\n"
            "3. Бот автоматически добавит группу в рассылку заказов!\n\n"
            "🔹 <b>Привязка Честного Знака (КИЗ):</b>\n"
            "• Нажмите «📷 Сканировать КИЗ» под заказом и отправьте код маркировки\n"
            "• Или в группе используйте: <code>/kiz &lt;ID_заказа&gt; &lt;код_маркировки&gt;</code>\n\n"
            "🔹 <b>Команды бота:</b>\n"
            "/orders — список сборочных заданий\n"
            "/status — статистика и состояние системы\n"
            "/supplies — список поставок\n"
            "/digest — утренний дайджест заказов\n"
            "/bind — привязать текущий чат/группу к рассылке\n"
            "/unbind — отвязать текущий чат/группу\n"
            "/chatid — узнать ID текущего чата\n"
            "/test_order — отправить тестовый заказ\n"
        )
        reply_markup = get_main_reply_keyboard() if is_private else None
        await message.answer(text, reply_markup=reply_markup)

    @router.message(Command("chatid", "id"))
    async def handle_chat_id(message: Message):
        chat_id = message.chat.id
        chat_type = message.chat.type
        chat_title = message.chat.title or message.chat.username or message.chat.first_name or "Диалог"
        with Session(sync_engine) as db:
            seller = _get_active_seller(db, chat_id)
            is_bound = False
            if seller and seller.telegram_chat_ids:
                is_bound = str(chat_id) in [str(c) for c in seller.telegram_chat_ids]

        status_icon = "✅ Привязан к уведомлениям" if is_bound else "⚪ Не привязан к уведомлениям"
        text = (
            f"🆔 <b>Информация о чате</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>ID чата / группы:</b> <code>{chat_id}</code>\n"
            f"🏷️ <b>Название:</b> {chat_title}\n"
            f"📂 <b>Тип:</b> <code>{chat_type}</code>\n"
            f"🔔 <b>Статус:</b> {status_icon}\n\n"
            f"💡 <i>Чтобы привязать этот чат/группу к магазину, отправьте:</i> /bind\n"
            f"💡 <i>Чтобы отвязать:</i> /unbind"
        )
        await message.answer(text)

    @router.message(Command("bind", "bind_group"))
    async def handle_bind_group(message: Message):
        chat_id = str(message.chat.id)
        chat_title = message.chat.title or message.chat.username or "Этот чат"
        with Session(sync_engine) as db:
            seller = _get_active_seller(db)
            if not seller:
                await message.answer("❌ Активный продавец не найден в базе данных.")
                return

            current_ids = list(seller.telegram_chat_ids or [])
            current_ids_str = [str(c) for c in current_ids]
            if chat_id not in current_ids_str:
                current_ids_str.append(chat_id)
                seller.telegram_chat_ids = current_ids_str
                db.commit()
                text = (
                    f"✅ <b>Чат успешно привязан!</b>\n\n"
                    f"🏢 <b>Магазин:</b> {seller.name}\n"
                    f"👥 <b>Чат/Группа:</b> {chat_title}\n"
                    f"🆔 <b>Chat ID:</b> <code>{chat_id}</code>\n\n"
                    f"🚀 Теперь все новые заказы FBS, утренние дайджесты и важные алерты будут отправляться сюда!"
                )
            else:
                text = (
                    f"ℹ️ <b>Чат уже привязан!</b>\n\n"
                    f"Чат/Группа <code>{chat_id}</code> уже находится в списке рассылки магазина «{seller.name}»."
                )

        await message.answer(text)

    @router.message(Command("unbind", "unbind_group"))
    async def handle_unbind_group(message: Message):
        chat_id = str(message.chat.id)
        with Session(sync_engine) as db:
            seller = _get_active_seller(db)
            if not seller:
                await message.answer("❌ Продавец не найден.")
                return

            current_ids = list(seller.telegram_chat_ids or [])
            current_ids_str = [str(c) for c in current_ids]
            if chat_id in current_ids_str:
                current_ids_str.remove(chat_id)
                seller.telegram_chat_ids = current_ids_str
                db.commit()
                text = f"✅ Чат <code>{chat_id}</code> успешно удален из списка рассылки магазина «{seller.name}»."
            else:
                text = f"ℹ️ Чат <code>{chat_id}</code> не был привязан к рассылке."

        await message.answer(text)

    @router.message(Command("status"))
    @router.message(F.text == "📊 Статистика")
    async def handle_status(message: Message):
        chat_id = message.chat.id
        with Session(sync_engine) as db:
            seller = _get_active_seller(db, chat_id)
            if not seller:
                await message.answer("❌ Продавец не найден в базе данных.")
                return

            total_orders = db.scalar(
                select(func.count(Order.id)).where(Order.seller_id == seller.id)
            ) or 0
            new_orders = db.scalar(
                select(func.count(Order.id)).where(
                    and_(Order.seller_id == seller.id, Order.status == OrderStatus.NEW)
                )
            ) or 0
            assembling_orders = db.scalar(
                select(func.count(Order.id)).where(
                    and_(Order.seller_id == seller.id, Order.status == OrderStatus.ASSEMBLING)
                )
            ) or 0
            delivering_orders = db.scalar(
                select(func.count(Order.id)).where(
                    and_(Order.seller_id == seller.id, Order.status == OrderStatus.DELIVERING)
                )
            ) or 0
            kiz_req_count = db.scalar(
                select(func.count(Order.id)).where(
                    and_(
                        Order.seller_id == seller.id,
                        Order.kiz_required == True,
                        Order.kiz_status.in_([KizStatus.PENDING, KizStatus.ERROR]),
                    )
                )
            ) or 0
            supplies_count = db.scalar(
                select(func.count(Supply.id)).where(Supply.seller_id == seller.id)
            ) or 0

        kiz_warning = f"\n⚠️ <b>Ожидают КИЗ:</b> {kiz_req_count} шт." if kiz_req_count > 0 else ""

        text = (
            f"📊 <b>Статус магазина «{seller.name}»</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>Всего заказов:</b> {total_orders} шт.\n"
            f"🆕 <b>Новые (NEW):</b> {new_orders} шт.\n"
            f"🔨 <b>В сборке (ASSEMBLING):</b> {assembling_orders} шт.\n"
            f"🚚 <b>В доставке (DELIVERING):</b> {delivering_orders} шт.\n"
            f"📦 <b>Поставок создано:</b> {supplies_count} шт."
            f"{kiz_warning}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ <b>WB Polling:</b> {'Включен' if seller.polling_enabled else 'Выключен'}\n"
            f"🌅 <b>Дайджест:</b> {seller.digest_hour:02d}:{seller.digest_minute:02d} ({seller.digest_timezone})"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📋 Заказы в сборке",
                        callback_data=f"orders:pending:{seller.id}",
                    ),
                    InlineKeyboardButton(
                        text="📦 Создать поставку",
                        callback_data=f"supply:create_all:{seller.id}",
                    ),
                ]
            ]
        )
        await message.answer(text, reply_markup=keyboard)

    @router.message(Command("orders"))
    @router.message(F.text == "📦 Заказы в сборке")
    async def handle_orders_list(message: Message):
        chat_id = message.chat.id
        is_private = message.chat.type == "private"
        with Session(sync_engine) as db:
            seller = _get_active_seller(db, chat_id)
            if not seller:
                await message.answer("❌ Продавец не найден.")
                return

            orders = db.execute(
                select(Order)
                .where(
                    and_(
                        Order.seller_id == seller.id,
                        Order.status.in_([OrderStatus.NEW, OrderStatus.ASSEMBLING, OrderStatus.DELIVERING]),
                    )
                )
                .order_by(Order.created_at.desc())
                .limit(10)
            ).scalars().all()

        if not orders:
            await message.answer(
                "✅ <b>Нет активных заказов</b>, ожидающих сборки или доставки.",
                reply_markup=get_main_reply_keyboard() if is_private else None,
            )
            return

        text = f"📦 <b>Активные заказы ({len(orders)} шт.):</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        keyboard_buttons = []

        for o in orders:
            price_val = float(o.price) if o.price else 0
            kiz_tag = " [⚠️ КИЗ]" if o.kiz_required else ""
            status_tag = f"[{o.status.value}]"
            text += f"• <code>#{o.id}</code> · {o.name or o.article} · <b>{price_val:.0f} ₽</b> {status_tag}{kiz_tag}\n"

            keyboard_buttons.append([
                InlineKeyboardButton(
                    text=f"📋 #{o.id} - Детали",
                    callback_data=f"order:detail:{o.id}"
                ),
                InlineKeyboardButton(
                    text=f"✅ В сборку",
                    callback_data=f"order:assemble:{o.id}"
                ),
            ])

        keyboard_buttons.append([
            InlineKeyboardButton(
                text="📦 Сформировать поставку из всех",
                callback_data=f"supply:create_all:{seller.id}"
            )
        ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        await message.answer(text, reply_markup=keyboard)

    @router.message(Command("supplies"))
    @router.message(F.text == "🚚 Поставки")
    async def handle_supplies_list(message: Message):
        chat_id = message.chat.id
        with Session(sync_engine) as db:
            seller = _get_active_seller(db, chat_id)
            if not seller:
                await message.answer("❌ Продавец не найден.")
                return

            supplies = db.execute(
                select(Supply)
                .where(Supply.seller_id == seller.id)
                .order_by(Supply.created_at.desc())
                .limit(5)
            ).scalars().all()

            if not supplies:
                await message.answer(
                    "🚚 <b>Поставок пока нет.</b>\nВы можете создать новую поставку из активных заказов.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(
                                text="📦 Сформировать поставку",
                                callback_data=f"supply:create_all:{seller.id}"
                            )
                        ]]
                    )
                )
                return

            text = f"🚚 <b>Последние поставки ({len(supplies)} шт.):</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            for s in supplies:
                orders_cnt = db.scalar(
                    select(func.count(Order.id)).where(Order.supply_id == s.id)
                ) or 0
                date_str = s.created_at.strftime("%d.%m.%Y %H:%M") if s.created_at else "—"
                text += (
                    f"📦 <code>{s.wb_supply_id}</code>\n"
                    f"   <b>Название:</b> {s.name or 'Без имени'}\n"
                    f"   <b>Статус:</b> {s.status.value} | <b>Заказов:</b> {orders_cnt}\n"
                    f"   <b>Дата:</b> {date_str}\n\n"
                )

            await message.answer(
                text,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(
                            text="➕ Создать новую поставку",
                            callback_data=f"supply:create_all:{seller.id}"
                        )
                    ]]
                )
            )

    @router.message(Command("digest"))
    @router.message(F.text == "🌅 Утренний дайджест")
    async def handle_digest(message: Message):
        chat_id = message.chat.id
        with Session(sync_engine) as db:
            seller = _get_active_seller(db, chat_id)
            if not seller or not seller.telegram_bot_token_encrypted:
                await message.answer("❌ Продавец или токен бота не найдены.")
                return

            orders = db.execute(
                select(Order)
                .where(
                    and_(
                        Order.seller_id == seller.id,
                        Order.status.in_([OrderStatus.NEW, OrderStatus.ASSEMBLING]),
                    )
                )
                .order_by(Order.created_at.desc())
            ).scalars().all()

            pending_data = [
                {
                    "id": o.id,
                    "name": o.name,
                    "article": o.article,
                    "price": int(o.price * 100) if o.price else 0,
                    "kiz_required": o.kiz_required,
                    "wb_created_at": o.wb_created_at.isoformat() if o.wb_created_at else None,
                }
                for o in orders
            ]

        bot_token = decrypt(seller.telegram_bot_token_encrypted)
        svc = TelegramService(bot_token)
        try:
            await svc.send_morning_digest(
                chat_ids=[chat_id],
                seller_id=str(seller.id),
                pending_orders=pending_data,
                digest_time_str=datetime.now().strftime("%H:%M") + f" ({seller.digest_timezone})",
            )
        finally:
            await svc.close()

    @router.message(Command("test_order"))
    @router.message(F.text == "🧪 Тестовый заказ")
    async def handle_test_order(message: Message):
        chat_id = message.chat.id
        import random
        test_id = random.randint(5500000000, 5599999999)
        test_order_data = {
            "name": "Худи оверсайз VRTN Brown M",
            "brand": "VRTN",
            "subject": "Толстовки",
            "article": "vrtn-hood-brown-m",
            "kiz_required": True,
            "price": 349000,  # в копейках
        }

        with Session(sync_engine) as db:
            seller = _get_active_seller(db, chat_id)
            if not seller or not seller.telegram_bot_token_encrypted:
                await message.answer("❌ Продавец или токен бота не найдены.")
                return

            # Also create in DB for full interactivity
            new_order = Order(
                id=test_id,
                seller_id=seller.id,
                status=OrderStatus.NEW,
                wb_created_at=datetime.now(timezone.utc),
                article=test_order_data["article"],
                brand=test_order_data["brand"],
                subject=test_order_data["subject"],
                name=test_order_data["name"],
                price=Decimal("3490.00"),
                kiz_required=True,
                kiz_status=KizStatus.PENDING,
            )
            db.add(new_order)
            db.commit()

        bot_token = decrypt(seller.telegram_bot_token_encrypted)
        svc = TelegramService(bot_token)
        try:
            await svc.send_new_order_notification(
                chat_ids=[chat_id],
                order_id=test_id,
                order_data=test_order_data,
            )
        finally:
            await svc.close()

    @router.message(Command("kiz"))
    async def handle_kiz_command(message: Message):
        """Ручная привязка КИЗ через команду: /kiz <order_id> <kiz_code>"""
        args = (message.text or "").split(maxsplit=2)
        if len(args) < 3:
            await message.reply(
                "ℹ️ <b>Формат команды:</b>\n"
                "<code>/kiz &lt;ID_заказа&gt; &lt;код_маркировки&gt;</code>\n\n"
                "Пример:\n"
                "<code>/kiz 5462395042 0104601234567890215abcXYZ</code>"
            )
            return

        order_id_str, kiz_code = args[1].replace("#", "").strip(), args[2].strip()
        try:
            order_id = int(order_id_str)
        except ValueError:
            await message.reply("❌ Некорректный ID заказа. Укажите числовой номер.")
            return

        with Session(sync_engine) as db:
            order = db.get(Order, order_id)
            if not order:
                await message.reply(f"❌ Заказ #{order_id} не найден в базе данных.")
                return

            order.kiz_code = kiz_code
            order.kiz_status = KizStatus.ATTACHED
            order.kiz_cz_status = "INTRODUCED"
            order.kiz_cz_status_updated_at = datetime.now(timezone.utc)
            order.kiz_attached_at = datetime.now(timezone.utc)

            kiz_op = KizOperation(
                order_id=order.id,
                seller_id=order.seller_id,
                kiz_code=kiz_code,
                operation_type=KizOperationType.ATTACH,
                created_at=datetime.now(timezone.utc),
            )
            db.add(kiz_op)

            audit = AuditLog(
                seller_id=str(order.seller_id),
                agent="telegram_bot",
                action="ATTACH_KIZ_COMMAND",
                entity_type="order",
                entity_id=str(order.id),
                payload={"kiz_code": kiz_code[:20] + "...", "chat_id": message.chat.id},
                created_at=datetime.now(timezone.utc),
            )
            db.add(audit)
            db.commit()

        await message.reply(
            f"✅ <b>КИЗ успешно привязан к заказу #{order_id}!</b>\n"
            f"🏷️ КИЗ: <code>{kiz_code}</code>\n"
            f"📊 Статус маркировки: <b>ATTACHED</b>"
        )

    # -------------------------------------------------------------------------
    # Callback Query Handlers (Inline Buttons)
    # -------------------------------------------------------------------------

    @router.callback_query(F.data.startswith("order:detail:"))
    async def cb_order_detail(callback: CallbackQuery):
        order_id_str = callback.data.split(":")[-1]
        try:
            order_id = int(order_id_str)
        except ValueError:
            await callback.answer("Некорректный ID заказа", show_alert=True)
            return

        with Session(sync_engine) as db:
            order = db.get(Order, order_id)
            if not order:
                await callback.answer("Заказ не найден в базе", show_alert=True)
                return

            price_val = float(order.price) if order.price else 0
            kiz_status_str = "Требуется ⚠️" if order.kiz_required else "Не требуется ✅"
            kiz_code_str = f"<code>{order.kiz_code}</code>" if order.kiz_code else "Не привязан"
            supply_str = f"<code>{order.wb_supply_id}</code>" if order.wb_supply_id else "Не назначена"
            date_str = order.created_at.strftime("%d.%m.%Y %H:%M") if order.created_at else "—"

            wb_status_human = {
                "waiting": "Ожидает приемки СЦ 🚚",
                "sorted": "Отсортирован на СЦ 📦",
                "ready_for_pickup": "В ПВЗ (готов к выдаче) 🏪",
                "sold": "Выкуплен покупателем 💰",
                "canceled_by_client": "Отказ при получении на ПВЗ 🔄",
                "declined_by_client": "Отменен покупателем 🚫",
                "defect": "Брак ⚠️",
            }.get((order.wb_status or "").lower(), order.wb_status)

            wb_status_line = f"🚚 <b>Логистика WB:</b> {wb_status_human}\n" if order.wb_status else ""

            text = (
                f"📋 <b>Детали заказа #{order.id}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 <b>Товар:</b> {order.name or '—'}\n"
                f"🔖 <b>Бренд:</b> {order.brand or '—'}\n"
                f"📁 <b>Категория:</b> {order.subject or '—'}\n"
                f"📝 <b>Артикул:</b> <code>{order.article or '—'}</code>\n"
                f"💰 <b>Цена:</b> {price_val:.0f} ₽\n"
                f"📊 <b>Статус заказа:</b> <code>{order.status.value}</code>\n"
                f"{wb_status_line}"
                f"🏷️ <b>Маркировка КИЗ:</b> {kiz_status_str}\n"
                f"🔢 <b>Код КИЗ:</b> {kiz_code_str} ({order.kiz_status.value})\n"
                f"🚚 <b>Поставка:</b> {supply_str}\n"
                f"🕐 <b>Дата создания:</b> {date_str}"
            )

            buttons = []
            if order.status in [OrderStatus.NEW, OrderStatus.ASSEMBLING]:
                buttons.append([
                    InlineKeyboardButton(text="✅ В сборку", callback_data=f"order:assemble:{order.id}"),
                    InlineKeyboardButton(text="📷 Привязать КИЗ", callback_data=f"kiz:scan:{order.id}"),
                ])
                buttons.append([
                    InlineKeyboardButton(text="❌ Отклонить", callback_data=f"order:cancel:{order.id}"),
                ])

            buttons.append([
                InlineKeyboardButton(text="🔙 К списку заказов", callback_data=f"orders:pending:{order.seller_id}")
            ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.answer(text, reply_markup=keyboard)
        await callback.answer()

    @router.callback_query(F.data.startswith("order:assemble:"))
    async def cb_order_assemble(callback: CallbackQuery):
        order_id_str = callback.data.split(":")[-1]
        try:
            order_id = int(order_id_str)
        except ValueError:
            await callback.answer("Ошибка ID", show_alert=True)
            return

        with Session(sync_engine) as db:
            order = db.get(Order, order_id)
            if not order:
                await callback.answer("Заказ не найден", show_alert=True)
                return

            order.status = OrderStatus.ASSEMBLING
            audit = AuditLog(
                seller_id=str(order.seller_id),
                agent="telegram_bot",
                action="ORDER_ASSEMBLE_REQUEST",
                entity_type="order",
                entity_id=str(order.id),
                payload={"chat_id": callback.message.chat.id},
                created_at=datetime.now(timezone.utc),
            )
            db.add(audit)
            db.commit()

            kiz_alert = "\n⚠️ <b>Не забудьте привязать КИЗ (Честный Знак)!</b>" if (order.kiz_required and not order.kiz_code) else ""

        await callback.answer("✅ Заказ переведен в сборку!")
        await callback.message.reply(
            f"✅ Заказ <code>#{order_id}</code> успешно переведен в статус <b>В СБОРКЕ (ASSEMBLING)</b>.{kiz_alert}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(text="📷 Привязать КИЗ", callback_data=f"kiz:scan:{order_id}"),
                    InlineKeyboardButton(text="📋 Детали", callback_data=f"order:detail:{order_id}"),
                ]]
            )
        )

    @router.callback_query(F.data.startswith("order:cancel:"))
    async def cb_order_cancel(callback: CallbackQuery):
        order_id_str = callback.data.split(":")[-1]
        try:
            order_id = int(order_id_str)
        except ValueError:
            await callback.answer("Ошибка ID", show_alert=True)
            return

        with Session(sync_engine) as db:
            order = db.get(Order, order_id)
            if not order:
                await callback.answer("Заказ не найден", show_alert=True)
                return

            order.status = OrderStatus.CANCELLED
            audit = AuditLog(
                seller_id=str(order.seller_id),
                agent="telegram_bot",
                action="ORDER_CANCEL_REQUEST",
                entity_type="order",
                entity_id=str(order.id),
                payload={"chat_id": callback.message.chat.id},
                created_at=datetime.now(timezone.utc),
            )
            db.add(audit)
            db.commit()

        await callback.answer("❌ Заказ отклонен!")
        await callback.message.reply(f"❌ Заказ <code>#{order_id}</code> отменен (CANCELLED).")

    @router.callback_query(F.data.startswith("kiz:scan:"))
    async def cb_kiz_scan(callback: CallbackQuery):
        order_id_str = callback.data.split(":")[-1]
        try:
            order_id = int(order_id_str)
        except ValueError:
            await callback.answer("Ошибка ID", show_alert=True)
            return

        chat_id = callback.message.chat.id
        _pending_kiz_input[chat_id] = order_id

        text = (
            f"📷 <b>Привязка КИЗ к заказу #{order_id}</b>\n\n"
            f"Отсканируйте 2D-сканером код маркировки DataMatrix или вставьте/отправьте строку КИЗ (SGTIN) ответным сообщением.\n\n"
            f"<i>(Или в группе используйте команду: <code>/kiz {order_id} &lt;код&gt;</code>)</i>"
        )
        await callback.answer()
        await callback.message.reply(text)

    @router.callback_query(F.data.startswith("orders:pending:"))
    async def cb_orders_pending(callback: CallbackQuery):
        seller_id = callback.data.split(":")[-1]
        with Session(sync_engine) as db:
            orders = db.execute(
                select(Order)
                .where(
                    and_(
                        Order.seller_id == seller_id,
                        Order.status.in_([OrderStatus.NEW, OrderStatus.ASSEMBLING]),
                    )
                )
                .order_by(Order.created_at.desc())
                .limit(10)
            ).scalars().all()

        if not orders:
            await callback.answer("Нет необработанных заказов")
            await callback.message.reply("✅ Нет необработанных заказов на сборку.")
            return

        text = f"📋 <b>Необработанные заказы ({len(orders)} шт.):</b>\n"
        buttons = []
        for o in orders:
            price_val = float(o.price) if o.price else 0
            kiz_tag = " [⚠️ КИЗ]" if o.kiz_required else ""
            text += f"• <code>#{o.id}</code>: {o.name or o.article} · <b>{price_val:.0f} ₽</b>{kiz_tag}\n"
            buttons.append([
                InlineKeyboardButton(text=f"📋 #{o.id}", callback_data=f"order:detail:{o.id}"),
                InlineKeyboardButton(text="✅ В сборку", callback_data=f"order:assemble:{o.id}"),
            ])

        buttons.append([
            InlineKeyboardButton(text="📦 Сформировать поставку", callback_data=f"supply:create_all:{seller_id}")
        ])

        await callback.answer()
        await callback.message.reply(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    @router.callback_query(F.data.startswith("supply:create_all:"))
    async def cb_supply_create_all(callback: CallbackQuery):
        seller_id = callback.data.split(":")[-1]
        import uuid
        with Session(sync_engine) as db:
            orders = db.execute(
                select(Order).where(
                    and_(
                        Order.seller_id == seller_id,
                        Order.status.in_([OrderStatus.NEW, OrderStatus.ASSEMBLING]),
                        Order.supply_id == None,
                    )
                )
            ).scalars().all()

            if not orders:
                await callback.answer("Нет заказов для включения в поставку", show_alert=True)
                await callback.message.reply("ℹ️ Нет свободных заказов для формирования поставки.")
                return

            wb_sup_id = f"WB-SUP-{uuid.uuid4().hex[:8].upper()}"
            sup_name = f"Поставка от {datetime.now().strftime('%d.%m.%Y %H:%M')}"
            supply = Supply(
                seller_id=seller_id,
                wb_supply_id=wb_sup_id,
                name=sup_name,
                status=SupplyStatus.CREATED,
                created_at=datetime.now(timezone.utc),
            )
            db.add(supply)
            db.flush()

            for o in orders:
                o.supply_id = supply.id
                o.wb_supply_id = wb_sup_id

            audit = AuditLog(
                seller_id=seller_id,
                agent="telegram_bot",
                action="CREATE_SUPPLY",
                entity_type="supply",
                entity_id=str(supply.id),
                payload={"wb_supply_id": wb_sup_id, "orders_count": len(orders)},
                created_at=datetime.now(timezone.utc),
            )
            db.add(audit)
            db.commit()

        await callback.answer("📦 Поставка успешно сформирована!")
        await callback.message.reply(
            f"🚚 <b>Поставка успешно сформирована!</b>\n\n"
            f"🆔 ID поставки: <code>{wb_sup_id}</code>\n"
            f"📦 Заказов включено: <b>{len(orders)} шт.</b>\n"
            f"📊 Статус: <code>CREATED</code>"
        )

    # -------------------------------------------------------------------------
    # Text Message Handler (Strict Filter: Ignores conversations in groups)
    # -------------------------------------------------------------------------

    @router.message(F.text)
    async def handle_text_message(message: Message):
        # 1. Completely ignore ALL human conversations in groups/supergroups
        if message.chat.type in ["group", "supergroup", "channel"]:
            return

        # 2. In private chats, only handle if user is explicitly inputting a KIZ code
        chat_id = message.chat.id
        text = message.text.strip()

        # If it's a command, it's already caught by Command() handlers above
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
            order.kiz_cz_status = "INTRODUCED"
            order.kiz_cz_status_updated_at = datetime.now(timezone.utc)
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

    return router
