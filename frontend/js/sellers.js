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
        
        // Token inputs & persistent status indicators
        const wbInput = document.getElementById('seller_wb_token');
        const czInput = document.getElementById('seller_cz_token');
        const tgInput = document.getElementById('seller_tg_token');
        const czStatusEl = document.getElementById('seller_cz_token_status');

        wbInput.value = '';
        czInput.value = '';
        tgInput.value = '';

        if (seller.has_wb_token) {
            wbInput.placeholder = '●●●●●●●● (токен сохранен в БД)';
        } else {
            wbInput.placeholder = 'Введите API токен Wildberries';
        }

        if (seller.has_cz_token) {
            czInput.placeholder = '●●●●●●●● (токен Честного Знака сохранен в БД)';
            if (czStatusEl) {
                czStatusEl.innerHTML = `<span style="color:var(--status-delivered); font-weight:600;">✅ Токен активен в БД (${seller.cz_token_preview || 'сохранен'})</span>`;
            }
        } else {
            czInput.placeholder = 'Оставьте пустым или получите через ЭЦП';
            if (czStatusEl) {
                czStatusEl.innerHTML = `<span style="color:var(--text-muted);">Токен не установлен. Нажмите «Получить через ЭЦП» или введите вручную.</span>`;
            }
        }

        if (seller.has_telegram_token) {
            tgInput.placeholder = '●●●●●●●● (токен бота сохранен в БД)';
        } else {
            tgInput.placeholder = '123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ';
        }
        
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

async function fetchCzTokenViaBrowser() {
    if (!currentEditingSellerId) {
        return showToast('Внимание', 'Сначала выберите или сохраните продавца', 'warning');
    }
    const inn = document.getElementById('seller_cz_inn').value.trim();
    if (!inn) {
        return showToast('Внимание', 'Укажите ИНН организации для Честного Знака', 'warning');
    }

    if (!window.cadesplugin) {
        return showToast('КриптоПро', 'Плагин CAdES / КриптоПро не обнаружен в браузере. Убедитесь, что плагин установлен и включен.', 'error');
    }

    const btn = document.getElementById('btnFetchCzToken');
    const statusEl = document.getElementById('seller_cz_token_status');
    if (btn) btn.disabled = true;
    if (statusEl) statusEl.innerHTML = '<span style="color:var(--primary-hover);">⏳ Запрос данных для подписи в ГИС МТ...</span>';

    try {
        // 1. Get challenge from True API
        const challenge = await apiFetch(`/sellers/${currentEditingSellerId}/cz-challenge`);
        const authUuid = challenge.uuid;
        const authData = challenge.data;
        if (!authUuid || !authData) {
            throw new Error("Не удалось получить строку аутентификации от ГИС МТ");
        }

        if (statusEl) statusEl.innerHTML = '<span style="color:var(--primary-hover);">✍️ Подписание сертификатом УКЭП...</span>';

        // 2. Open cert store and find matching cert
        const oStore = await window.cadesplugin.CreateObjectAsync("CAdESCOM.Store");
        await oStore.Open(window.cadesplugin.CAPICOM_CURRENT_USER_STORE, window.cadesplugin.CAPICOM_MY_STORE, window.cadesplugin.CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED);
        const certs = await oStore.Certificates;
        const count = await certs.Count;

        if (count === 0) {
            throw new Error("В хранилище сертификатов не найдено личных сертификатов УКЭП");
        }

        // Pick selected cert or matching INN cert or first cert
        const targetThumbprint = document.getElementById('seller_cert_path').value.trim();
        let selectedCert = null;

        for (let i = 1; i <= count; i++) {
            const c = await certs.Item(i);
            const thumb = await c.Thumbprint;
            const sub = await c.SubjectName;
            if (targetThumbprint && thumb.toLowerCase() === targetThumbprint.toLowerCase()) {
                selectedCert = c;
                break;
            }
            if (inn && sub.includes(inn)) {
                selectedCert = c;
                break;
            }
        }
        if (!selectedCert) {
            selectedCert = await certs.Item(1);
        }

        // 3. Create attached CMS signature of authData
        const oSigner = await window.cadesplugin.CreateObjectAsync("CAdESCOM.CPSigner");
        await oSigner.propset_Certificate(selectedCert);

        const oSignedData = await window.cadesplugin.CreateObjectAsync("CAdESCOM.CadesSignedData");
        await oSignedData.propset_Content(authData);

        // Attached signature (false = attached content in signed blob)
        const signature = await oSignedData.SignCades(oSigner, window.cadesplugin.CADESCOM_CADES_BES, false);
        await oStore.Close();

        if (statusEl) statusEl.innerHTML = '<span style="color:var(--primary-hover);">🔐 Получение токена сессии ГИС МТ...</span>';

        // 4. Send signed challenge to API
        const signinRes = await apiFetch(`/sellers/${currentEditingSellerId}/cz-signin`, {
            method: 'POST',
            body: JSON.stringify({
                uuid: authUuid,
                data: signature
            })
        });

        showToast('Успех', signinRes.message || 'Токен Честного Знака успешно обновлен!', 'success');
        if (statusEl) {
            statusEl.innerHTML = `<span style="color:var(--status-delivered);">✅ Токен активен (${signinRes.token_preview || 'сохранен'})</span>`;
        }
        document.getElementById('seller_cz_token').placeholder = 'Токен активен (обновлен через ЭЦП)';
    } catch (e) {
        showToast('Ошибка аутентификации в ЧЗ', e.message, 'error');
        if (statusEl) {
            statusEl.innerHTML = `<span style="color:var(--status-cancelled);">❌ Ошибка: ${e.message}</span>`;
        }
    } finally {
        if (btn) btn.disabled = false;
    }
}
