/**
 * WB FBS Manager — Sellers Management & Store Settings
 */

let currentEditingSellerId = null;

function openAddSellerModal() {
    currentEditingSellerId = null;
    document.getElementById('sellerForm').reset();
    document.getElementById('sellerModalTitle').innerText = "Добавить нового продавца";
    document.getElementById('wbTokenRequiredLabel').style.display = 'inline';
    const testResultBox = document.getElementById('sellerTestResultBox');
    if (testResultBox) testResultBox.style.display = 'none';

    // Populate crypto certificates
    populateCertificatesDropdown();
    openModal('sellerModal');
}

async function editSeller(sellerId) {
    currentEditingSellerId = sellerId;
    document.getElementById('sellerModalTitle').innerText = "Редактировать продавца";
    document.getElementById('wbTokenRequiredLabel').style.display = 'none';
    const testResultBox = document.getElementById('sellerTestResultBox');
    if (testResultBox) testResultBox.style.display = 'none';

    showToast('Загрузка...', 'Получение данных продавца', 'info');
    
    try {
        const seller = await apiFetch(`/sellers/${sellerId}`);
        document.getElementById('seller_name').value = seller.name || '';
        document.getElementById('seller_wb_supplier_id').value = seller.wb_supplier_id || '';
        document.getElementById('seller_cz_inn').value = seller.cz_inn || '';
        document.getElementById('seller_cz_oms_id').value = seller.cz_oms_id || '';
        document.getElementById('seller_cz_fias').value = seller.mod_fias || '';
        
        const certVal = seller.cryptopro_cert_thumbprint || seller.cz_cert_path || '';
        document.getElementById('seller_cert_path').value = certVal;
        
        document.getElementById('seller_tg_chat_ids').value = (seller.telegram_chat_ids || []).join(', ');

        // Polling interval (API returns seconds, form shows minutes)
        const intervalMin = Math.max(1, Math.round((seller.polling_interval_seconds || 60) / 60));
        document.getElementById('seller_polling_interval').value = intervalMin;
        document.getElementById('polling_interval_label').textContent = intervalMin + ' мин';

        // Digest settings
        document.getElementById('seller_digest_enabled').checked = seller.digest_enabled !== false;
        document.getElementById('seller_digest_hour').value = seller.digest_hour ?? 8;
        document.getElementById('seller_digest_minute').value = seller.digest_minute ?? 0;
        const tzSelect = document.getElementById('seller_digest_timezone');
        const tzVal = seller.digest_timezone || 'Europe/Moscow';
        const tzOption = Array.from(tzSelect.options).find(o => o.value === tzVal);
        tzSelect.value = tzOption ? tzVal : 'Europe/Moscow';
        
        // Clear sensitive token inputs (so user only enters if changing them)
        document.getElementById('seller_wb_token').value = '';
        document.getElementById('seller_cz_token').value = '';
        document.getElementById('seller_tg_token').value = '';
        
        // Sync certificate select if matches known thumbprint
        await populateCertificatesDropdown();
        const sellerCertSelect = document.getElementById('seller_cert_select');
        if (sellerCertSelect && certVal) {
            const matchOpt = Array.from(sellerCertSelect.options).find(o => o.value.toLowerCase() === certVal.toLowerCase());
            if (matchOpt) {
                sellerCertSelect.value = matchOpt.value;
                onSellerCertChanged();
            }
        }

        openModal('sellerModal');
    } catch (e) {
        showToast('Ошибка', 'Не удалось получить данные продавца: ' + e.message, 'error');
    }
}

function onSellerCertChanged() {
    const select = document.getElementById('seller_cert_select');
    if (!select) return;
    const thumb = select.value;
    const certInput = document.getElementById('seller_cert_path');
    const previewEl = document.getElementById('seller_cert_preview');

    if (!thumb) {
        if (previewEl) previewEl.innerHTML = '';
        return;
    }

    if (certInput) certInput.value = thumb;

    const cert = cryptoProCerts.find(c => c.thumbprint === thumb);
    if (cert) {
        if (previewEl) {
            previewEl.innerHTML = `<span style="color:#34d399;">✓ Выбран:</span> <strong>${cert.subject}</strong> ${cert.inn ? `(ИНН: ${cert.inn})` : ''} — действует до ${new Date(cert.validTo).toLocaleDateString('ru-RU')}`;
        }
        // Auto-fill INN if empty
        const innInput = document.getElementById('seller_cz_inn');
        if (innInput && !innInput.value && cert.inn) {
            innInput.value = cert.inn;
            showToast('Автозаполнение', `ИНН ${cert.inn} подставлен из сертификата ЭЦП`, 'info');
        }
    }
}

async function loadSellers() {
    let sellers = [];
    try {
        const data = await apiFetch('/sellers');
        if (Array.isArray(data)) sellers = data;
        else if (data && Array.isArray(data.items)) sellers = data.items;
    } catch(e) {
        console.error("Ошибка загрузки продавцов:", e);
    }

    const tbody = document.getElementById('sellers-table-body');
    if (!tbody) return;

    if (sellers.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 24px;">Продавцы не найдены. Нажмите "Добавить продавца" или "Demo Data".</td></tr>`;
        return;
    }

    tbody.innerHTML = sellers.map(s => `
        <tr>
            <td style="font-weight: 600;">${s.name}</td>
            <td style="font-family:monospace;">${s.wb_supplier_id || '-'}</td>
            <td>${s.cz_inn || '-'}</td>
            <td><span class="badge ${s.is_active ? 'bg-delivered' : 'bg-cancelled'}">${s.is_active ? 'Активен' : 'Отключен'}</span></td>
            <td><span class="badge ${s.polling_enabled ? 'bg-new' : 'kiz-pending'}">${s.polling_enabled ? 'Включен' : 'Выключен'}</span></td>
            <td>
                <div style="display:flex; gap:6px;">
                    <button class="icon-btn" title="Редактировать" onclick="editSeller('${s.id}')">✏️</button>
                    <button class="icon-btn" title="Проверить все токены и адресатов" onclick="testConnectionFor('${s.id}')">🔌</button>
                    <button class="icon-btn" title="Переключить авто-опрос" onclick="togglePollingFor('${s.id}', ${!s.polling_enabled})">⚡</button>
                    <button class="icon-btn" title="Отключить продавца" style="color: var(--status-cancelled)" onclick="deleteSeller('${s.id}', '${(s.name || '').replace(/'/g, "\\'")}')">🗑️</button>
                </div>
            </td>
        </tr>
    `).join('');
}

async function saveSeller() {
    const btn = document.getElementById('saveSellerBtn');
    btn.classList.add('loading');

    const name = document.getElementById('seller_name').value.trim();
    const wbToken = document.getElementById('seller_wb_token').value.trim();
    const wbSupplierId = document.getElementById('seller_wb_supplier_id').value.trim();
    const czInn = document.getElementById('seller_cz_inn').value.trim();
    const czOmsId = document.getElementById('seller_cz_oms_id').value.trim();
    const czFias = document.getElementById('seller_cz_fias').value.trim();
    const czToken = document.getElementById('seller_cz_token').value.trim();
    const certPath = document.getElementById('seller_cert_path').value.trim();
    const tgToken = document.getElementById('seller_tg_token').value.trim();
    const tgChatIdsRaw = document.getElementById('seller_tg_chat_ids').value.trim();

    if (!name) {
        showToast('Ошибка валидации', 'Введите название продавца', 'error');
        btn.classList.remove('loading');
        return;
    }

    const tgChatIds = tgChatIdsRaw ? tgChatIdsRaw.split(',').map(s => s.trim()).filter(Boolean) : [];

    try {
        if (!currentEditingSellerId) {
            if (!wbToken) {
                showToast('Ошибка валидации', 'API токен WB обязателен для нового продавца', 'error');
                btn.classList.remove('loading');
                return;
            }
            const payload = {
                name,
                wb_api_token: wbToken,
                wb_supplier_id: wbSupplierId || null,
                cz_inn: czInn || null,
                cz_oms_id: czOmsId || null,
                cz_token: czToken || null,
                cz_cert_path: certPath || null,
                cryptopro_cert_thumbprint: certPath || null,
                mod_fias: czFias || null,
                telegram_bot_token: tgToken || null,
                telegram_chat_ids: tgChatIds.length ? tgChatIds : null,
                polling_interval_minutes: parseInt(document.getElementById('seller_polling_interval').value) || 1,
                digest: {
                    enabled: document.getElementById('seller_digest_enabled').checked,
                    hour: parseInt(document.getElementById('seller_digest_hour').value) || 8,
                    minute: parseInt(document.getElementById('seller_digest_minute').value) || 0,
                    timezone: document.getElementById('seller_digest_timezone').value || 'Europe/Moscow',
                },
            };
            await apiFetch('/sellers', {
                method: 'POST',
                body: JSON.stringify(payload)
            });
            showToast('Успех', 'Продавец успешно добавлен в базу данных!', 'success');
        } else {
            const payload = {
                name: name,
                wb_supplier_id: wbSupplierId || null,
                cz_inn: czInn || null,
                cz_oms_id: czOmsId || null,
                cz_cert_path: certPath || null,
                cryptopro_cert_thumbprint: certPath || null,
                mod_fias: czFias || null,
                telegram_chat_ids: tgChatIds,
                polling_interval_minutes: parseInt(document.getElementById('seller_polling_interval').value) || 1,
                digest_enabled: document.getElementById('seller_digest_enabled').checked,
                digest_hour: parseInt(document.getElementById('seller_digest_hour').value) || 8,
                digest_minute: parseInt(document.getElementById('seller_digest_minute').value) || 0,
                digest_timezone: document.getElementById('seller_digest_timezone').value || 'Europe/Moscow',
            };
            // Only send tokens if user typed new values (prevent resetting encrypted secrets)
            if (wbToken) payload.wb_api_token = wbToken;
            if (czToken) payload.cz_token = czToken;
            if (tgToken) payload.telegram_bot_token = tgToken;

            await apiFetch(`/sellers/${currentEditingSellerId}`, {
                method: 'PATCH',
                body: JSON.stringify(payload)
            });
            showToast('Успех', 'Настройки продавца сохранены и обновлены в базе данных!', 'success');
        }

        closeModal('sellerModal');
        await loadSellers();
        await loadSellersForDropdown();
    } catch (e) {
        showToast('Ошибка', 'Не удалось сохранить продавца: ' + e.message, 'error');
    } finally {
        btn.classList.remove('loading');
    }
}

async function deleteSeller(sellerId, sellerName) {
    if (!confirm(`Вы действительно хотите отключить продавца "${sellerName}"?`)) return;

    try {
        await apiFetch(`/sellers/${sellerId}`, { method: 'DELETE' });
        showToast('Продавец отключен', `Продавец "${sellerName}" отключен`, 'success');
        await loadSellers();
        await loadSellersForDropdown();
    } catch (e) {
        showToast('Ошибка', 'Не удалось отключить продавца: ' + e.message, 'error');
    }
}

async function testConnectionFor(sellerId) {
    showToast('Проверка...', 'Проверка связи с WB, ЧЗ и Telegram-получателями...', 'info');
    try {
        const res = await apiFetch(`/sellers/${sellerId}/test-connection`, { method: 'POST' });
        if (res.success) {
            showToast('Проверка связи успешна', res.message, 'success');
        } else {
            showToast('Внимание при проверке', res.message, 'warning');
        }
        return res;
    } catch (e) {
        showToast('Ошибка', 'Проверка соединения завершилась ошибкой: ' + e.message, 'error');
        return { success: false, message: e.message };
    }
}

async function testSellerConnection() {
    if (!currentEditingSellerId) {
        return showToast('Инфо', 'Сохраните продавца перед проверкой соединения', 'info');
    }
    const testResultBox = document.getElementById('sellerTestResultBox');
    if (testResultBox) {
        testResultBox.style.display = 'block';
        testResultBox.innerHTML = '<span style="color:var(--text-muted);">⏳ Тестирование подключений WB API, Честный Знак и Telegram адресатов...</span>';
    }

    const res = await testConnectionFor(currentEditingSellerId);
    if (testResultBox && res) {
        testResultBox.innerHTML = `<div style="line-height:1.6; white-space:pre-line;">${res.message}</div>`;
        testResultBox.style.borderColor = res.success ? 'rgba(34, 197, 94, 0.4)' : 'rgba(239, 68, 68, 0.4)';
        testResultBox.style.background = res.success ? 'rgba(34, 197, 94, 0.1)' : 'rgba(239, 68, 68, 0.1)';
    }
}

async function togglePollingFor(sellerId, enable) {
    try {
        const res = await apiFetch(`/sellers/${sellerId}/toggle-polling?enabled=${enable}`, { method: 'POST' });
        showToast('Авто-опрос', res.message, 'success');
        await loadSellers();
    } catch (e) {
        showToast('Ошибка', e.message, 'error');
    }
}
