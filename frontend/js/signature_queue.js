/**
 * WB FBS Manager — WB Archive Processing & Electronic Signature Queue (Очередь ЭЦП)
 */

let currentArchiveData = null;
let currentSignatureBatches = [];
let activeBatchDetails = null;

function openArchiveFileInput(event) {
    if (event) {
        event.stopPropagation();
    }
    const input = document.getElementById('archiveFileInput');
    if (input) {
        input.value = '';
        input.click();
    }
}

function handleArchiveFileSelect(event) {
    const file = event.target.files?.[0];
    if (file) {
        uploadAndPreviewArchive(file);
    }
}

function handleArchiveDrop(event) {
    event.preventDefault();
    event.stopPropagation();
    const dropZone = document.getElementById('archiveDropZone');
    if (dropZone) {
        dropZone.style.borderColor = 'rgba(124,58,237,0.4)';
        dropZone.style.background = 'rgba(124,58,237,0.03)';
    }
    const file = event.dataTransfer?.files?.[0];
    if (file) {
        uploadAndPreviewArchive(file);
    }
}

async function uploadAndPreviewArchive(file) {
    if (!currentSellerId && currentSellersList && currentSellersList.length > 0) {
        currentSellerId = currentSellersList[0].id;
    }
    if (!currentSellerId) {
        return showToast('Ошибка', 'Сначала выберите активный магазин в верхнем меню', 'error');
    }
    if (!file.name.toLowerCase().endsWith('.xlsx') && !file.name.toLowerCase().endsWith('.xls')) {
        return showToast('Ошибка', 'Пожалуйста, загрузите файл формата Excel (.xlsx или .xls)', 'error');
    }

    const formData = new FormData();
    formData.append('file', file);

    showToast('Обработка файла', `Чтение архива ${file.name}...`, 'info');

    try {
        const token = authToken || localStorage.getItem('wbfbs_auth_token') || localStorage.getItem('token');
        const res = await fetch(`${API_BASE}/sellers/${currentSellerId}/archive/preview`, {
            method: 'POST',
            headers: {
                ...(token ? { 'Authorization': `Bearer ${token}` } : {}),
                'X-Seller-ID': currentSellerId,
            },
            body: formData
        });

        if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            throw new Error(errData.detail || `Ошибка сервера (${res.status})`);
        }

        const data = await res.json();
        currentArchiveData = data;
        renderArchivePreview(data);
        openModal('archivePreviewModal');
    } catch (e) {
        showToast('Ошибка разбора архива', e.message, 'error');
    }
}

function renderArchivePreview(data) {
    const summary = data.summary || {};
    document.getElementById('archiveModalSubtitle').textContent = `Файл: ${data.filename || 'archive.xlsx'}`;

    const currentSeller = (currentSellersList || []).find(s => String(s.id) === String(currentSellerId));
    const archiveFiasEl = document.getElementById('archiveModalFiasInfo');
    if (archiveFiasEl) {
        if (currentSeller && currentSeller.mod_fias) {
            archiveFiasEl.innerHTML = `📍 Место деятельности (ФИАС ID): <code style="color:#67e8f9; font-weight:600;">${currentSeller.mod_fias}</code>`;
        } else {
            archiveFiasEl.innerHTML = `⚠️ <span style="color:#facc15;">ФИАС ID склада не указан в настройках продавца (требуется ГИС МТ при выводе из оборота).</span>`;
        }
    }

    document.getElementById('archiveSummarySales').textContent = summary.sales_count || 0;
    document.getElementById('archiveSummaryReturns').textContent = summary.returns_count || 0;
    document.getElementById('archiveSummaryTotal').textContent = summary.total_rows || 0;

    document.getElementById('archiveTabSalesCount').textContent = summary.sales_count || 0;
    document.getElementById('archiveTabReturnsCount').textContent = summary.returns_count || 0;

    // Render withdrawals (sales)
    const withdrawalsBody = document.getElementById('archiveWithdrawalsTableBody');
    if (data.withdrawals && data.withdrawals.length > 0) {
        withdrawalsBody.innerHTML = data.withdrawals.map((w, idx) => `
            <tr>
                <td><input type="checkbox" class="archive-item-withdrawal" data-idx="${idx}" ${w.selected ? 'checked' : ''}></td>
                <td style="font-weight: 600;">#${w.order_id || '-'}</td>
                <td style="font-family: monospace; font-size: 13px;">${w.kiz_code || '<span style="color:var(--text-muted)">нет КИЗ</span>'}</td>
                <td><span class="badge" style="background: rgba(34, 197, 94, 0.15); color: #4ade80; font-weight: 700;">🧾 ${w.receipt_number || '-'}</span></td>
                <td style="color: var(--text-muted); font-size: 13px;">${w.receipt_date || '-'}</td>
                <td style="font-weight: 600;">${w.price ? w.price.toLocaleString('ru-RU') + ' ₽' : '-'}</td>
                <td><span class="badge ${w.db_status === 'DELIVERED' ? 'badge-delivered' : (w.db_status === 'CANCELLED' ? 'badge-cancelled' : 'badge-neutral')}">${w.db_status}</span></td>
                <td>
                    <div style="display:flex; flex-direction:column; gap:2px;">
                        <span class="badge ${w.needs_withdrawal ? 'badge-warning' : 'badge-delivered'}">${w.needs_withdrawal ? '⚠️ Требует выбытия' : '✅ Выведен'}</span>
                        ${w.cz_status_desc ? `<span style="font-size:10px; color:var(--text-muted);">${w.cz_status_desc}</span>` : ''}
                    </div>
                </td>
            </tr>
        `).join('');
    } else {
        withdrawalsBody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: var(--text-muted); padding: 20px;">Нет данных о продажах</td></tr>';
    }

    // Render returns
    const returnsBody = document.getElementById('archiveReturnsTableBody');
    if (data.returns && data.returns.length > 0) {
        returnsBody.innerHTML = data.returns.map((r, idx) => `
            <tr>
                <td><input type="checkbox" class="archive-item-return" data-idx="${idx}" ${r.selected ? 'checked' : ''}></td>
                <td style="font-weight: 600;">#${r.order_id || '-'}</td>
                <td style="font-family: monospace; font-size: 13px;">${r.kiz_code || '<span style="color:var(--text-muted)">нет КИЗ</span>'}</td>
                <td>
                    <div style="font-weight: 500;">${r.name || 'Товар'}</div>
                    <div style="font-size: 11px; color: var(--text-muted);">${r.article || ''}</div>
                    ${r.receipt_number ? `<span class="badge" style="background: rgba(34, 197, 94, 0.15); color: #4ade80; font-size: 11px; font-weight: 600; margin-top: 2px;">🧾 Чек: ${r.receipt_number}</span>` : ''}
                </td>
                <td><span class="badge ${r.needs_cz_return ? 'badge-warning' : 'badge-delivered'}">${r.action_recommended}</span></td>
                <td><span class="badge ${r.db_status === 'CANCELLED' ? 'badge-cancelled' : 'badge-neutral'}">${r.db_status}</span></td>
                <td>
                    <div style="display:flex; flex-direction:column; gap:2px;">
                        ${r.db_cz_status ? `<span class="badge badge-info">${r.db_cz_status}</span>` : '<span style="color:var(--text-muted)">-</span>'}
                        ${r.cz_status_desc ? `<span style="font-size:10px; color:var(--text-muted);">${r.cz_status_desc}</span>` : ''}
                    </div>
                </td>
            </tr>
        `).join('');
    } else {
        returnsBody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 20px;">Нет данных о возвратах</td></tr>';
    }

    switchArchiveTab('sales');
}

async function syncArchiveCzLive() {
    const activeSellerId = currentSellerId 
        || (typeof selectedSellerId !== 'undefined' ? selectedSellerId : null) 
        || (document.getElementById('seller-select') ? document.getElementById('seller-select').value : null) 
        || localStorage.getItem('currentSellerId') 
        || localStorage.getItem('wbfbs_current_seller_id');

    if (!activeSellerId) {
        return showToast('Ошибка', 'Сначала выберите активный магазин в верхнем меню', 'error');
    }

    if (!currentArchiveData) {
        return showToast('Внимание', 'Сначала загрузите файл архива WB (.xlsx)', 'warning');
    }

    const allCodes = [];
    (currentArchiveData.withdrawals || []).forEach(w => { if (w.kiz_code) allCodes.push(w.kiz_code); });
    (currentArchiveData.returns || []).forEach(r => { if (r.kiz_code) allCodes.push(r.kiz_code); });

    if (allCodes.length === 0) {
        return showToast('Информация', 'В архиве нет кодов маркировки для сверки', 'info');
    }

    const btn = document.getElementById('btnSyncArchiveCz');
    const origHtml = btn ? btn.innerHTML : '';
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span>⏳</span> Сверка с ГИС МТ...';
    }

    showToast('Честный Знак', `Сверка ${allCodes.length} кодов маркировки с True API...`, 'info');

    try {
        const res = await apiFetch(`/sellers/${activeSellerId}/archive/sync-cz`, {
            method: 'POST',
            body: JSON.stringify({ kiz_codes: allCodes })
        });

        const syncedItems = res.items || {};
        let updatedCount = 0;

        // Update withdrawals
        if (currentArchiveData.withdrawals) {
            currentArchiveData.withdrawals.forEach(w => {
                const info = syncedItems[w.kiz_code];
                if (info) {
                    w.cz_status = info.cz_status;
                    w.cz_status_desc = info.cz_status_desc;
                    w.is_already_withdrawn = info.is_withdrawn;
                    w.needs_withdrawal = info.needs_withdrawal;
                    w.selected = info.needs_withdrawal && Boolean(w.kiz_code);
                    updatedCount++;
                }
            });
        }

        // Update returns
        if (currentArchiveData.returns) {
            currentArchiveData.returns.forEach(r => {
                const info = syncedItems[r.kiz_code];
                if (info) {
                    r.db_cz_status = info.cz_status;
                    r.cz_status_desc = info.cz_status_desc;
                    r.needs_cz_return = info.is_withdrawn;
                    r.action_recommended = info.is_withdrawn ? "⚠️ Требует возврата в оборот" : "✅ Уже в обороте (готов к привязке)";
                    r.selected = info.is_withdrawn && Boolean(r.kiz_code);
                    updatedCount++;
                }
            });
        }

        // Recalculate summary
        if (currentArchiveData.summary) {
            if (currentArchiveData.withdrawals) {
                currentArchiveData.summary.sales_needing_withdrawal = currentArchiveData.withdrawals.filter(w => w.needs_withdrawal).length;
            }
            if (currentArchiveData.returns) {
                currentArchiveData.summary.returns_needing_cz_return = currentArchiveData.returns.filter(r => r.needs_cz_return).length;
            }
        }

        renderArchivePreview(currentArchiveData);
        showToast('Честный Знак', `Статусы обновлены из ГИС МТ (${updatedCount} кодов)`, 'success');
        if (typeof loadOrders === 'function') loadOrders();
    } catch (e) {
        showToast('Ошибка сверки с ЧЗ', e.message, 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = origHtml;
        }
    }
}

function switchArchiveTab(tab) {
    const salesContent = document.getElementById('archiveTabSalesContent');
    const returnsContent = document.getElementById('archiveTabReturnsContent');
    const salesBtn = document.getElementById('archiveTabSalesBtn');
    const returnsBtn = document.getElementById('archiveTabReturnsBtn');

    if (tab === 'sales') {
        salesContent.style.display = 'block';
        returnsContent.style.display = 'none';
        salesBtn.style.background = 'rgba(34, 197, 94, 0.15)';
        salesBtn.style.color = '#4ade80';
        salesBtn.style.borderBottom = '2px solid #22c55e';
        returnsBtn.style.background = 'transparent';
        returnsBtn.style.color = 'var(--text-muted)';
        returnsBtn.style.borderBottom = 'none';
    } else {
        salesContent.style.display = 'none';
        returnsContent.style.display = 'block';
        returnsBtn.style.background = 'rgba(234, 179, 8, 0.15)';
        returnsBtn.style.color = '#facc15';
        returnsBtn.style.borderBottom = '2px solid #eab308';
        salesBtn.style.background = 'transparent';
        salesBtn.style.color = 'var(--text-muted)';
        salesBtn.style.borderBottom = 'none';
    }
}

function toggleAllArchiveCheckboxes(type, checked) {
    const selector = type === 'withdrawals' ? '.archive-item-withdrawal' : '.archive-item-return';
    document.querySelectorAll(selector).forEach(cb => {
        cb.checked = checked;
    });
}

async function submitArchiveProcessing() {
    if (!currentArchiveData || !currentSellerId) return;

    const selectedWithdrawals = [];
    document.querySelectorAll('.archive-item-withdrawal:checked').forEach(cb => {
        const idx = parseInt(cb.getAttribute('data-idx'));
        if (currentArchiveData.withdrawals?.[idx]) {
            selectedWithdrawals.push(currentArchiveData.withdrawals[idx]);
        }
    });

    const selectedReturns = [];
    document.querySelectorAll('.archive-item-return:checked').forEach(cb => {
        const idx = parseInt(cb.getAttribute('data-idx'));
        if (currentArchiveData.returns?.[idx]) {
            selectedReturns.push(currentArchiveData.returns[idx]);
        }
    });

    if (selectedWithdrawals.length === 0 && selectedReturns.length === 0) {
        return showToast('Внимание', 'Не выбрано ни одной позиции для обработки', 'warning');
    }

    const signMode = document.getElementById('archiveSignModeSelect').value;
    const btn = document.getElementById('archiveProcessBtn');
    const loader = btn.querySelector('.loader');
    const btnText = btn.querySelector('.btn-text');

    loader.style.display = 'inline-block';
    btnText.style.display = 'none';
    btn.disabled = true;

    try {
        const res = await apiFetch(`/sellers/${currentSellerId}/archive/process`, {
            method: 'POST',
            body: JSON.stringify({
                withdrawals: selectedWithdrawals,
                returns: selectedReturns,
                sign_mode: signMode,
            })
        });

        if (signMode === 'client_cades' && res.cades_payloads && res.cades_payloads.length > 0) {
            showToast('КриптоПро', `Подписание ${res.cades_payloads.length} документов в браузере...`, 'info');
            closeModal('archivePreviewModal');

            // Sign each payload with CAdES browser plugin
            let successCount = 0;
            for (const item of res.cades_payloads) {
                try {
                    const signature = await signDataWithCryptoPro(item.document_base64, null);
                    await apiFetch(`/sellers/${currentSellerId}/kiz/submit-signed-document`, {
                        method: 'POST',
                        body: JSON.stringify({
                            document_type: item.type,
                            document_base64: item.document_base64,
                            signature_base64: signature,
                            order_ids: item.order_id ? [item.order_id] : [],
                            action: item.action,
                        })
                    });
                    successCount++;
                } catch (signErr) {
                    showToast('Ошибка ЭЦП', `Не удалось подписать заказ #${item.order_id}: ${signErr.message}`, 'error');
                }
            }
            showToast('Успешно', `Подписано и отправлено в ГИС МТ: ${successCount} из ${res.cades_payloads.length}`, 'success');
        } else {
            showToast('Успешно', res.message || 'Операции запущены', 'success');
            closeModal('archivePreviewModal');
        }

        if (typeof loadDashboard === 'function') await loadDashboard();
        if (typeof loadOrders === 'function') await loadOrders();

    } catch (e) {
        showToast('Ошибка обработки', e.message, 'error');
    } finally {
        loader.style.display = 'none';
        btnText.style.display = 'inline';
        btn.disabled = false;
    }
}

async function updateSignatureBadge() {
    if (!currentSellerId) return;
    try {
        const batches = await apiFetch(`/sellers/${currentSellerId}/kiz/signature-batches?status=PENDING_SIGNATURE`);
        const badge = document.getElementById('navSignatureBadge');
        if (badge) {
            const count = Array.isArray(batches) ? batches.length : 0;
            badge.innerText = count;
            badge.style.display = count > 0 ? 'inline-block' : 'none';
        }
    } catch (e) {
        console.log("Badge update note:", e);
    }
}

async function loadSignatureBatches() {
    if (!currentSellerId) {
        const container = document.getElementById('signatureActiveBatchContainer');
        if (container) {
            container.innerHTML = `
                <div class="glass-card" style="text-align: center; padding: 40px; color: var(--text-muted);">
                    ⚠️ Пожалуйста, выберите магазин в шапке панели
                </div>`;
        }
        return;
    }

    const container = document.getElementById('signatureActiveBatchContainer');
    if (container) {
        container.innerHTML = `
            <div style="text-align: center; padding: 40px; color: var(--text-muted);">
                <div class="loader" style="display:inline-block; margin-bottom: 8px;"></div>
                <div>Загрузка очереди на подписание...</div>
            </div>`;
    }

    try {
        const batches = await apiFetch(`/sellers/${currentSellerId}/kiz/signature-batches`);
        currentSignatureBatches = batches || [];

        // Find first batch pending signature
        const pendingBatch = currentSignatureBatches.find(b => b.status === 'PENDING_SIGNATURE');
        if (pendingBatch) {
            const details = await apiFetch(`/sellers/${currentSellerId}/kiz/signature-batches/${pendingBatch.id}`);
            activeBatchDetails = details;
            renderActiveBatch(details);
        } else {
            activeBatchDetails = null;
            renderEmptyBatchState();
        }

        renderBatchesHistory(currentSignatureBatches);
        updateSignatureBadge();

    } catch (e) {
        if (container) {
            container.innerHTML = `
                <div class="glass-card" style="text-align: center; padding: 30px; color: var(--status-cancelled);">
                    ❌ Ошибка загрузки очереди: ${e.message}
                </div>`;
        }
    }
}

function renderEmptyBatchState() {
    const container = document.getElementById('signatureActiveBatchContainer');
    if (!container) return;

    container.innerHTML = `
        <div class="glass-card" style="text-align: center; padding: 48px 24px; border: 1px dashed rgba(124, 58, 237, 0.3);">
            <div style="font-size: 40px; margin-bottom: 12px;">🎉</div>
            <h3 style="font-size: 18px; margin-bottom: 8px; color: var(--text-main);">Нет пакетов, ожидающих подписания ЭЦП</h3>
            <p style="color: var(--text-muted); font-size: 14px; max-width: 500px; margin: 0 auto 20px;">
                Все загруженные отчёты обработаны. Как только менеджер пришлёт файл <code>archive.xlsx</code> в Telegram-бот (или вы загрузите его вручную), здесь появится готовый список на ввод и вывод КИЗ.
            </p>
            <button class="btn btn-primary" onclick="openArchiveFileInput()" style="background: linear-gradient(135deg, #7c3aed, #4f46e5);">
                <span>📁</span> Загрузить отчёт вручную
            </button>
        </div>
    `;
}

function renderActiveBatch(batch) {
    const container = document.getElementById('signatureActiveBatchContainer');
    if (!container) return;

    const data = batch.data_payload || {};
    const withdrawals = data.withdrawals || [];
    const returns = data.returns || [];
    const summary = data.summary || {};

    const sales_needing = summary.sales_needing_withdrawal !== undefined ? summary.sales_needing_withdrawal : (batch.sales_count || 0);
    const returns_needing = summary.returns_needing_cz_return !== undefined ? summary.returns_needing_cz_return : (batch.returns_count || 0);
    const already_withdrawn = summary.sales_already_withdrawn !== undefined ? summary.sales_already_withdrawn : (batch.already_withdrawn_count || 0);
    const already_in_circ = summary.returns_already_in_circulation !== undefined ? summary.returns_already_in_circulation : Math.max(0, returns.length - returns_needing);

    const totalToSign = sales_needing + returns_needing;
    const dateStr = batch.created_at ? new Date(batch.created_at).toLocaleString('ru-RU') : '—';
    const sourceIcon = batch.source === 'telegram' ? '📱 Telegram-бот' : '🌐 Веб-загрузка';

    container.innerHTML = `
        <div class="glass-card" style="border: 1px solid rgba(124, 58, 237, 0.4); box-shadow: 0 4px 20px rgba(124, 58, 237, 0.1);">
            <div style="display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 12px; margin-bottom: 16px; padding-bottom: 16px; border-bottom: 1px solid var(--border-color);">
                <div>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="badge" style="background: rgba(124, 58, 237, 0.2); color: #c4b5fd; font-weight: 700;">
                            ПАКЕТ #${batch.id.substring(0, 8)}
                        </span>
                        <span class="badge" style="background: rgba(245, 158, 11, 0.2); color: #fbbf24;">
                            Ожидает подписания ЭЦП
                        </span>
                    </div>
                    <div style="font-size: 13px; color: var(--text-muted); margin-top: 6px;">
                        Файл: <b>${batch.filename}</b> · Источник: ${sourceIcon} · Получен: ${dateStr}
                    </div>
                </div>
                <div style="display: flex; gap: 8px; align-items: center;">
                    <button class="btn btn-secondary btn-sm" id="btnSyncBatchCz" onclick="syncBatchCzLive('${batch.id}')" title="Запросить свежие статусы всех кодов КИЗ напрямую из ГИС МТ True API" style="display: flex; align-items: center; gap: 4px;">
                        <span>🔄</span> Сверить с ЧЗ
                    </button>
                    <button class="btn btn-danger btn-sm" onclick="cancelBatchAction('${batch.id}')" title="Отменить этот пакет">
                        ✕ Отклонить
                    </button>
                </div>
            </div>

            <!-- Summary Stats -->
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 20px;">
                <div style="background: rgba(34, 197, 94, 0.1); border: 1px solid rgba(34, 197, 94, 0.2); padding: 12px 16px; border-radius: 8px;">
                    <div style="font-size: 11px; color: #4ade80; font-weight: 600;">ВЫВОД ИЗ ОБОРОТА (ЧЕКИ)</div>
                    <div style="font-size: 22px; font-weight: 700; color: #4ade80; margin-top: 2px;">${sales_needing}</div>
                    <div style="font-size: 11px; color: var(--text-muted);">Требуют выбытия</div>
                </div>
                <div style="background: rgba(245, 158, 11, 0.1); border: 1px solid rgba(245, 158, 11, 0.2); padding: 12px 16px; border-radius: 8px;">
                    <div style="font-size: 11px; color: #fbbf24; font-weight: 600;">ВВОД В ОБОРОТ (ВОЗВРАТЫ)</div>
                    <div style="font-size: 22px; font-weight: 700; color: #fbbf24; margin-top: 2px;">${returns_needing}</div>
                    <div style="font-size: 11px; color: var(--text-muted);">Требуют возврата</div>
                </div>
                <div style="background: rgba(148, 163, 184, 0.1); border: 1px solid rgba(148, 163, 184, 0.2); padding: 12px 16px; border-radius: 8px;">
                    <div style="font-size: 11px; color: #cbd5e1; font-weight: 600;">УЖЕ ВЫБЫЛИ РАНЕЕ</div>
                    <div style="font-size: 22px; font-weight: 700; color: #cbd5e1; margin-top: 2px;">${already_withdrawn}</div>
                    <div style="font-size: 11px; color: var(--text-muted);">Повторный вывод не нужен</div>
                </div>
                <div style="background: rgba(59, 130, 246, 0.1); border: 1px solid rgba(59, 130, 246, 0.2); padding: 12px 16px; border-radius: 8px;">
                    <div style="font-size: 11px; color: #60a5fa; font-weight: 600;">УЖЕ В ОБОРОТЕ</div>
                    <div style="font-size: 22px; font-weight: 700; color: #60a5fa; margin-top: 2px;">${already_in_circ}</div>
                    <div style="font-size: 11px; color: var(--text-muted);">Готовы к привязке</div>
                </div>
            </div>

            <!-- Signing Controls Block -->
            <div style="background: rgba(124, 58, 237, 0.08); border: 1px solid rgba(124, 58, 237, 0.25); border-radius: 10px; padding: 16px; margin-bottom: 20px;">
                <div style="font-weight: 600; font-size: 14px; margin-bottom: 10px; display: flex; align-items: center; gap: 6px;">
                    <span>🔏</span> Параметры подписания ЭЦП (КриптоПро)
                </div>
                <div style="display: flex; gap: 12px; align-items: center; flex-wrap: wrap;">
                    <div style="flex: 1; min-width: 250px;">
                        <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Сертификат УКЭП (КриптоПро Browser Plugin):</label>
                        <select id="batchCertSelect" class="form-control" style="width: 100%;">
                            <option value="">Поиск сертификатов ЭЦП...</option>
                        </select>
                    </div>
                    <div style="width: 200px;">
                        <label style="font-size: 11px; color: var(--text-muted); display: block; margin-bottom: 4px;">Режим подписания:</label>
                        <select id="batchSignModeSelect" class="form-control" style="width: 100%;">
                            <option value="client_cades">Браузерная ЭЦП (CADES)</option>
                            <option value="server">Серверная подпись</option>
                        </select>
                    </div>
                    <div style="align-self: flex-end;">
                        <button class="btn btn-primary" id="btnSignSubmitBatch" onclick="submitBatchSigningAction('${batch.id}')" style="background: linear-gradient(135deg, #10b981, #059669); font-weight: 700; padding: 10px 20px; box-shadow: 0 4px 12px rgba(16, 185, 129, 0.3);">
                            <span class="btn-text">✍️ Подписать и отправить в ГИС МТ (${totalToSign} шт.)</span>
                            <div class="loader" style="display:none;"></div>
                        </button>
                    </div>
                </div>
            </div>

            <!-- Detail Tabs for Sales with Receipts and Returns -->
            <div style="display: flex; gap: 8px; border-bottom: 1px solid var(--border-color); margin-bottom: 12px;">
                <button id="batchTabSalesBtn" class="btn" style="background: rgba(34, 197, 94, 0.15); color: #4ade80; border-bottom: 2px solid #22c55e; border-radius: 6px 6px 0 0; padding: 8px 16px; font-weight: 600;" onclick="switchBatchTab('sales')">
                    🟢 Продажи с чеками (${withdrawals.length}) ${sales_needing > 0 ? `<span style="font-size:11px; opacity:0.85;">(${sales_needing} к выводу)</span>` : ''}
                </button>
                <button id="batchTabReturnsBtn" class="btn" style="background: transparent; color: var(--text-muted); border-radius: 6px 6px 0 0; padding: 8px 16px; font-weight: 600;" onclick="switchBatchTab('returns')">
                    🔄 Возвраты в оборот (${returns.length}) ${returns_needing > 0 ? `<span style="font-size:11px; opacity:0.85;">(${returns_needing} к возврату)</span>` : ''}
                </button>
            </div>

            <!-- Sales Table -->
            <div id="batchTabSalesContent">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                    <div style="font-size: 13px; color: var(--text-muted);">
                        Будет сформирован документ вывода из оборота ГИС МТ (<code>LK_RECEIPT</code>) по причине «Дистанционная продажа».
                    </div>
                    <div style="display: flex; gap: 8px;">
                        <button class="btn btn-secondary" style="font-size: 12px; padding: 4px 10px;" onclick="toggleAllBatchCheckboxes('withdrawals', true)">Выбрать все</button>
                        <button class="btn btn-secondary" style="font-size: 12px; padding: 4px 10px;" onclick="toggleAllBatchCheckboxes('withdrawals', false)">Снять выбор</button>
                    </div>
                </div>
                <div class="table-container" style="max-height: 340px; overflow-y: auto;">
                    <table>
                        <thead>
                            <tr>
                                <th style="width: 40px;"><input type="checkbox" id="batchSelectAllWithdrawals" onchange="toggleAllBatchCheckboxes('withdrawals', this.checked)" ${sales_needing > 0 ? 'checked' : ''}></th>
                                <th>№ задания</th>
                                <th>Стикер</th>
                                <th>КИЗ / Код маркировки</th>
                                <th>Номер чека</th>
                                <th>Дата чека</th>
                                <th>Стоимость</th>
                                <th>Статус в ЧЗ</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${withdrawals.length === 0 ? '<tr><td colspan="8" style="text-align:center; padding:16px; color:var(--text-muted);">Нет продаж для вывода</td></tr>' : 
                                withdrawals.map((w, idx) => {
                                    const needsWithdrawal = Boolean(w.needs_withdrawal);
                                    const isSelected = w.selected !== false && needsWithdrawal;
                                    const statusBadge = needsWithdrawal
                                        ? `<span class="badge badge-warning">⚠️ Требует выбытия</span>`
                                        : `<span class="badge badge-delivered">✅ Выведен</span>`;
                                    return `
                                        <tr>
                                            <td><input type="checkbox" class="batch-item-withdrawal" data-idx="${idx}" data-kiz="${w.kiz_code || ''}" ${isSelected ? 'checked' : ''} onchange="updateBatchSelectedCount()"></td>
                                            <td style="font-weight:600;">#${w.order_id || '—'}</td>
                                            <td><code>${w.sticker_id || '—'}</code></td>
                                            <td style="font-family: monospace; font-size: 11px;">${w.kiz_code || '—'}</td>
                                            <td><span class="badge" style="background:rgba(59,130,246,0.15); color:#60a5fa; font-weight:600;">🧾 ${w.receipt_number || 'По акту/OTHER'}</span></td>
                                            <td style="color:var(--text-muted); font-size:12px;">${w.receipt_date || '—'}</td>
                                            <td>${w.price ? w.price.toLocaleString('ru-RU') + ' ₽' : '—'}</td>
                                            <td>
                                                <div style="display:flex; flex-direction:column; gap:2px;">
                                                    ${statusBadge}
                                                    ${w.cz_status_desc ? `<span style="font-size:10px; color:var(--text-muted);">${w.cz_status_desc}</span>` : ''}
                                                </div>
                                            </td>
                                        </tr>
                                    `;
                                }).join('')
                            }
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- Returns Table -->
            <div id="batchTabReturnsContent" style="display: none;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                    <div style="font-size: 13px; color: var(--text-muted);">
                        Товары, от которых покупатель отказался. Если КИЗ был выведен, сформируется документ возврата в оборот (<code>LP_RETURN</code>).
                    </div>
                    <div style="display: flex; gap: 8px;">
                        <button class="btn btn-secondary" style="font-size: 12px; padding: 4px 10px;" onclick="toggleAllBatchCheckboxes('returns', true)">Выбрать все</button>
                        <button class="btn btn-secondary" style="font-size: 12px; padding: 4px 10px;" onclick="toggleAllBatchCheckboxes('returns', false)">Снять выбор</button>
                    </div>
                </div>
                <div class="table-container" style="max-height: 340px; overflow-y: auto;">
                    <table>
                        <thead>
                            <tr>
                                <th style="width: 40px;"><input type="checkbox" id="batchSelectAllReturns" onchange="toggleAllBatchCheckboxes('returns', this.checked)" ${returns_needing > 0 ? 'checked' : ''}></th>
                                <th>№ задания</th>
                                <th>Стикер</th>
                                <th>КИЗ / Код маркировки</th>
                                <th>Товар</th>
                                <th>Рекомендуемое действие</th>
                                <th>Статус в ЧЗ</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${returns.length === 0 ? '<tr><td colspan="7" style="text-align:center; padding:16px; color:var(--text-muted);">Нет возвратов для ввода</td></tr>' : 
                                returns.map((r, idx) => {
                                    const needsReturn = Boolean(r.needs_cz_return);
                                    const isSelected = r.selected !== false && needsReturn;
                                    const statusBadge = needsReturn
                                        ? `<span class="badge badge-warning">⚠️ Требует возврата в оборот</span>`
                                        : `<span class="badge badge-delivered">✅ Уже в обороте</span>`;
                                    return `
                                        <tr>
                                            <td><input type="checkbox" class="batch-item-return" data-idx="${idx}" data-kiz="${r.kiz_code || ''}" ${isSelected ? 'checked' : ''} onchange="updateBatchSelectedCount()"></td>
                                            <td style="font-weight:600;">#${r.order_id || '—'}</td>
                                            <td><code>${r.sticker_id || '—'}</code></td>
                                            <td style="font-family: monospace; font-size: 11px;">${r.kiz_code || '—'}</td>
                                            <td>
                                                <div style="font-weight:500;">${r.name || 'Товар'}</div>
                                                <div style="font-size:11px; color:var(--text-muted);">${r.article || ''}</div>
                                            </td>
                                            <td><span class="badge ${needsReturn ? 'badge-warning' : 'badge-delivered'}">${r.action_recommended || (needsReturn ? '⚠️ Требует возврата' : '✅ В обороте')}</span></td>
                                            <td>
                                                <div style="display:flex; flex-direction:column; gap:2px;">
                                                    ${statusBadge}
                                                    ${r.cz_status_desc ? `<span style="font-size:10px; color:var(--text-muted);">${r.cz_status_desc}</span>` : ''}
                                                </div>
                                            </td>
                                        </tr>
                                    `;
                                }).join('')
                            }
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    `;

    // Populate certificates dropdown
    if (typeof populateCertificatesDropdown === 'function') {
        populateCertificatesDropdown('batchCertSelect');
    }

    updateBatchSelectedCount();
}

function updateBatchSelectedCount() {
    const selectedWithdrawals = document.querySelectorAll('.batch-item-withdrawal:checked');
    const selectedReturns = document.querySelectorAll('.batch-item-return:checked');
    const count = selectedWithdrawals.length + selectedReturns.length;

    const btn = document.getElementById('btnSignSubmitBatch');
    if (!btn) return;
    const btnText = btn.querySelector('.btn-text');

    if (count > 0) {
        if (btnText) btnText.textContent = `✍️ Подписать и отправить в ГИС МТ (${count} шт.)`;
        btn.disabled = false;
        btn.style.opacity = '1';
        btn.style.cursor = 'pointer';
        btn.style.background = 'linear-gradient(135deg, #10b981, #059669)';
    } else {
        if (btnText) btnText.textContent = `✅ Все КИЗ уже в актуальном статусе (0 шт.)`;
        btn.disabled = true;
        btn.style.opacity = '0.6';
        btn.style.cursor = 'not-allowed';
        btn.style.background = 'rgba(148, 163, 184, 0.2)';
    }
}

function toggleAllBatchCheckboxes(type, checked) {
    const selector = type === 'withdrawals' ? '.batch-item-withdrawal' : '.batch-item-return';
    document.querySelectorAll(selector).forEach(cb => {
        cb.checked = checked;
    });
    updateBatchSelectedCount();
}

async function syncBatchCzLive(batchId) {
    if (!currentSellerId) return showToast('Ошибка', 'Выберите продавца', 'error');

    const btn = document.getElementById('btnSyncBatchCz');
    const origHtml = btn ? btn.innerHTML : '';
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span>⏳</span> Сверка с ГИС МТ...';
    }

    showToast('Честный Знак', 'Сверка кодов маркировки пакета с True API...', 'info');

    try {
        const res = await apiFetch(`/sellers/${currentSellerId}/kiz/signature-batches/${batchId}/sync-cz`, {
            method: 'POST'
        });

        if (activeBatchDetails && activeBatchDetails.id === batchId) {
            activeBatchDetails.sales_count = res.sales_count;
            activeBatchDetails.returns_count = res.returns_count;
            activeBatchDetails.already_withdrawn_count = res.already_withdrawn_count;
            activeBatchDetails.data_payload = res.data_payload;
            renderActiveBatch(activeBatchDetails);
        } else {
            await loadSignatureBatches();
        }

        showToast('Честный Знак', `Статусы пакета обновлены! К выводу: ${res.sales_count}, К возврату: ${res.returns_count}`, 'success');
        if (typeof loadOrders === 'function') loadOrders();
    } catch (e) {
        showToast('Ошибка сверки с ЧЗ', e.message, 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = origHtml;
        }
    }
}

function switchBatchTab(tab) {
    const salesBtn = document.getElementById('batchTabSalesBtn');
    const returnsBtn = document.getElementById('batchTabReturnsBtn');
    const salesContent = document.getElementById('batchTabSalesContent');
    const returnsContent = document.getElementById('batchTabReturnsContent');

    if (!salesBtn || !returnsBtn || !salesContent || !returnsContent) return;

    if (tab === 'sales') {
        salesBtn.style.background = 'rgba(34, 197, 94, 0.15)';
        salesBtn.style.color = '#4ade80';
        salesBtn.style.borderBottom = '2px solid #22c55e';
        returnsBtn.style.background = 'transparent';
        returnsBtn.style.color = 'var(--text-muted)';
        returnsBtn.style.borderBottom = 'none';
        salesContent.style.display = 'block';
        returnsContent.style.display = 'none';
    } else {
        returnsBtn.style.background = 'rgba(245, 158, 11, 0.15)';
        returnsBtn.style.color = '#fbbf24';
        returnsBtn.style.borderBottom = '2px solid #f59e0b';
        salesBtn.style.background = 'transparent';
        salesBtn.style.color = 'var(--text-muted)';
        salesBtn.style.borderBottom = 'none';
        returnsContent.style.display = 'block';
        salesContent.style.display = 'none';
    }
}

function renderBatchesHistory(batches) {
    const tbody = document.getElementById('signature-batches-history-body');
    if (!tbody) return;

    if (!batches || batches.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; padding: 20px; color: var(--text-muted);">История пока пуста</td></tr>';
        return;
    }

    tbody.innerHTML = batches.map(b => {
        const dateStr = b.created_at ? new Date(b.created_at).toLocaleString('ru-RU') : '—';
        const sourceBadge = b.source === 'telegram' ? '📱 Telegram' : '🌐 Web';
        
        let statusBadge = '<span class="badge" style="background:rgba(245,158,11,0.2); color:#fbbf24;">Ожидает ЭЦП</span>';
        if (b.status === 'COMPLETED') {
            statusBadge = '<span class="badge" style="background:rgba(34,197,94,0.2); color:#4ade80;">✅ Завершён</span>';
        } else if (b.status === 'PARTIALLY_COMPLETED') {
            statusBadge = '<span class="badge" style="background:rgba(59,130,246,0.2); color:#60a5fa;">⚠️ Частично</span>';
        } else if (b.status === 'FAILED') {
            statusBadge = '<span class="badge" style="background:rgba(239,68,68,0.2); color:#f87171;">❌ Ошибка</span>';
        } else if (b.status === 'CANCELLED') {
            statusBadge = '<span class="badge" style="background:rgba(148,163,184,0.2); color:#94a3b8;">Отменён</span>';
        }

        return `
            <tr>
                <td style="font-weight:600;">#${b.id.substring(0, 8)}</td>
                <td><b>${b.filename}</b> <span style="font-size:11px; color:var(--text-muted);">(${sourceBadge})</span></td>
                <td><span style="color:#4ade80; font-weight:600;">${b.sales_count}</span> шт.</td>
                <td><span style="color:#fbbf24; font-weight:600;">${b.returns_count}</span> шт.</td>
                <td>${statusBadge}</td>
                <td>${b.signed_by || '—'}</td>
                <td style="font-size:12px; color:var(--text-muted);">${dateStr}</td>
                <td>
                    ${b.status === 'PENDING_SIGNATURE' ? `
                        <button class="btn btn-primary btn-sm" onclick="submitBatchSigningAction('${b.id}')" style="padding: 4px 8px; font-size: 11px;">
                            ✍️ Подписать
                        </button>
                    ` : `
                        <button class="btn btn-secondary btn-sm" onclick="viewBatchDetailsModal('${b.id}')" style="padding: 4px 8px; font-size: 11px;">
                            Детали
                        </button>
                    `}
                </td>
            </tr>
        `;
    }).join('');
}

async function submitBatchSigningAction(batchId) {
    if (!currentSellerId) return showToast('Ошибка', 'Выберите продавца', 'error');

    const selectedCodes = [];
    document.querySelectorAll('.batch-item-withdrawal:checked, .batch-item-return:checked').forEach(cb => {
        const code = cb.getAttribute('data-kiz');
        if (code) selectedCodes.push(code);
    });

    const btn = document.getElementById('btnSignSubmitBatch');
    const loader = btn?.querySelector('.loader');
    const btnText = btn?.querySelector('.btn-text');
    const signMode = document.getElementById('batchSignModeSelect')?.value || 'client_cades';
    const certSelect = document.getElementById('batchCertSelect');
    const selectedThumbprint = certSelect?.value;

    if (signMode === 'client_cades' && !selectedThumbprint) {
        return showToast('Внимание', 'Пожалуйста, выберите сертификат ЭЦП в выпадающем списке', 'warning');
    }

    if (loader) loader.style.display = 'inline-block';
    if (btnText) btnText.style.display = 'none';
    if (btn) btn.disabled = true;

    try {
        // 1. Prepare documents
        showToast('ЭЦП', 'Подготовка канонических документов ГИС МТ...', 'info');
        const prepRes = await apiFetch(`/sellers/${currentSellerId}/kiz/signature-batches/${batchId}/prepare-documents`, {
            method: 'POST',
            body: JSON.stringify({
                selected_kiz_codes: selectedCodes.length > 0 ? selectedCodes : null
            })
        });

        const docs = prepRes.documents || [];
        if (docs.length === 0) {
            return showToast('Информация', 'Все выбранные позиции уже в актуальном статусе, подписание не требуется', 'info');
        }

        let signedDocs = [];
        let certSubject = '';

        if (signMode === 'client_cades') {
            showToast('КриптоПро', `Подписание ${docs.length} документов через плагин...`, 'info');
            const selectedOpt = certSelect?.options[certSelect.selectedIndex];
            certSubject = selectedOpt ? selectedOpt.text : 'Сертификат УКЭП';

            for (let i = 0; i < docs.length; i++) {
                const item = docs[i];
                try {
                    const sig = await signDataWithCryptoPro(item.document_base64, selectedThumbprint);
                    signedDocs.push({
                        action: item.action,
                        type: item.type,
                        order_id: item.order_id,
                        kiz_code: item.kiz_code,
                        document_base64: item.document_base64,
                        signature_base64: sig,
                    });
                } catch (signErr) {
                    throw new Error(`Ошибка подписания документа #${i+1}: ${signErr.message}`);
                }
            }
        }

        // 2. Submit signed documents
        showToast('Честный Знак', 'Отправка подписанного пакета в ГИС МТ...', 'info');
        const submitRes = await apiFetch(`/sellers/${currentSellerId}/kiz/signature-batches/${batchId}/submit-signed`, {
            method: 'POST',
            body: JSON.stringify({
                sign_mode: signMode,
                cert_subject: certSubject,
                signed_documents: signedDocs,
            })
        });

        showToast('Успешно', `Пакет обработан! Успешно отправлено: ${submitRes.successful_submissions} документов`, 'success');
        await loadSignatureBatches();
        if (typeof loadDashboard === 'function') await loadDashboard();
        if (typeof loadOrders === 'function') await loadOrders();

    } catch (e) {
        showToast('Ошибка подписания пакета', e.message, 'error');
    } finally {
        if (loader) loader.style.display = 'none';
        if (btnText) btnText.style.display = 'inline';
        if (btn) btn.disabled = false;
        updateBatchSelectedCount();
    }
}

async function cancelBatchAction(batchId) {
    if (!confirm('Вы уверены, что хотите отклонить этот пакет?')) return;
    try {
        await apiFetch(`/sellers/${currentSellerId}/kiz/signature-batches/${batchId}`, {
            method: 'DELETE'
        });
        showToast('Пакет отклонен', 'Пакет удален из очереди на подписание', 'info');
        await loadSignatureBatches();
    } catch (e) {
        showToast('Ошибка', e.message, 'error');
    }
}

async function viewBatchDetailsModal(batchId) {
    try {
        const details = await apiFetch(`/sellers/${currentSellerId}/kiz/signature-batches/${batchId}`);
        activeBatchDetails = details;
        renderActiveBatch(details);
        window.scrollTo({ top: 0, behavior: 'smooth' });
    } catch (e) {
        showToast('Ошибка', e.message, 'error');
    }
}

// Global window bindings for inline HTML onclick handlers
window.openArchiveFileInput = openArchiveFileInput;
window.handleArchiveFileSelect = handleArchiveFileSelect;
window.handleArchiveDrop = handleArchiveDrop;
window.uploadAndPreviewArchive = uploadAndPreviewArchive;
window.renderArchivePreview = renderArchivePreview;
window.syncArchiveCzLive = syncArchiveCzLive;
window.syncBatchCzLive = syncBatchCzLive;
window.switchArchiveTab = switchArchiveTab;
window.toggleAllArchiveCheckboxes = toggleAllArchiveCheckboxes;
window.toggleAllBatchCheckboxes = toggleAllBatchCheckboxes;
window.updateBatchSelectedCount = updateBatchSelectedCount;
window.submitArchiveProcessing = submitArchiveProcessing;
window.loadSignatureBatches = loadSignatureBatches;
window.updateSignatureBadge = updateSignatureBadge;
window.submitBatchSigningAction = submitBatchSigningAction;
window.cancelBatchAction = cancelBatchAction;
window.switchBatchTab = switchBatchTab;
window.viewBatchDetailsModal = viewBatchDetailsModal;

