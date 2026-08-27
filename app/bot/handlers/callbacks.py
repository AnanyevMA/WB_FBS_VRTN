"""
Telegram Bot Inline Callback Query Handlers — WB FBS Manager
"""
import logging
import uuid
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, and_
from sqlalchemy.orm import Session

from app.models.order import Order, OrderStatus
from app.models.supply import Supply, SupplyStatus
from app.models.audit import AuditLog
from app.bot.helpers import sync_engine, _pending_kiz_input
from app.bot.keyboards import get_order_detail_keyboard

logger = logging.getLogger(__name__)


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

        is_active = order.status in [OrderStatus.NEW, OrderStatus.ASSEMBLING]
        keyboard = get_order_detail_keyboard(order.id, str(order.seller_id), is_active=is_active)

    await callback.message.answer(text, reply_markup=keyboard)
    await callback.answer()


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


async def cb_supply_create_all(callback: CallbackQuery):
    seller_id = callback.data.split(":")[-1]
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


def register_callback_handlers(router: Router):
    """Registers all callback query handlers on the provided router."""
    router.callback_query.register(cb_order_detail, F.data.startswith("order:detail:"))
    router.callback_query.register(cb_order_assemble, F.data.startswith("order:assemble:"))
    router.callback_query.register(cb_order_cancel, F.data.startswith("order:cancel:"))
    router.callback_query.register(cb_kiz_scan, F.data.startswith("kiz:scan:"))
    router.callback_query.register(cb_orders_pending, F.data.startswith("orders:pending:"))
    router.callback_query.register(cb_supply_create_all, F.data.startswith("supply:create_all:"))
