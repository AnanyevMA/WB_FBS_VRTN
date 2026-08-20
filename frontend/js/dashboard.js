/**
 * WB FBS Manager — Dashboard & Statistics
 */

async function loadDashboard() {
    if (!currentSellerId) {
        document.getElementById('stat-orders-today').innerText = '0';
        document.getElementById('stat-pending').innerText = '0';
        document.getElementById('stat-withdrawals').innerText = '0';
        document.getElementById('stat-issues').innerText = '0';
        document.getElementById('dashboard-recent-table').innerHTML = `<tr><td colspan="5" style="text-align:center; padding: 24px; color: var(--text-muted);">Продавцы не найдены. Добавьте продавца или нажмите "Demo Data".</td></tr>`;
        return;
    }

    try {
        const stats = await apiFetch(`/sellers/${currentSellerId}/orders/stats`);
        document.getElementById('stat-orders-today').innerText = stats.orders_today || '0';
        document.getElementById('stat-pending').innerText = stats.pending_assembly || '0';
        document.getElementById('stat-withdrawals').innerText = stats.withdrawals_success || '0';
        document.getElementById('stat-issues').innerText = stats.kiz_issues || '0';
        
        document.getElementById('stat-orders-trend').innerText = `Всего заказов: ${stats.total_orders || 0}`;
    } catch (e) {
        console.error("Ошибка загрузки статистики:", e);
    }

    try {
        const data = await apiFetch(`/sellers/${currentSellerId}/orders?page_size=5`);
        const orders = data.items || [];
        const tbody = document.getElementById('dashboard-recent-table');
        
        if (orders.length === 0) {
            tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; padding: 24px; color: var(--text-muted);">Заказов пока нет</td></tr>`;
            return;
        }

        tbody.innerHTML = orders.map(o => {
            const sizeBadge = o.tech_size ? `<span style="background: rgba(99,102,241,0.18); color: #a5b4fc; font-size:11px; font-weight:600; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(99,102,241,0.3);">Размер: ${o.tech_size}${o.wb_size && o.wb_size !== o.tech_size ? ` (RU: ${o.wb_size})` : ''}</span>` : '';
            return `
            <tr>
                <td style="font-weight: 600;">#${o.id}</td>
                <td>
                    <div style="font-weight:500; display:flex; align-items:center; flex-wrap:wrap; gap:6px;">
                        <span>${o.name || o.subject}</span>
                        ${sizeBadge}
                    </div>
                    <div style="font-size:12px; color:var(--text-muted); margin-top:2px;">Арт: ${o.article}</div>
                </td>
                <td>${getStatusBadge(o.status, 'order')}</td>
                <td>${getStatusBadge(o.kiz_status, 'kiz')}</td>
                <td style="color: var(--text-muted); font-size:13px;">${o.created_at ? new Date(o.created_at).toLocaleString('ru-RU') : '-'}</td>
            </tr>
        `}).join('');
    } catch (e) {
        console.error("Ошибка загрузки последних заказов:", e);
    }
}

async function loadSellersForDropdown() {
    const select = document.getElementById('activeSellerSelect');
    let sellers = [];
    
    try {
        const data = await apiFetch('/sellers');
        if (Array.isArray(data)) sellers = data;
        else if (data && Array.isArray(data.items)) sellers = data.items;
    } catch (e) {
        console.error("Ошибка загрузки продавцов:", e);
    }

    currentSellersList = sellers;

    if (sellers.length === 0) {
        select.innerHTML = '<option value="">(Нет активных продавцов)</option>';
        currentSellerId = null;
        showSeedBanner(true);
        return;
    }

    showSeedBanner(false);
    select.innerHTML = sellers.map(s => `<option value="${s.id}">${s.name}</option>`).join('');
    
    if (!currentSellerId || !sellers.some(s => s.id == currentSellerId)) {
        currentSellerId = sellers[0].id;
    }
    select.value = currentSellerId;

    // Change listener
    select.onchange = (e) => {
        currentSellerId = e.target.value;
        showToast('Переключение', `Выбран магазин`, 'info');
        const activeNav = document.querySelector('.nav-item.active');
        const currentRoute = activeNav ? activeNav.dataset.route : '/';
        navigateTo(currentRoute);
    };
}

function showSeedBanner(show) {
    const container = document.getElementById('seedBannerContainer');
    if (!container) return;
    if (show) {
        container.innerHTML = `
            <div class="seed-banner">
                <div>
                    <div style="font-weight: 600; font-size: 15px; margin-bottom: 4px;">База данных пока пуста</div>
                    <div style="font-size: 13px; color: var(--text-muted);">Нажмите "Сгенерировать демо-данные", чтобы мгновенно создать тестового продавца и 5 заказов FBS.</div>
                </div>
                <button class="btn btn-primary btn-sm" onclick="seedMockData()">Сгенерировать демо-данные</button>
            </div>
        `;
    } else {
        container.innerHTML = '';
    }
}

async function seedMockData() {
    try {
        showToast('Загрузка...', 'Создание демо-данных в базе...', 'info');
        const res = await apiFetch('/debug/seed-mock-data', { method: 'POST' });
        showToast('Успех', 'Сгенерирован демо-продавец и 5 заказов', 'success');
        await loadSellersForDropdown();
        navigateTo('/');
    } catch (e) {
        showToast('Ошибка', 'Не удалось создать тестовые данные: ' + e.message, 'error');
    }
}
