/**
 * WB FBS Manager — Orders Management (FBS Orders, Thermal Stickers, Assembling)
 */

async function loadOrders(silent = false) {
    if (!currentSellerId) {
        document.getElementById('orders-table-body').innerHTML = `<tr><td colspan="7" style="text-align:center; padding: 24px; color: var(--text-muted);">Выберите продавца</td></tr>`;
        return;
    }

    const statusFilter = document.getElementById('orderStatusFilter').value;
    const kizFilter = document.getElementById('orderKizStatusFilter').value;
    const searchQuery = document.getElementById('orderSearchInput').value.trim();

    let queryParams = [];
    if (statusFilter !== 'ALL') queryParams.push(`status=${statusFilter}`);
    if (kizFilter !== 'ALL') queryParams.push(`kiz_status=${kizFilter}`);
    if (searchQuery) queryParams.push(`q=${encodeURIComponent(searchQuery)}`);

    const queryString = queryParams.length ? '?' + queryParams.join('&') : '';

    try {
        const data = await apiFetch(`/sellers/${currentSellerId}/orders${queryString}`);
        const orders = data.items || [];
        const tbody = document.getElementById('orders-table-body');

        if (orders.length === 0) {
            tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; padding: 24px; color: var(--text-muted);">Заказы не найдены</td></tr>`;
            return;
        }

        tbody.innerHTML = orders.map(o => {
            const sizeBadge = o.tech_size ? `<span style="background: rgba(99,102,241,0.18); color: #a5b4fc; font-size:11px; font-weight:600; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(99,102,241,0.3);">Размер: ${o.tech_size}${o.wb_size && o.wb_size !== o.tech_size ? ` (RU: ${o.wb_size})` : ''}</span>` : '';
            const czStatusBadge = getCzStatusBadge(o.kiz_cz_status, o.kiz_status, !!o.kiz_code);

            return `
            <tr>
                <td style="font-weight: 600;">#${o.id}</td>
                <td>
                    <div style="font-weight:500; display:flex; align-items:center; flex-wrap:wrap; gap:6px;">
                        <span>${o.name || o.subject}</span>
                        ${sizeBadge}
                    </div>
                    <div style="font-size:12px; color:var(--text-muted); margin-top:2px;">Арт: ${o.article} | ${o.brand}</div>
                </td>
                <td style="font-weight: 600;">${o.price} ₽</td>
                <td>
                    <div style="display:flex; flex-direction:column; gap:4px; align-items:flex-start;">
                        <div>${getStatusBadge(o.status, 'order')}</div>
                        ${getWbStatusBadge(o.wb_status, o.supplier_status)}
                    </div>
                </td>
                <td>
                    <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                        ${getStatusBadge(o.kiz_status, 'kiz')}
                        ${czStatusBadge}
                    </div>
                    ${o.kiz_code ? `<div style="font-size:11px; font-family:monospace; color:var(--text-muted); margin-top:4px; display:flex; align-items:center; gap:4px;"><span>${o.kiz_code.substring(0, 18)}...</span><button class="icon-btn" style="padding:1px 4px; font-size:11px;" title="Проверить статус в Честном Знаке" onclick="checkKizLiveStatus('${o.id}')">🔍</button></div>` : ''}
                </td>
                <td style="color: var(--text-muted); font-size:13px;" title="Дата и время заказа на Wildberries">${(o.wb_created_at || o.created_at) ? new Date(o.wb_created_at || o.created_at).toLocaleString('ru-RU') : '-'}</td>
                <td>
                    <div style="display:flex; gap:6px;">
                        <button class="icon-btn" title="Просмотр и этикетка" onclick="viewOrderDetail('${o.id}')">👁️</button>
                        ${o.status === 'NEW' ? `<button class="icon-btn" title="Перевести на сборку" onclick="markOrderAssembling('${o.id}')">📦</button>` : ''}
                        <button class="icon-btn" title="Привязать КИЗ" onclick="openAttachKizModal('${o.id}', '${o.name || o.article}')">🏷️</button>
                        ${o.kiz_code && o.kiz_status === 'ATTACHED' ? `<button class="icon-btn" style="color:#10b981;" title="Вывести КИЗ из оборота в ЧЗ через ЭЦП" onclick="openKizSigningModal(['${o.id}'], 'WITHDRAWAL')">✍️</button>` : ''}
                        ${o.kiz_code && (o.kiz_status === 'WITHDRAWN' || o.status === 'CANCELLED') ? `<button class="icon-btn" style="color:#f59e0b;" title="Вернуть КИЗ в оборот в ЧЗ через ЭЦП" onclick="openKizSigningModal(['${o.id}'], 'RETURN')">🔄</button>` : ''}
                        ${o.status !== 'CANCELLED' ? `<button class="icon-btn" title="Отменить заказ" style="color:var(--status-cancelled)" onclick="cancelOrder('${o.id}')">❌</button>` : ''}
                    </div>
                </td>
            </tr>
        `}).join('');

        if(!silent) showToast('Заказы загружены', `Загружено ${orders.length} заказов`, 'success');
    } catch (e) {
        showToast('Ошибка', 'Не удалось загрузить заказы: ' + e.message, 'error');
    }
}

async function checkKizLiveStatus(orderId) {
    if(!currentSellerId) return showToast('Ошибка', 'Выберите продавца', 'error');
    try {
        showToast('Честный Знак', 'Запрос статуса КИЗ в ГИС МТ...', 'info');
        const res = await apiFetch(`/sellers/${currentSellerId}/orders/${orderId}/kiz-check`, { method: 'POST' });
        const czStatus = res.kiz_cz_status || 'INTRODUCED';
        const czName = STATUS_MAP_CZ[czStatus] || czStatus;
        const statusName = `${czName} (${czStatus})`;
        if (res.kiz_status === 'ERROR' || (res.product_info && res.product_info.is_valid === false)) {
            const msg = res.product_info?.validation_message || `Статус в ЧЗ: ${statusName}. Обнаружено несоответствие!`;
            showToast('Внимание! Ошибка КИЗ', msg, 'error');
        } else {
            showToast('Статус КИЗ в ГИС МТ', `Статус: ${statusName}`, 'success');
        }
        await loadOrders(true);
    } catch (e) {
        showToast('Ошибка проверки КИЗ', e.message, 'error');
    }
}

async function viewOrderDetail(orderId) {
    try {
        const order = await apiFetch(`/sellers/${currentSellerId}/orders/${orderId}`);
        const sticker = await apiFetch(`/sellers/${currentSellerId}/orders/${orderId}/sticker`);

        document.getElementById('orderDetailTitle').innerText = `Детали сборочного задания #${order.id}`;
        
        document.getElementById('orderDetailBody').innerHTML = `
            <div style="margin-bottom: 16px;">
                <div style="color: var(--text-muted); font-size:12px;">Наименование товара</div>
                <div style="font-weight:600; font-size:15px; margin-top:2px;">${order.name || '-'}</div>
            </div>

            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px;">
                <div>
                    <div style="color: var(--text-muted); font-size:12px;">Артикул</div>
                    <div style="font-weight:600;">${order.article || '-'}</div>
                </div>
                <div>
                    <div style="color: var(--text-muted); font-size:12px;">Размер</div>
                    <div style="font-weight:600; color:#a5b4fc;">${order.tech_size ? `${order.tech_size} ${order.wb_size && order.wb_size !== order.tech_size ? `(RU: ${order.wb_size})` : ''}` : 'Без размера / Не указан'}</div>
                </div>
                <div>
                    <div style="color: var(--text-muted); font-size:12px;">Бренд</div>
                    <div style="font-weight:600;">${order.brand || '-'}</div>
                </div>
                <div>
                    <div style="color: var(--text-muted); font-size:12px;">Предмет</div>
                    <div style="font-weight:600;">${order.subject || '-'}</div>
                </div>
                <div>
                    <div style="color: var(--text-muted); font-size:12px;">Цена</div>
                    <div style="font-weight:600; color:var(--primary-hover);">${order.price} ₽</div>
                </div>
                <div>
                    <div style="color: var(--text-muted); font-size:12px;">Статус заказа</div>
                    <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap; margin-top:3px;">
                        ${getStatusBadge(order.status, 'order')}
                        ${getWbStatusBadge(order.wb_status, order.supplier_status)}
                    </div>
                </div>
                <div>
                    <div style="color: var(--text-muted); font-size:12px;">Дата заказа на WB</div>
                    <div style="font-weight:600;">${order.wb_created_at ? new Date(order.wb_created_at).toLocaleString('ru-RU') : '-'}</div>
                </div>
            </div>

            <div style="margin-bottom: 20px; padding: 14px; background: rgba(15,23,42,0.6); border: 1px solid var(--border-color); border-radius: 8px;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                    <span style="color: var(--text-muted); font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:0.5px;">Маркировка Честный Знак (КИЗ)</span>
                    ${order.kiz_code ? `<button class="btn btn-secondary" style="padding:4px 10px; font-size:12px;" onclick="checkKizLiveStatus('${order.id}'); viewOrderDetail('${order.id}');">🔍 Проверить в ЧЗ</button>` : ''}
                </div>
                <div style="font-family:monospace; font-size:13px; word-break:break-all; background:rgba(0,0,0,0.3); padding:8px; border-radius:6px; margin-bottom:10px;">
                    ${order.kiz_code || 'Код КИЗ не прикреплен'}
                </div>
                <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
                    <div><span style="color:var(--text-muted); font-size:11px;">Локальный статус:</span> ${getStatusBadge(order.kiz_status, 'kiz')}</div>
                    <div><span style="color:var(--text-muted); font-size:11px;">Статус в ГИС МТ:</span> ${getCzStatusBadge(order.kiz_cz_status, order.kiz_status, !!order.kiz_code)}</div>
                    ${order.kiz_cz_status_updated_at ? `<div style="font-size:11px; color:var(--text-muted);">Обновлено: ${new Date(order.kiz_cz_status_updated_at).toLocaleString('ru-RU')}</div>` : ''}
                </div>
            </div>

            <div style="text-align: center; margin-top: 16px;">
                <div style="font-weight: 500; font-size: 13px; color: var(--text-muted); margin-bottom: 8px;">Этикетка Wildberries</div>
                <div class="sticker-preview">
                    ${sticker.svg_content || '<div>Этикетка недоступна</div>'}
                </div>
            </div>
        `;

        document.getElementById('orderDetailFooter').innerHTML = `
            ${order.kiz_code && order.kiz_status === 'ATTACHED' ? `<button class="btn btn-success" style="background:#10b981; border:none; display:flex; align-items:center; gap:6px;" onclick="closeModal('orderDetailModal'); openKizSigningModal(['${order.id}'], 'WITHDRAWAL');"><span>✍️</span> Вывести КИЗ (ЭЦП)</button>` : ''}
            ${order.kiz_code && (order.kiz_status === 'WITHDRAWN' || order.status === 'CANCELLED') ? `<button class="btn btn-warning" style="background:#f59e0b; border:none; display:flex; align-items:center; gap:6px; color:#000;" onclick="closeModal('orderDetailModal'); openKizSigningModal(['${order.id}'], 'RETURN');"><span>🔄</span> Вернуть КИЗ (ЭЦП)</button>` : ''}
            ${order.status === 'NEW' ? `<button class="btn btn-success" onclick="markOrderAssembling('${order.id}'); closeModal('orderDetailModal');">На сборку</button>` : ''}
            <button class="btn btn-primary" onclick="window.print()">Печать этикетки</button>
            <button class="btn btn-secondary" onclick="closeModal('orderDetailModal')">Закрыть</button>
        `;

        openModal('orderDetailModal');
    } catch (e) {
        showToast('Ошибка', 'Не удалось загрузить детали заказа: ' + e.message, 'error');
    }
}

async function markOrderAssembling(orderId) {
    try {
        const res = await apiFetch(`/sellers/${currentSellerId}/orders/${orderId}/mark-assembling`, { method: 'POST' });
        showToast('Успех', res.message, 'success');
        await loadOrders();
        await loadDashboard();
    } catch (e) {
        showToast('Ошибка', e.message, 'error');
    }
}

async function cancelOrder(orderId) {
    if (!confirm(`Вы действительно хотите отменить заказ #${orderId}?`)) return;
    try {
        const res = await apiFetch(`/sellers/${currentSellerId}/orders/${orderId}/cancel`, { method: 'POST' });
        showToast('Заказ отменен', res.message, 'success');
        await loadOrders();
        await loadDashboard();
    } catch (e) {
        showToast('Ошибка', e.message, 'error');
    }
}

function openAttachKizModal(orderId, orderInfo) {
    document.getElementById('modalAttachOrderId').value = orderId;
    document.getElementById('modalAttachOrderInfo').innerText = `Заказ #${orderId} — ${orderInfo}`;
    document.getElementById('modalKizInput').value = '';
    openModal('attachKizModal');
    setTimeout(() => document.getElementById('modalKizInput').focus(), 150);
}

async function submitModalKizAttach() {
    const orderId = document.getElementById('modalAttachOrderId').value;
    const kizCode = document.getElementById('modalKizInput').value.trim();

    if (!kizCode) {
        return showToast('Ошибка', 'Введите код КИЗ', 'error');
    }

    const btn = document.getElementById('submitModalAttachKizBtn');
    btn.classList.add('loading');

    try {
        const res = await apiFetch(`/sellers/${currentSellerId}/orders/${orderId}/kiz`, {
            method: 'POST',
            body: JSON.stringify({ kiz_code: kizCode })
        });
        showToast('Успех', res.message || 'КИЗ прикреплен к заказу', 'success');
        closeModal('attachKizModal');
        await loadOrders();
        await loadDashboard();
    } catch (e) {
        showToast('Ошибка', e.message, 'error');
    } finally {
        btn.classList.remove('loading');
    }
}
