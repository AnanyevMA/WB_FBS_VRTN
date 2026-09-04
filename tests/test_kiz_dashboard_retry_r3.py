import pytest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch
from pathlib import Path
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import AsyncSessionLocal, init_db
from app.models.seller import Seller
from app.models.order import Order, KizStatus, OrderStatus
from app.models.audit import AuditLog
from app.services.encryption import encrypt
from app.services.auth_service import create_access_token, ensure_initial_admin


@pytest.fixture(autouse=True)
def mock_celery_apply():
    with patch('celery.Celery.send_task') as mock_st, \
         patch('app.agents.cz_withdrawal.withdraw_order_kiz.delay') as mock_wdelay, \
         patch('app.agents.cz_withdrawal.withdraw_order_kiz.apply_async') as mock_wasync:
        yield {
            'send_task': mock_st,
            'withdraw_delay': mock_wdelay,
            'withdraw_async': mock_wasync,
        }


@pytest.mark.asyncio
async def test_order_model_cz_columns():
    await init_db()

    seller_id = str(uuid.uuid4())
    order_id = int(str(uuid.uuid4().int)[:9])
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as session:
        seller = Seller(
            id=seller_id,
            name='Model Test Seller',
            wb_api_token_encrypted=encrypt('token'),
            is_active=True,
        )
        session.add(seller)

        order = Order(
            id=order_id,
            seller_id=seller_id,
            name='Платье летнее',
            article='dress-01',
            price=Decimal('1990.50'),
            status=OrderStatus.NEW,
            kiz_required=True,
            kiz_code='0104630199251318215QTSRh>4sVc+.',
            kiz_status=KizStatus.ERROR,
            kiz_cz_status='CHECKED_NOT_OK',
            cz_withdrawal_doc_id='doc-err-123',
            cz_doc_status='CHECKED_NOT_OK',
            cz_rejection_reason='07: Недопустимое количество символов в значении поля Код идентификации',
            wb_created_at=now,
            created_at=now,
        )
        session.add(order)
        await session.commit()

    async with AsyncSessionLocal() as session:
        retrieved = await session.get(Order, order_id)
        assert retrieved is not None
        assert retrieved.cz_withdrawal_doc_id == 'doc-err-123'
        assert retrieved.cz_doc_status == 'CHECKED_NOT_OK'
        assert '07: Недопустимое количество символов' in retrieved.cz_rejection_reason
        assert retrieved.kiz_status == KizStatus.ERROR


@pytest.mark.asyncio
async def test_order_serialization_in_list_and_get_orders():
    await init_db()

    seller_id = str(uuid.uuid4())
    order_id = int(str(uuid.uuid4().int)[:9])
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as session:
        admin_user = await ensure_initial_admin(session)
        auth_token = create_access_token(
            data={'sub': admin_user.id, 'username': admin_user.username, 'role': 'admin', 'is_superuser': True}
        )
        seller = Seller(
            id=seller_id,
            name='Serialization Seller',
            wb_api_token_encrypted=encrypt('token'),
            is_active=True,
        )
        session.add(seller)

        order = Order(
            id=order_id,
            seller_id=seller_id,
            name='Блузка женская',
            article='blouse-02',
            price=Decimal('2500.00'),
            status=OrderStatus.DELIVERED,
            kiz_required=True,
            kiz_code='0104630199251318215QTSRh>4sVc+.',
            kiz_status=KizStatus.ERROR,
            kiz_cz_status='CHECKED_NOT_OK',
            cz_withdrawal_doc_id='doc-serial-777',
            cz_doc_status='CHECKED_NOT_OK',
            cz_rejection_reason='07: Недопустимое количество символов в значении поля Код идентификации',
            wb_created_at=now,
            created_at=now,
        )
        session.add(order)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test', headers={'Authorization': f'Bearer {auth_token}'}) as ac:
        res_list = await ac.get(f'/api/v1/sellers/{seller_id}/orders?view=all')
        assert res_list.status_code == 200, res_list.text
        data_list = res_list.json()
        items = data_list.get('items', [])
        target_item = next((i for i in items if i['id'] == order_id), None)
        assert target_item is not None
        assert target_item['cz_withdrawal_doc_id'] == 'doc-serial-777'
        assert target_item['cz_doc_status'] == 'CHECKED_NOT_OK'
        assert '07: Недопустимое количество символов' in target_item['cz_rejection_reason']

        res_get = await ac.get(f'/api/v1/sellers/{seller_id}/orders/{order_id}')
        assert res_get.status_code == 200, res_get.text
        data_order = res_get.json()
        assert data_order['id'] == order_id
        assert data_order['cz_withdrawal_doc_id'] == 'doc-serial-777'
        assert data_order['cz_doc_status'] == 'CHECKED_NOT_OK'
        assert '07: Недопустимое количество символов' in data_order['cz_rejection_reason']


@pytest.mark.asyncio
async def test_retry_withdrawal_endpoint_cleans_cis_and_resets_status(mock_celery_apply):
    await init_db()

    seller_id = str(uuid.uuid4())
    order_id = int(str(uuid.uuid4().int)[:9])
    now = datetime.now(timezone.utc)

    dirty_kiz = '0104630199251318215QTSRh>4sVc+. 91EE12 92xyzSignatureTails=='

    async with AsyncSessionLocal() as session:
        admin_user = await ensure_initial_admin(session)
        auth_token = create_access_token(
            data={'sub': admin_user.id, 'username': admin_user.username, 'role': 'admin', 'is_superuser': True}
        )
        seller = Seller(
            id=seller_id,
            name='Retry Seller',
            wb_api_token_encrypted=encrypt('token'),
            is_active=True,
        )
        session.add(seller)

        order = Order(
            id=order_id,
            seller_id=seller_id,
            name='Куртка демисезонная',
            article='jacket-03',
            price=Decimal('3499.00'),
            status=OrderStatus.DELIVERED,
            kiz_required=True,
            kiz_code=dirty_kiz,
            kiz_status=KizStatus.ERROR,
            kiz_cz_status='CHECKED_NOT_OK',
            cz_withdrawal_doc_id='doc-rejected-555',
            cz_doc_status='CHECKED_NOT_OK',
            cz_rejection_reason='07: Недопустимое количество символов в значении поля Код идентификации',
            wb_created_at=now,
            created_at=now,
        )
        session.add(order)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test', headers={'Authorization': f'Bearer {auth_token}'}) as ac:
        res = await ac.post(f'/api/v1/sellers/{seller_id}/orders/{order_id}/retry-withdrawal')
        assert res.status_code == 200, res.text
        payload = res.json()
        assert payload['status'] == 'ok'
        assert 'Вывод из оборота повторно поставлен в очередь' in payload['message']

        order_data = payload['order']
        clean_expected = '0104630199251318215QTSRh>4sVc+.'
        assert len(clean_expected) == 31
        assert order_data['kiz_code'] == clean_expected
        assert len(order_data['kiz_code']) == 31
        assert order_data['kiz_status'] == 'ATTACHED'
        assert order_data['cz_rejection_reason'] is None
        assert order_data['cz_doc_status'] is None

    async with AsyncSessionLocal() as session:
        db_order = await session.get(Order, order_id)
        assert db_order.kiz_code == clean_expected
        assert len(db_order.kiz_code) == 31
        assert db_order.kiz_status == KizStatus.ATTACHED
        assert db_order.cz_rejection_reason is None
        assert db_order.cz_doc_status is None

        audit = (await session.execute(
            AuditLog.__table__.select().where(AuditLog.entity_id == str(order_id))
        )).first()
        assert audit is not None
        assert audit.action == 'RETRY_WITHDRAW_CZ'

    mock_wdelay = mock_celery_apply['withdraw_delay']
    assert mock_wdelay.called
    call_kwargs = mock_wdelay.call_args.kwargs
    assert call_kwargs['seller_id'] == seller_id
    assert call_kwargs['order_id'] == order_id
    assert call_kwargs['kiz_code'] == clean_expected
    assert call_kwargs['price_kopecks'] == 349900


@pytest.mark.asyncio
async def test_retry_withdrawal_various_dirty_kiz_formats(mock_celery_apply):
    await init_db()

    seller_id = str(uuid.uuid4())
    order_id1 = int(str(uuid.uuid4().int)[:9])
    order_id2 = int(str(uuid.uuid4().int)[:9])
    now = datetime.now(timezone.utc)

    kiz_gs = '0104630199251318215QTSRh>4sVc+.\x1d91EE12\x1d92xyz'
    kiz_scanner = ']d20104630199251318215QTSRh>4sVc+. 91EE12 92xyz'

    async with AsyncSessionLocal() as session:
        admin_user = await ensure_initial_admin(session)
        auth_token = create_access_token(
            data={'sub': admin_user.id, 'username': admin_user.username, 'role': 'admin', 'is_superuser': True}
        )
        seller = Seller(
            id=seller_id,
            name='Formats Seller',
            wb_api_token_encrypted=encrypt('token'),
            is_active=True,
        )
        session.add(seller)

        session.add(Order(
            id=order_id1,
            seller_id=seller_id,
            name='Товар 1',
            article='art-1',
            price=Decimal('100.00'),
            status=OrderStatus.DELIVERED,
            kiz_required=True,
            kiz_code=kiz_gs,
            kiz_status=KizStatus.ERROR,
            cz_rejection_reason='Error 1',
            wb_created_at=now,
            created_at=now,
        ))
        session.add(Order(
            id=order_id2,
            seller_id=seller_id,
            name='Товар 2',
            article='art-2',
            price=Decimal('200.00'),
            status=OrderStatus.DELIVERED,
            kiz_required=True,
            kiz_code=kiz_scanner,
            kiz_status=KizStatus.ERROR,
            cz_rejection_reason='Error 2',
            wb_created_at=now,
            created_at=now,
        ))
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test', headers={'Authorization': f'Bearer {auth_token}'}) as ac:
        res1 = await ac.post(f'/api/v1/sellers/{seller_id}/orders/{order_id1}/retry-withdrawal')
        assert res1.status_code == 200
        assert res1.json()['order']['kiz_code'] == '0104630199251318215QTSRh>4sVc+.'
        assert len(res1.json()['order']['kiz_code']) == 31

        res2 = await ac.post(f'/api/v1/sellers/{seller_id}/orders/{order_id2}/retry-withdrawal')
        assert res2.status_code == 200
        assert res2.json()['order']['kiz_code'] == '0104630199251318215QTSRh>4sVc+.'
        assert len(res2.json()['order']['kiz_code']) == 31


@pytest.mark.asyncio
async def test_retry_withdrawal_error_handling():
    await init_db()

    seller_id = str(uuid.uuid4())
    other_seller_id = str(uuid.uuid4())
    order_id_no_kiz = int(str(uuid.uuid4().int)[:9])
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as session:
        admin_user = await ensure_initial_admin(session)
        auth_token = create_access_token(
            data={'sub': admin_user.id, 'username': admin_user.username, 'role': 'admin', 'is_superuser': True}
        )
        session.add(Seller(id=seller_id, name='Error Seller', wb_api_token_encrypted=encrypt('tok1'), is_active=True))
        session.add(Seller(id=other_seller_id, name='Other Seller', wb_api_token_encrypted=encrypt('tok2'), is_active=True))
        session.add(Order(
            id=order_id_no_kiz,
            seller_id=seller_id,
            name='Без КИЗ',
            article='art-none',
            status=OrderStatus.NEW,
            kiz_code=None,
            kiz_status=KizStatus.PENDING,
            wb_created_at=now,
            created_at=now,
        ))
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test', headers={'Authorization': f'Bearer {auth_token}'}) as ac:
        res_404 = await ac.post(f'/api/v1/sellers/{seller_id}/orders/999999999/retry-withdrawal')
        assert res_404.status_code == 404

        res_mismatch = await ac.post(f'/api/v1/sellers/{other_seller_id}/orders/{order_id_no_kiz}/retry-withdrawal')
        assert res_mismatch.status_code == 404

        res_no_kiz = await ac.post(f'/api/v1/sellers/{seller_id}/orders/{order_id_no_kiz}/retry-withdrawal')
        assert res_no_kiz.status_code == 400
        assert 'отсутствует код маркировки' in res_no_kiz.json()['detail']


@pytest.mark.asyncio
async def test_retry_withdrawal_with_none_price_and_kiz_product_info_sync(mock_celery_apply):
    """Verify retry handles None price without error and syncs KizProductInfo record."""
    await init_db()

    seller_id = str(uuid.uuid4())
    order_id = int(str(uuid.uuid4().int)[:9])
    now = datetime.now(timezone.utc)
    rand_s = uuid.uuid4().hex[:7]
    dirty_kiz = f"0104630199259999215QTSN{rand_s} 91EE12 92test"
    clean_kiz = f"0104630199259999215QTSN{rand_s}"

    from app.services.kiz_service import KizProductInfo

    async with AsyncSessionLocal() as session:
        admin_user = await ensure_initial_admin(session)
        auth_token = create_access_token(
            data={"sub": admin_user.id, "username": admin_user.username, "role": "admin", "is_superuser": True}
        )
        session.add(Seller(id=seller_id, name="Sync Seller", wb_api_token_encrypted=encrypt("tok"), is_active=True))
        session.add(Order(
            id=order_id,
            seller_id=seller_id,
            name="Товар без цены",
            article="art-noprice",
            price=None,
            status=OrderStatus.DELIVERED,
            kiz_required=True,
            kiz_code=dirty_kiz,
            kiz_status=KizStatus.ERROR,
            cz_rejection_reason="Ошибка ГИС МТ",
            wb_created_at=now,
            created_at=now,
        ))
        session.add(KizProductInfo(
            kiz_code=dirty_kiz,
            clean_cis=dirty_kiz,
            seller_id=seller_id,
            order_id=order_id,
            gtin="04630199259999",
            serial_number="5QTSNone4sVc+",
            cz_status="REJECTED",
        ))
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {auth_token}"}) as ac:
        res = await ac.post(f"/api/v1/sellers/{seller_id}/orders/{order_id}/retry-withdrawal")
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["status"] == "ok"
        assert data["order"]["kiz_code"] == clean_kiz
        assert data["order"]["price"] == "0.00"

    async with AsyncSessionLocal() as session:
        kpi = (await session.execute(
            KizProductInfo.__table__.select().where(KizProductInfo.order_id == order_id)
        )).first()
        assert kpi is not None
        assert kpi.kiz_code == clean_kiz
        assert kpi.clean_cis == clean_kiz

    mock_wdelay = mock_celery_apply['withdraw_delay']
    assert mock_wdelay.called
    call_kwargs = mock_wdelay.call_args.kwargs
    assert call_kwargs["price_kopecks"] == 0



def test_frontend_dashboard_retry_ui_contract():
    orders_js_path = Path('frontend/js/orders.js')
    style_css_path = Path('frontend/css/style.css')
    index_html_path = Path('frontend/index.html')

    assert orders_js_path.exists()
    assert style_css_path.exists()
    assert index_html_path.exists()

    orders_js = orders_js_path.read_text(encoding='utf-8')
    style_css = style_css_path.read_text(encoding='utf-8')
    index_html = index_html_path.read_text(encoding='utf-8')

    assert 'async function retryCzWithdrawal(' in orders_js
    assert 'retry-withdrawal' in orders_js

    assert 'ЧЗ: Отклонен' in orders_js
    assert 'Причина отклонения:' in orders_js
    assert 'kiz-rejection-box' in orders_js

    assert 'retryCzWithdrawal' in orders_js
    assert 'Повторить вывод в ЧЗ' in orders_js

    assert 'kiz-rejection-banner' in orders_js
    assert 'orderDetailFooter' in orders_js

    assert 'window.retryCzWithdrawal = retryCzWithdrawal;' in orders_js

    assert '.kiz-rejection-box' in style_css
    assert '.kiz-rejection-banner' in style_css
