"""
Unit Tests for Telegram Bot Document Upload Handler (Archive XLSX Processing)
"""
import pytest
import io
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import openpyxl

from app.database import AsyncSessionLocal, init_db
from app.models.seller import Seller
from app.models.order import Order, KizStatus, OrderStatus
from app.models.kiz import KizSignatureBatch, BatchStatus
from app.services.encryption import encrypt


def _create_mock_wb_archive_bytes(order1_id: int, order2_id: int) -> bytes:
    """Creates a minimal mock WB archive Excel workbook in memory."""
    wb = openpyxl.Workbook()
    # 1. Sheet "КИЗ"
    ws_kiz = wb.active
    ws_kiz.title = "КИЗ"
    ws_kiz.append(["№ задания", "Стикер", "КИЗ", "Номер чека", "Номер фискального накопителя", "Дата", "Тип операции", "Стоимость"])
    ws_kiz.append([order1_id, "998877", "0104630199251318215TEST1", "ЧЕК-112233", "99990001", "2026-08-25", "Продажа", 1990])
    ws_kiz.append([order2_id, "998878", "0104630199251318215TEST2", "", "", "2026-08-26", "Возврат", 1490])

    # 2. Sheet "Сборочные задания"
    ws_tasks = wb.create_sheet(title="Сборочные задания")
    ws_tasks.append(["№ задания", "Стикер", "Артикул продавца", "Наименование", "Статус задания"])
    ws_tasks.append([order1_id, "998877", "vrtn-hood-01", "Худи VRTN", "продано"])
    ws_tasks.append([order2_id, "998878", "vrtn-tshirt-02", "Футболка VRTN", "отказ"])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_bot_archive_excel_parsing_and_batch_creation():
    await init_db()

    seller_id = str(uuid.uuid4())
    order1_id = int(str(uuid.uuid4().int)[:9])
    order2_id = int(str(uuid.uuid4().int)[:9])
    chat_id = int(str(uuid.uuid4().int)[:9])

    async with AsyncSessionLocal() as session:
        seller = Seller(
            id=seller_id,
            name=f"Bot Test Seller {chat_id}",
            wb_api_token_encrypted=encrypt("wb_test_token"),
            telegram_bot_token_encrypted=encrypt("tg_test_token"),
            telegram_chat_ids=[str(chat_id)],
            is_active=True,
            archive_reminder_enabled=True,
            archive_reminder_days=2,
            last_archive_uploaded_at=None,
        )
        session.add(seller)

        order1 = Order(
            id=order1_id,
            seller_id=seller_id,
            name="Худи VRTN",
            article="vrtn-hood-01",
            price=1990,
            status=OrderStatus.DELIVERING,
            kiz_required=True,
            kiz_code="0104630199251318215TEST1",
            kiz_status=KizStatus.ATTACHED,
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        order2 = Order(
            id=order2_id,
            seller_id=seller_id,
            name="Футболка VRTN",
            article="vrtn-tshirt-02",
            price=1490,
            status=OrderStatus.CANCELLED,
            kiz_required=True,
            kiz_code="0104630199251318215TEST2",
            kiz_status=KizStatus.WITHDRAWN,
            wb_created_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        session.add_all([order1, order2])
        await session.commit()

    # Emulate Telegram Message & Bot handling
    mock_file_bytes = _create_mock_wb_archive_bytes(order1_id, order2_id)

    from app.bot import create_bot_router
    router = create_bot_router()

    # Find the document handler in router
    doc_handler = next((h.callback for h in router.message.handlers if getattr(h.callback, "__name__", "") == "handle_document"), None)
    assert doc_handler is not None, "Document handler must be registered in bot router"

    mock_bot = AsyncMock()
    mock_file_info = MagicMock()
    mock_file_info.file_path = "documents/mock_archive.xlsx"
    mock_bot.get_file.return_value = mock_file_info

    async def _mock_download(file_path, destination):
        destination.write(mock_file_bytes)
    mock_bot.download_file.side_effect = _mock_download

    mock_msg = MagicMock()
    mock_msg.bot = mock_bot
    mock_msg.chat.id = chat_id
    mock_msg.document.file_name = "archive_august_2026.xlsx"
    mock_msg.document.file_id = "doc_file_id_123"

    mock_status_msg = AsyncMock()
    mock_msg.reply = AsyncMock(return_value=mock_status_msg)

    # Execute handler
    await doc_handler(mock_msg)

    # Verify status message was edited with final summary
    assert mock_status_msg.edit_text.called
    reply_call_text = mock_status_msg.edit_text.call_args[0][0]
    assert "Отчёт Wildberries успешно обработан" in reply_call_text
    assert "Продажи (с чеками на вывод)" in reply_call_text
    assert "Возвраты (на ввод в оборот)" in reply_call_text
    assert "добавлен в очередь на подписание ЭЦП" in reply_call_text

    # Verify database state
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select
        res = await session.execute(select(KizSignatureBatch).where(KizSignatureBatch.seller_id == seller_id))
        batches = res.scalars().all()
        assert len(batches) == 1
        b = batches[0]
        assert b.filename == "archive_august_2026.xlsx"
        assert b.source == "telegram"
        assert b.status == BatchStatus.PENDING_SIGNATURE
        assert b.sales_count == 1
        assert b.returns_count == 1
        assert b.total_count == 2

        # Verify seller last_archive_uploaded_at was updated
        s = await session.get(Seller, seller_id)
        assert s.last_archive_uploaded_at is not None
