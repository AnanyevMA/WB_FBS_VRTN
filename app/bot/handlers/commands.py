"""
Telegram Bot Command Handlers — WB FBS Manager
"""
import logging
import random
from datetime import datetime, timezone
from decimal import Decimal

from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, func, and_
from sqlalchemy.orm import Session

from app.models.seller import Seller
from app.models.order import Order, OrderStatus, KizStatus
from app.models.supply import Supply
from app.models.kiz import KizOperation, KizOperationType
from app.models.audit import AuditLog
from app.services.encryption import decrypt
from app.services.telegram_service import TelegramService
from app.services.time_service import (
    format_seller_digest_time,
    get_server_time_info,
)
from app.bot.helpers import _get_active_seller, sync_engine
from app.bot.keyboards import get_main_reply_keyboard, get_orders_list_keyboard

logger = logging.getLogger(__name__)


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

    server_info = get_server_time_info()
    seller_now_str = format_seller_digest_time(seller)
    polling_sec = getattr(seller, "polling_interval_seconds", 60) or 60
    polling_status = f"✅ Включен ({polling_sec} сек.)" if seller.polling_enabled else "❌ Выключен"

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
        f"⚡ <b>WB Polling:</b> {polling_status}\n"
        f"🖥️ <b>Время сервера:</b> {server_info['server_local_now']} ({server_info['server_timezone']})\n"
        f"⏰ <b>Время магазина:</b> {seller_now_str}\n"
        f"🌅 <b>Расписание дайджеста:</b> {seller.digest_hour:02d}:{seller.digest_minute:02d} ({seller.digest_timezone or 'Europe/Moscow'})"
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
    for o in orders:
        price_val = float(o.price) if o.price else 0
        kiz_tag = " [⚠️ КИЗ]" if o.kiz_required else ""
        status_tag = f"[{o.status.value}]"
        text += f"• <code>#{o.id}</code> · {o.name or o.article} · <b>{price_val:.0f} ₽</b> {status_tag}{kiz_tag}\n"

    keyboard = get_orders_list_keyboard(orders, seller.id)
    await message.answer(text, reply_markup=keyboard)


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
            digest_time_str=format_seller_digest_time(seller),
        )
    finally:
        await svc.close()


async def handle_test_order(message: Message):
    chat_id = message.chat.id
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
        order.kiz_cz_status = None
        order.kiz_cz_status_updated_at = None
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


def register_command_handlers(router: Router):
    """Registers all command handlers on the provided router."""
    router.message.register(handle_start, CommandStart())
    router.message.register(handle_help, Command("help"))
    router.message.register(handle_help, F.text == "❓ Справка")
    router.message.register(handle_chat_id, Command("chatid", "id"))
    router.message.register(handle_bind_group, Command("bind", "bind_group"))
    router.message.register(handle_unbind_group, Command("unbind", "unbind_group"))
    router.message.register(handle_status, Command("status"))
    router.message.register(handle_status, F.text == "📊 Статистика")
    router.message.register(handle_orders_list, Command("orders"))
    router.message.register(handle_orders_list, F.text == "📦 Заказы в сборке")
    router.message.register(handle_supplies_list, Command("supplies"))
    router.message.register(handle_supplies_list, F.text == "🚚 Поставки")
    router.message.register(handle_digest, Command("digest"))
    router.message.register(handle_digest, F.text == "🌅 Утренний дайджест")
    router.message.register(handle_test_order, Command("test_order"))
    router.message.register(handle_test_order, F.text == "🧪 Тестовый заказ")
    router.message.register(handle_kiz_command, Command("kiz"))
