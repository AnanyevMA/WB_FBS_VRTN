/**
 * WB FBS Manager — Audit Logs & Operations History
 */

async function loadAuditLogs() {
    const tbody = document.getElementById('audit-table-body');
    if (!tbody) return;
    tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; padding: 24px; color: var(--text-muted);">Загрузка журнала аудита...</td></tr>`;

    try {
        const endpoint = currentSellerId ? `/sellers/${currentSellerId}/audit` : '/audit';
        const data = await apiFetch(endpoint);
        const logs = data.items || [];

        if (logs.length === 0) {
            tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; padding: 24px; color: var(--text-muted);">Записи в журнале аудита пока отсутствуют</td></tr>`;
            return;
        }

        tbody.innerHTML = logs.map(l => `
            <tr>
                <td style="color: var(--text-muted); font-size:13px; white-space:nowrap;">
                    ${l.created_at ? new Date(l.created_at).toLocaleString('ru-RU') : '-'}
                </td>
                <td><span class="badge bg-new">${l.agent}</span></td>
                <td style="font-weight:600;">${l.action}</td>
                <td>${l.entity_type ? `${l.entity_type} #${l.entity_id || ''}` : '-'}</td>
                <td style="font-size:12px; font-family:monospace; color:var(--text-muted);">
                    ${l.error ? `<span style="color:var(--status-cancelled)">❌ ${l.error}</span>` : JSON.stringify(l.payload || {})}
                </td>
            </tr>
        `).join('');

    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; padding: 24px; color: var(--status-cancelled);">Ошибка загрузки аудита: ${e.message}</td></tr>`;
    }
}
