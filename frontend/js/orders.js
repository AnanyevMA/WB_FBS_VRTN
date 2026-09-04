/**
 * WB FBS Manager — Orders Management (FBS Orders, Thermal Stickers, Assembling)
 */

let currentOrderView = 'active'; // 'active' | 'archive' | 'all'
let currentSortBy = 'wb_created_at';
let currentSortDir = 'desc';

function setOrderView(view) {
    currentOrderView = view;
    document.querySelectorAll('.order-view-tabs .tab-btn').forEach(btn => btn.classList.remove('active'));
    if (view === 'active') {
        const el = document.getElementById('tabBtnActive');
        if (el) el.classList.add('active');
    } else if (view === 'archive') {
        const el = document.getElementById('tabBtnArchive');
        if (el) el.classList.add('active');
    } else if (view === 'all') {
        const el = document.getElementById('tabBtnAll');
        if (el) el.classList.add('active');
    }
    loadOrders();
}

function handleOrderSort(column) {
    if (currentSortBy === column) {
        currentSortDir = currentSortDir === 'desc' ? 'asc' : 'desc';
    } else {
        currentSortBy = column;
        // Default sort direction for dates, ids and price is desc, for text is asc
        currentSortDir = (column === 'wb_created_at' || column === 'id' || column === 'price') ? 'desc' : 'asc';
    }
    updateSortHeadersUI();
    loadOrders();
}

function updateSortHeadersUI() {
    const sortColumns = ['id', 'name', 'price', 'status', 'kiz_status', 'wb_created_at'];
    sortColumns.forEach(col => {
        const th = document.querySelector(`th[data-sort="${col}"]`);
        const icon = document.getElementById(`sort-icon-${col}`);
        if (!th) return;

        if (col === currentSortBy) {
            th.classList.add('sort-active');
            th.classList.remove('sort-asc', 'sort-desc');
            th.classList.add(currentSortDir === 'asc' ? 'sort-asc' : 'sort-desc');
            if (icon) icon.innerText = currentSortDir === 'asc' ? '▲' : '▼';
        } else {
            th.classList.remove('sort-active', 'sort-asc', 'sort-desc');
            if (icon) icon.innerText = '↕';
        }
    });
}

async function loadOrders(silent = false) {
    if (!currentSellerId) {
        document.getElementById('orders-table-body').innerHTML = `<tr><td colspan="7" style="text-align:center; padding: 24px; color: var(--text-muted);">Выберите продавца</td></tr>`;
        return;
    }

    const statusFilter = document.getElementById('orderStatusFilter')?.value || 'ALL';
    const kizFilter = document.getElementById('orderKizStatusFilter')?.value || 'ALL';
    const searchQuery = document.getElementById('orderSearchInput')?.value.trim() || '';

    let queryParams = [
        `view=${currentOrderView}`,
        `sort_by=${currentSortBy}`,
        `sort_dir=${currentSortDir}`
    ];
    if (statusFilter !== 'ALL') queryParams.push(`status=${statusFilter}`);
    if (kizFilter !== 'ALL') queryParams.push(`kiz_status=${kizFilter}`);
    if (searchQuery) queryParams.push(`q=${encodeURIComponent(searchQuery)}`);

    const queryString = '?' + queryParams.join('&');

    try {
        const data = await apiFetch(`/sellers/${currentSellerId}/orders${queryString}`);
        const orders = data.items || [];
        const tbody = document.getElementById('orders-table-body');

        // Update tab badge counters
        if (data.active_count !== undefined) {
            const el = document.getElementById('tabCountActive');
            if (el) el.innerText = data.active_count;
        }
        if (data.archived_count !== undefined) {
            const el = document.getElementById('tabCountArchive');
            if (el) el.innerText = data.archived_count;
        }
        if (data.total_orders_count !== undefined) {
            const el = document.getElementById('tabCountAll');
            if (el) el.innerText = data.total_orders_count;
        }

        if (orders.length === 0) {
            let emptyMsg = 'Заказы не найдены';
            if (currentOrderView === 'active') {
                emptyMsg = 'Активных заказов нет (все заказы завершены и переведены в архив)';
            } else if (currentOrderView === 'archive') {
                emptyMsg = 'В архиве пока нет завершенных заказов';
            }
            tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; padding: 24px; color: var(--text-muted);">${emptyMsg}</td></tr>`;
            return;
        }

        tbody.innerHTML = orders.map(o => {
            const sizeBadge = o.tech_size ? `<span style="background: rgba(99,102,241,0.18); color: #a5b4fc; font-size:11px; font-weight:600; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(99,102,241,0.3);">Размер: ${o.tech_size}${o.wb_size && o.wb_size !== o.tech_size ? ` (RU: ${o.wb_size})` : ''}</span>` : '';
            const isWithdrawnOrRetired = o.kiz_status === 'WITHDRAWN' || o.kiz_cz_status === 'RETIRED' || o.kiz_cz_status === 'WITHDRAWN';
            const isCzRejected = !isWithdrawnOrRetired && (o.kiz_status === 'ERROR' || o.cz_doc_status === 'CHECKED_NOT_OK' || !!o.cz_rejection_reason);
            const czStatusBadge = isCzRejected
                ? `<span class="badge kiz-error" title="Документ отклонен ГИС МТ">ЧЗ: Отклонен</span>`
                : getCzStatusBadge(o.kiz_cz_status, o.kiz_status, !!o.kiz_code);
            const archiveBadge = o.is_archived ? `<span class="badge bg-archived" style="font-size: 10px; padding: 1px 6px;" title="${o.archive_reason === 'sold_and_withdrawn' ? 'Заказ выкуплен, КИЗ выведен из оборота' : 'Отказ от товара, КИЗ возвращен/введен в оборот'}">📁 В архиве</span>` : '';
            const rejectionReasonHtml = isCzRejected && o.cz_rejection_reason
                ? `<div class="kiz-rejection-box" title="${o.cz_rejection_reason}">⚠️ Причина отклонения: ${o.cz_rejection_reason}</div>`
                : '';

            return `
            <tr style="${o.is_archived ? 'opacity: 0.82;' : ''}">
                <td style="font-weight: 600;">
                    <div style="display:flex; align-items:center; gap:6px;">
                        <span>#${o.id}</span>
                        ${archiveBadge}
                    </div>
                </td>
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
                    ${rejectionReasonHtml}
                </td>
                <td style="color: var(--text-muted); font-size:13px;" title="Дата и время заказа на Wildberries">${(o.wb_created_at || o.created_at) ? new Date(o.wb_created_at || o.created_at).toLocaleString('ru-RU') : '-'}</td>
                <td>
                    <div style="display:flex; gap:6px;">
                        <button class="icon-btn" title="Просмотр и этикетка" onclick="viewOrderDetail('${o.id}')">👁️</button>
                        ${o.status === 'NEW' ? `<button class="icon-btn" title="Перевести на сборку" onclick="markOrderAssembling('${o.id}')">📦</button>` : ''}
                        <button class="icon-btn" title="Привязать КИЗ" onclick="openAttachKizModal('${o.id}', '${o.name || o.article}')">🏷️</button>
                        ${o.kiz_code && isCzRejected ? `<button class="icon-btn" title="Повторить вывод в ЧЗ" style="color:var(--primary);" onclick="retryCzWithdrawal('${o.id}')">🔄</button>` : ''}
                        ${o.kiz_code && !isWithdrawnOrRetired && !isCzRejected ? `<button class="icon-btn" style="color:#10b981;" title="Вывести КИЗ из оборота в ЧЗ через ЭЦП" onclick="openKizSigningModal(['${o.id}'], 'WITHDRAWAL')">✍️</button>` : ''}
                        ${o.kiz_code && (isWithdrawnOrRetired || o.status === 'CANCELLED') ? `<button class="icon-btn" style="color:#f59e0b;" title="Вернуть КИЗ в оборот в ЧЗ через ЭЦП" onclick="openKizSigningModal(['${o.id}'], 'RETURN')">🔄</button>` : ''}
                        ${o.status !== 'CANCELLED' ? `<button class="icon-btn" title="Отменить заказ" style="color:var(--status-cancelled)" onclick="cancelOrder('${o.id}')">❌</button>` : ''}
                    </div>
                </td>
            </tr>
        `}).join('');

        if(!silent) showToast('Заказы загружены', `Загружено ${orders.length} заказов (${currentOrderView === 'active' ? 'Активные' : currentOrderView === 'archive' ? 'Архив' : 'Все'})`, 'success');
    } catch (e) {
        showToast('Ошибка', 'Не удалось загрузить заказы: ' + e.message, 'error');
    }
}

async function checkKizLiveStatus(orderId) {
    if(!currentSellerId) return showToast('Ошибка', 'Выберите продавца', 'error');
    try {
        showToast('Честный Знак', 'Запрос статуса КИЗ в ГИС МТ...', 'info');
        const res = await apiFetch(`/sellers/${currentSellerId}/orders/${orderId}/kiz-check`, { method: 'POST' });
        
        const czStatus = res.kiz_cz_status;
        if (!czStatus) {
            const err = res.cz_error || 'Не удалось получить статус в ГИС МТ (проверьте токен/УКЭП в настройках продавца)';
            showToast('Статус ЧЗ не получен', err, 'warning');
        } else {
            const czName = STATUS_MAP_CZ[czStatus] || czStatus;
            const statusName = `${czName} (${czStatus})`;
            if (res.kiz_status === 'ERROR' || (res.product_info && res.product_info.is_valid === false)) {
                const msg = res.product_info?.validation_message || `Статус в ЧЗ: ${statusName}. Обнаружено несоответствие!`;
                showToast('Внимание! Ошибка КИЗ', msg, 'error');
            } else {
                showToast('Статус КИЗ в ГИС МТ', `Статус: ${statusName}`, 'success');
            }
        }
        await loadOrders(true);
        const modal = document.getElementById('orderDetailModal');
        if (modal && modal.style.display !== 'none') {
            await viewOrderDetail(orderId);
        }
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
            ${order.is_archived ? `
            <div style="margin-bottom: 16px; padding: 10px 14px; background: rgba(148, 163, 184, 0.1); border: 1px solid rgba(148, 163, 184, 0.25); border-radius: 8px; display: flex; align-items: center; gap: 8px;">
                <span style="font-size: 16px;">📁</span>
                <div>
                    <span style="font-weight: 600; font-size: 13px; color: #cbd5e1;">Заказ завершен и переведен в архив</span>
                    <div style="font-size: 12px; color: var(--text-muted); margin-top: 1px;">
                        ${order.archive_reason === 'sold_and_withdrawn' ? 'Заказ выкуплен покупателем, а код маркировки выведен из оборота в Честном Знаке.' : 'Покупатель отказался от товара, код маркировки возвращен / введен в оборот.'}
                    </div>
                </div>
            </div>
            ` : ''}

            <div style="margin-bottom: 16px;">
                <div style="color: var(--text-muted); font-size:12px;">Наименование товара</div>
                <div style="font-weight:600; font-size:15px; margin-top:2px;">${order.name || '-'}</div>
            </div>

            <div style="display:grid; grid-template-columns: 1fr 1fr; gap:12px; margin-bottom:16px;">
                <div>
                    <span style="color:var(--text-muted); font-size:12px;">Статус заказа:</span>
                    <div style="margin-top:4px;">${getStatusBadge(order.status, 'order')}</div>
                </div>
                <div>
                    <span style="color:var(--text-muted); font-size:12px;">Статус Wildberries:</span>
                    <div style="margin-top:4px;">${getWbStatusBadge(order.wb_status, order.supplier_status)}</div>
                </div>
                <div>
                    <span style="color:var(--text-muted); font-size:12px;">Товар:</span>
                    <div style="font-weight:500;">${order.name || order.subject || '-'}</div>
                    <div style="font-size:11px; color:var(--text-muted);">Арт: ${order.article || '-'} | Размер: ${order.tech_size || order.wb_size || '-'}</div>
                </div>
                <div>
                    <span style="color:var(--text-muted); font-size:12px;">Дата создания:</span>
                    <div style="font-weight:600;">${order.wb_created_at ? new Date(order.wb_created_at).toLocaleString('ru-RU') : '-'}</div>
                </div>
            </div>

            ${isCzRejected ? `
            <div class="kiz-rejection-banner">
                <span style="font-size: 20px;">⚠️</span>
                <div style="flex:1;">
                    <div style="font-weight: 600; font-size: 13px; color: #f87171;">Документ вывода из оборота отклонен ГИС МТ (Честный Знак)</div>
                    ${order.cz_doc_status ? `<div style="font-size:12px; color:var(--text-muted); margin-top:2px;">Статус документа: <span style="color:#cbd5e1; font-weight:600;">${order.cz_doc_status}</span></div>` : ''}
                    ${order.cz_withdrawal_doc_id ? `<div style="font-size:12px; color:var(--text-muted); margin-top:2px;">ID документа в ГИС МТ: <span style="font-family:monospace; color:#93c5fd;">${order.cz_withdrawal_doc_id}</span></div>` : ''}
                    <div style="font-size: 12px; color: #fca5a5; margin-top: 4px; line-height: 1.4;">
                        <strong>Причина отклонения:</strong> ${order.cz_rejection_reason || 'Документ не прошел валидацию в True API ГИС МТ'}
                    </div>
                </div>
            </div>
            ` : ''}

            <div style="margin-bottom: 20px; padding: 14px; background: rgba(15,23,42,0.6); border: 1px solid var(--border-color); border-radius: 8px;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                    <span style="color: var(--text-muted); font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:0.5px;">Маркировка Честный Знак (КИЗ)</span>
                    ${order.kiz_code ? `<button class="btn btn-secondary" style="padding:4px 10px; font-size:12px;" onclick="checkKizLiveStatus('${order.id}')">🔍 Проверить в ЧЗ</button>` : ''}
                </div>
                <div style="font-family:monospace; font-size:13px; word-break:break-all; background:rgba(0,0,0,0.3); padding:8px; border-radius:6px; margin-bottom:10px;">
                    ${order.kiz_code || 'Код КИЗ не прикреплен'}
                </div>
                <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
                    <div><span style="color:var(--text-muted); font-size:11px;">Локальный статус:</span> ${getStatusBadge(order.kiz_status, 'kiz')}</div>
                    <div><span style="color:var(--text-muted); font-size:11px;">Статус в ГИС МТ:</span> ${isCzRejected ? '<span class="badge kiz-error" title="Документ отклонен ГИС МТ">ЧЗ: Отклонен</span>' : getCzStatusBadge(order.kiz_cz_status, order.kiz_status, !!order.kiz_code)}</div>
                    ${order.cz_doc_status ? `<div><span style="color:var(--text-muted); font-size:11px;">Статус документа:</span> <span style="font-weight:600; font-size:11px; color:#cbd5e1;">${order.cz_doc_status}</span></div>` : ''}
                    ${order.cz_withdrawal_doc_id ? `<div><span style="color:var(--text-muted); font-size:11px;">ID документа:</span> <span style="font-family:monospace; font-size:11px; color:#93c5fd;">${order.cz_withdrawal_doc_id}</span></div>` : ''}
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
            ${order.kiz_code && isCzRejected ? `<button class="btn btn-warning" style="background:#0284c7; border:none; display:flex; align-items:center; gap:6px; color:#fff;" onclick="closeModal('orderDetailModal'); retryCzWithdrawal('${order.id}');"><span>🔄</span> Повторить вывод в ЧЗ</button>` : ''}
            ${order.kiz_code && !isWithdrawnOrRetired && !isCzRejected ? `<button class="btn btn-success" style="background:#10b981; border:none; display:flex; align-items:center; gap:6px;" onclick="closeModal('orderDetailModal'); openKizSigningModal(['${order.id}'], 'WITHDRAWAL');"><span>✍️</span> Вывести КИЗ (ЭЦП)</button>` : ''}
            ${order.kiz_code && (isWithdrawnOrRetired || order.status === 'CANCELLED') ? `<button class="btn btn-warning" style="background:#f59e0b; border:none; display:flex; align-items:center; gap:6px; color:#000;" onclick="closeModal('orderDetailModal'); openKizSigningModal(['${order.id}'], 'RETURN');"><span>🔄</span> Вернуть КИЗ (ЭЦП)</button>` : ''}
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

async function syncAllOrdersCzStatus() {
    if (!currentSellerId) return showToast('Ошибка', 'Сначала выберите продавца', 'error');
    const btn = document.getElementById('syncCzOrdersBtn');
    if (btn) btn.classList.add('loading');
    showToast('Честный Знак', 'Запрос актуальных статусов КИЗ в ГИС МТ...', 'info');
    try {
        const res = await apiFetch(`/sellers/${currentSellerId}/orders/sync-cz`, { method: 'POST' });
        showToast('Честный Знак', res.message || 'Статусы КИЗ успешно обновлены через Честный Знак', 'success');
        await loadOrders(true);
        await loadDashboard();
    } catch (err) {
        showToast('Ошибка Честного Знака', err.message || String(err), 'error');
    } finally {
        if (btn) btn.classList.remove('loading');
    }
}

async function syncOrdersFromWB() {
    if (!currentSellerId) return showToast('Ошибка', 'Сначала выберите продавца', 'error');
    const btn = document.getElementById('syncOrdersBtn');
    if (btn) btn.classList.add('loading');
    try {
        const res = await apiFetch(`/sellers/${currentSellerId}/orders/sync`, { method: 'POST' });
        showToast('Успех', res.message || 'Синхронизация заказов с WB запущена', 'success');
        await loadOrders();
        await loadDashboard();
    } catch (err) {
        showToast('Ошибка', err.message || String(err), 'error');
    } finally {
        if (btn) btn.classList.remove('loading');
    }
}

async function retryCzWithdrawal(orderId) {
    if (!currentSellerId) return showToast('Ошибка', 'Сначала выберите продавца', 'error');
    try {
        showToast('Честный Знак', 'Повторная отправка на вывод из оборота (с нормализацией КИЗ)...', 'info');
        const res = await apiFetch(`/sellers/${currentSellerId}/orders/${orderId}/retry-withdrawal`, {
            method: 'POST',
        });
        if (res.status === 'ok' || res.success) {
            showToast('Успешно', res.message || 'Вывод из оборота повторно поставлен в очередь', 'success');
            await loadOrders(true);
            await loadDashboard();
        } else {
            showToast('Ошибка', res.message || 'Не удалось повторить вывод в Честный Знак', 'error');
        }
    } catch (e) {
        showToast('Ошибка', 'Не удалось повторить вывод в ЧЗ: ' + e.message, 'error');
    }
}

window.retryCzWithdrawal = retryCzWithdrawal;
window.syncAllOrdersCzStatus = syncAllOrdersCzStatus;
window.syncOrdersFromWB = syncOrdersFromWB;

