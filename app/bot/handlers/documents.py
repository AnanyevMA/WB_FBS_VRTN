"""
Telegram Bot Document Handlers — WB FBS Manager
"""
import io
import json
import logging
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.types import Message
from sqlalchemy import select

from app.models.seller import Seller
from app.models.audit import AuditLog
from app.database import AsyncSessionLocal
from app.services.archive_service import parse_wb_archive_excel, analyze_archive_data
from app.models.kiz import KizSignatureBatch, BatchStatus

logger = logging.getLogger(__name__)


async def handle_document(message: Message):
    """
    Обработка загрузки файла отчёта архива WB (.xlsx / .xls).
    Автоматически парсит номера чеков, даты, привязанные КИЗ и создаёт
    пакет операций со статусом PENDING_SIGNATURE для владельца ЭЦП в дашборде.
    """
    doc = message.document
    if not doc:
        return

    filename = doc.file_name or "archive.xlsx"
    filename_lower = filename.lower()
    if not (filename_lower.endswith(".xlsx") or filename_lower.endswith(".xls")):
        await message.reply(
            "⚠️ <b>Формат файла не поддерживается.</b>\n\n"
            "Пожалуйста, отправьте отчёт в формате <code>.xlsx</code> или <code>.xls</code> "
            "(выгрузка архива сборочных заданий с КИЗ из личного кабинета Wildberries)."
        )
        return

    status_msg = await message.reply("⏳ <i>Принят файл отчёта. Скачиваю и анализирую чеки WB...</i>")

    try:
        bot = message.bot
        file_obj = await bot.get_file(doc.file_id)
        file_bytes_io = io.BytesIO()
        await bot.download_file(file_obj.file_path, destination=file_bytes_io)
        file_bytes = file_bytes_io.getvalue()

        async with AsyncSessionLocal() as db:
            chat_id = message.chat.id
            stmt = select(Seller).where(Seller.is_active == True)
            res = await db.execute(stmt)
            sellers = res.scalars().all()

            target_seller = None
            for s in sellers:
                raw_ids = s.telegram_chat_ids
                if isinstance(raw_ids, str):
                    try:
                        raw_ids = json.loads(raw_ids)
                    except Exception:
                        raw_ids = [raw_ids]
                if raw_ids and isinstance(raw_ids, (list, tuple)):
                    if str(chat_id) in [str(c).strip() for c in raw_ids]:
                        target_seller = s
                        break

            if not target_seller and sellers:
                target_seller = sellers[0]

            if not target_seller:
                await status_msg.edit_text("❌ <b>Ошибка:</b> Не найден активный магазин для этого чата.")
                return

            # Парсинг и анализ Excel-отчета
            parsed_sheets = parse_wb_archive_excel(file_bytes)
            analysis = await analyze_archive_data(seller=target_seller, archive_data=parsed_sheets, db=db)

            withdrawals = analysis.get("withdrawals", [])
            returns = analysis.get("returns", [])
            summary = analysis.get("summary", {})

            sales_count = len(withdrawals)
            returns_count = len(returns)
            already_withdrawn = sum(1 for w in withdrawals if not w.get("needs_withdrawal", True))
            total_processed = summary.get("total_rows") or (sales_count + returns_count)

            # Создаем пакет на подписание в БД
            new_batch = KizSignatureBatch(
                seller_id=target_seller.id,
                filename=filename,
                source="telegram",
                status=BatchStatus.PENDING_SIGNATURE,
                sales_count=sales_count,
                returns_count=returns_count,
                already_withdrawn_count=already_withdrawn,
                total_count=total_processed,
                data_payload=analysis,
            )
            db.add(new_batch)

            # Обновляем дату последней загрузки для сброса напоминаний
            target_seller.last_archive_uploaded_at = datetime.now(timezone.utc)

            audit = AuditLog(
                seller_id=str(target_seller.id),
                agent="telegram_bot",
                action="TELEGRAM_ARCHIVE_UPLOAD",
                entity_type="archive_batch",
                entity_id=new_batch.id,
                payload={
                    "filename": filename,
                    "sales": sales_count,
                    "returns": returns_count,
                    "total": total_processed,
                    "chat_id": chat_id,
                },
            )
            db.add(audit)
            await db.commit()

        reply_text = (
            f"✅ <b>Отчёт Wildberries успешно обработан!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📁 <b>Файл:</b> <code>{filename}</code>\n"
            f"🏪 <b>Магазин:</b> {target_seller.name}\n\n"
            f"📊 <b>Результаты анализа операций:</b>\n"
            f"• 🟢 <b>Продажи (с чеками на вывод):</b> <b>{sales_count}</b> шт.\n"
            f"• 🔄 <b>Возвраты (на ввод в оборот):</b> <b>{returns_count}</b> шт.\n"
            f"• ⚪ <b>Уже выбыли ранее:</b> {already_withdrawn} шт.\n"
            f"• 📦 <b>Всего записей в отчёте:</b> {total_processed} шт.\n\n"
            f"🔏 <b>Пакет №<code>{new_batch.id[:8]}</code> добавлен в очередь на подписание ЭЦП!</b>\n"
            f"Владелец ЭЦП может открыть веб-дашборд в разделе <b>«🔏 Очередь ЭЦП»</b> и подписать документы в 1 клик."
        )
        await status_msg.edit_text(reply_text)

    except Exception as e:
        logger.error(f"Error handling document in telegram bot: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ <b>Ошибка при обработке файла:</b>\n<code>{str(e)}</code>")


def register_document_handlers(router: Router):
    """Registers all document handlers on the provided router."""
    router.message.register(handle_document, F.document)
