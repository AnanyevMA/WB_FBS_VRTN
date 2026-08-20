/**
 * WB FBS Manager — Supplies Management (WB Supplies, QR/Barcodes, Synchronization)
 */

async function loadSupplies() {
    if(!currentSellerId) {
        document.getElementById('supplies-table-body').innerHTML = `<tr><td colspan="6" style="text-align:center; padding: 24px; color: var(--text-muted);">Выберите продавца</td></tr>`;
        return;
    }

    try {
        const data = await apiFetch(`/sellers/${currentSellerId}/supplies`);
        const supplies = data.items || [];
        const tbody = document.getElementById('supplies-table-body');

        if (supplies.length === 0) {
            tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding: 24px; color: var(--text-muted);">Поставки пока не созданы</td></tr>`;
            return;
        }

        tbody.innerHTML = supplies.map(s => `
            <tr>
                <td style="font-weight: 600; font-family:monospace;">${s.wb_supply_id}</td>
                <td style="font-weight: 500;">${s.name}</td>
                <td>${getStatusBadge(s.status, 'supply')}</td>
                <td style="font-weight: 600;">${s.orders_count} шт</td>
                <td style="color: var(--text-muted); font-size:13px;">${s.created_at ? new Date(s.created_at).toLocaleString('ru-RU') : '-'}</td>
                <td>
                    <button class="icon-btn" title="Штрихкод поставки" onclick="showSupplyBarcode('${s.id}', '${s.wb_supply_id}')">📄 Штрихкод</button>
                </td>
            </tr>
        `).join('');

    } catch (e) {
        showToast('Ошибка', 'Не удалось загрузить поставки: ' + e.message, 'error');
    }
}

async function syncSuppliesFromWB() {
    if (!currentSellerId) return showToast('Ошибка', 'Сначала выберите продавца', 'error');
    const btn = document.getElementById('syncSuppliesBtn');
    if (btn) btn.classList.add('loading');
    try {
        const res = await apiFetch(`/sellers/${currentSellerId}/supplies/sync`, { method: 'POST' });
        showToast('Успех', res.message || 'Поставки синхронизированы с WB', 'success');
        await loadSupplies();
    } catch (err) {
        showToast('Ошибка', err.message, 'error');
    } finally {
        if (btn) btn.classList.remove('loading');
    }
}

async function openCreateSupplyModal() {
    if(!currentSellerId) return showToast('Ошибка', 'Сначала выберите продавца', 'error');
    
    document.getElementById('supplyName').value = `Поставка от ${new Date().toLocaleDateString('ru-RU')}`;
    
    // Fetch orders in ASSEMBLING state to select
    const selectContainer = document.getElementById('supplyOrdersSelectContainer');
    selectContainer.innerHTML = '<div style="color: var(--text-muted);">Загрузка заказов...</div>';

    try {
        const data = await apiFetch(`/sellers/${currentSellerId}/orders?status=ASSEMBLING`);
        const orders = data.items || [];
        
        if (orders.length === 0) {
            selectContainer.innerHTML = '<div style="color: var(--text-muted); font-size:13px;">Нет заказов в статусе "На сборке". Поставка будет создана пустой.</div>';
        } else {
            selectContainer.innerHTML = orders.map(o => `
                <label style="display:flex; align-items:center; gap:10px; margin-bottom:8px; cursor:pointer;">
                    <input type="checkbox" name="supply_order_choice" value="${o.id}" checked>
                    <span><strong>#${o.id}</strong> — ${o.name || o.article} (${o.price} ₽)</span>
                </label>
            `).join('');
        }
        openModal('supplyModal');
    } catch (e) {
        openModal('supplyModal');
    }
}

async function createSupply() {
    const name = document.getElementById('supplyName').value.trim();
    if(!name) return showToast('Ошибка', 'Введите название поставки', 'error');

    const checkboxes = document.querySelectorAll('input[name="supply_order_choice"]:checked');
    const selectedOrderIds = Array.from(checkboxes).map(cb => parseInt(cb.value));

    const btn = document.getElementById('createSupplySubmitBtn');
    btn.classList.add('loading');

    try {
        const res = await apiFetch(`/sellers/${currentSellerId}/supplies`, {
            method: 'POST',
            body: JSON.stringify({
                name: name,
                order_ids: selectedOrderIds
            })
        });
        showToast('Поставка создана', res.message || 'Поставка сформирована', 'success');
        closeModal('supplyModal');
        await loadSupplies();
    } catch (e) {
        showToast('Ошибка', e.message, 'error');
    } finally {
        btn.classList.remove('loading');
    }
}

async function showSupplyBarcode(supplyId, wbSupplyId) {
    try {
        const data = await apiFetch(`/sellers/${currentSellerId}/supplies/${supplyId}/barcode`);
        document.getElementById('barcodeModalTitle').innerText = `Штрихкод ${wbSupplyId}`;
        document.getElementById('barcodeModalBody').innerHTML = `
            <div style="font-weight: 700; font-size: 18px; margin-bottom: 12px; color: black;">WILDBERRIES SUPPLY</div>
            <div style="background: black; color: white; padding: 20px; border-radius: 8px; font-family: monospace; font-size: 24px; letter-spacing: 4px;">
                *${wbSupplyId}*
            </div>
            <div style="margin-top: 12px; font-size: 14px; color: black;">Идентификатор поставки: ${wbSupplyId}</div>
        `;
        openModal('barcodeModal');
    } catch (e) {
        showToast('Ошибка', e.message, 'error');
    }
}
