/**
 * WB FBS Manager — UI Helpers (Toasts, Modals, Status Badges)
 */

function showToast(title, message, type = 'info') {
    const container = document.getElementById('toastContainer');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    
    let icon = 'ℹ️';
    if (type === 'success') icon = '✅';
    if (type === 'error') icon = '❌';
    if (type === 'warning') icon = '⚠️';

    toast.innerHTML = `
        <div class="toast-icon">${icon}</div>
        <div class="toast-content">
            <div class="toast-title">${title}</div>
            <div class="toast-msg">${message}</div>
        </div>
    `;

    container.appendChild(toast);
    toast.offsetHeight;
    toast.classList.add('show');

    setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    }, 4500);
}

function openModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.add('active');
}

function closeModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.remove('active');
}

function getStatusBadge(status, type = 'order') {
    if (!status) return '<span class="badge">-</span>';
    const s = status.toUpperCase();
    if (type === 'order') {
        const ru = STATUS_MAP_ORDER[s] || s;
        return `<span class="badge bg-${s.toLowerCase()}">${ru}</span>`;
    } else if (type === 'kiz') {
        const ru = STATUS_MAP_KIZ[s] || s;
        return `<span class="badge kiz-${s.toLowerCase()}">${ru}</span>`;
    } else {
        const ru = STATUS_MAP_SUPPLY[s] || s;
        return `<span class="badge bg-${s.toLowerCase()}">${ru}</span>`;
    }
}

function getCzStatusBadge(czStatus, kizStatus, hasKizCode) {
    if (kizStatus === 'NOT_REQUIRED') return '';
    if (!czStatus) {
        return hasKizCode ? '<span style="background: rgba(100, 116, 139, 0.15); color: #94a3b8; font-size:11px; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(100, 116, 139, 0.25);" title="Статус в ГИС МТ еще не запрашивался">ЧЗ: не проверен</span>' : '';
    }
    const s = czStatus.toUpperCase();
    if (s === 'INTRODUCED' || s === 'IN_CIRCULATION') {
        return `<span style="background: rgba(16, 185, 129, 0.15); color: #34d399; font-size:11px; font-weight:600; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(16, 185, 129, 0.3);" title="Код находится в обороте в ГИС МТ (Честный Знак)">ЧЗ: В обороте</span>`;
    } else if (s === 'RETIRED' || s === 'OUT_OF_CIRCULATION') {
        return `<span style="background: rgba(148, 163, 184, 0.15); color: #94a3b8; font-size:11px; font-weight:600; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(148, 163, 184, 0.3);" title="Код официально выведен из оборота в ГИС МТ">ЧЗ: Выведен</span>`;
    } else if (s === 'EMITTED' || s === 'EMISSION') {
        return `<span style="background: rgba(59, 130, 246, 0.15); color: #60a5fa; font-size:11px; font-weight:600; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(59, 130, 246, 0.3);" title="Код эмитирован в СУЗ">ЧЗ: Эмитирован</span>`;
    } else if (s === 'APPLIED') {
        return `<span style="background: rgba(168, 85, 247, 0.15); color: #c084fc; font-size:11px; font-weight:600; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(168, 85, 247, 0.3);" title="Код нанесен">ЧЗ: Нанесен</span>`;
    } else if (s === 'DISAGGREGATED' || s === 'WRITTEN_OFF' || s === 'KILLED') {
        return `<span style="background: rgba(239, 68, 68, 0.15); color: #f87171; font-size:11px; font-weight:600; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(239, 68, 68, 0.3);" title="Код списан или расформирован в ГИС МТ">ЧЗ: Списан</span>`;
    }
    return `<span style="background: rgba(59, 130, 246, 0.15); color: #60a5fa; font-size:11px; font-weight:600; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(59, 130, 246, 0.3);">ЧЗ: ${STATUS_MAP_CZ[s] || s}</span>`;
}

function getWbStatusBadge(wbStatus, supplierStatus) {
    if (!wbStatus && !supplierStatus) return '';
    const s = (wbStatus || '').toLowerCase();
    if (s === 'sorted') {
        return `<span style="background: rgba(139, 92, 246, 0.15); color: #a78bfa; font-size:11px; font-weight:600; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(139, 92, 246, 0.3);" title="Товар принят и отсортирован на складе/СЦ Wildberries">📦 СЦ: Отсортирован</span>`;
    } else if (s === 'ready_for_pickup') {
        return `<span style="background: rgba(245, 158, 11, 0.15); color: #fbbf24; font-size:11px; font-weight:600; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(245, 158, 11, 0.3);" title="Товар прибыл в ПВЗ и ожидает получения клиентом">🏪 ПВЗ: Готов к выдаче</span>`;
    } else if (s === 'sold') {
        return `<span style="background: rgba(16, 185, 129, 0.15); color: #34d399; font-size:11px; font-weight:600; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(16, 185, 129, 0.3);" title="Товар выдан покупателю (продажа)">💰 Выкуплен</span>`;
    } else if (s === 'canceled_by_client' || s === 'declined_by_client') {
        return `<span style="background: rgba(239, 68, 68, 0.15); color: #f87171; font-size:11px; font-weight:600; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(239, 68, 68, 0.3);" title="Покупатель отказался от товара">${s === 'canceled_by_client' ? '🔄 Отказ на ПВЗ' : '🚫 Отмена клиентом'}</span>`;
    } else if (s === 'waiting') {
        return `<span style="background: rgba(100, 116, 139, 0.15); color: #cbd5e1; font-size:11px; font-weight:600; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(100, 116, 139, 0.3);" title="Заказ передан в доставку, ожидает приемки на СЦ">🚚 В пути на СЦ</span>`;
    } else if (s === 'defect') {
        return `<span style="background: rgba(239, 68, 68, 0.15); color: #f87171; font-size:11px; font-weight:600; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(239, 68, 68, 0.3);" title="Отменен по причине брака">⚠️ Брак</span>`;
    } else if (wbStatus) {
        return `<span style="background: rgba(59, 130, 246, 0.15); color: #60a5fa; font-size:11px; font-weight:600; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(59, 130, 246, 0.3);">WB: ${STATUS_MAP_WB[s] || s}</span>`;
    }
    return '';
}
