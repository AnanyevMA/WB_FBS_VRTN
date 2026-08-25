/**
 * WB FBS Manager — KIZ (DataMatrix) Scanner & CryptoPro Electronic Digital Signature (EDS / УКЭП)
 */

async function processKizScan(code) {
    const kizCode = code.trim();
    if(!kizCode) return;
    
    if(!currentSellerId) {
        return showToast('Ошибка', 'Сначала выберите продавца', 'error');
    }

    const specifiedOrderId = document.getElementById('kizOrderSearch').value.trim();
    const orderIdNum = specifiedOrderId ? parseInt(specifiedOrderId) : null;
    const scanHistory = document.getElementById('scan-history-body');

    const timeStr = new Date().toLocaleTimeString('ru-RU');
    
    try {
        const res = await apiFetch(`/sellers/${currentSellerId}/kiz/attach`, {
            method: 'POST',
            body: JSON.stringify({
                kiz_code: kizCode,
                order_id: orderIdNum
            })
        });

        // Append to history table
        const tr = document.createElement('tr');
        const prod = res.product_info || {};
        const isBlocked = prod.blocked_by_ogv || (prod.ogvs && prod.ogvs.length > 0);
        const statusHtml = isBlocked
            ? `<span style="color: var(--status-cancelled); display: flex; align-items: center; gap: 6px; font-weight:500;">
                 ⛔ ОГВ Блокировка: ${prod.validation_message || 'Заблокирован госорганами'}
               </span>`
            : (prod.is_valid === false
                ? `<span style="color: #f59e0b; display: flex; align-items: center; gap: 6px; font-weight:500;">
                     ⚠️ ${prod.validation_message || res.message}
                   </span>`
                : `<span style="color: var(--status-delivered); display: flex; align-items: center; gap: 6px; font-weight:500;">
                     ✅ ${res.message} ${prod.cz_status ? `(${prod.cz_status})` : ''}
                   </span>`);

        tr.innerHTML = `
            <td style="color: var(--text-muted); font-size:13px;">${timeStr}</td>
            <td style="font-family: monospace; font-size: 14px;">${kizCode}</td>
            <td style="font-weight:600;">#${res.order_id}</td>
            <td>${statusHtml}</td>
        `;

        if (scanHistory) {
            if (scanHistory.rows.length === 1 && scanHistory.rows[0].cells.length === 1) {
                scanHistory.innerHTML = '';
            }
            scanHistory.prepend(tr);
        }

        if (isBlocked) {
            showToast('Внимание!', `Код заблокирован ОГВ: ${prod.validation_message}`, 'warning');
        } else {
            showToast('КИЗ отсканирован', res.message, 'success');
        }

    } catch (e) {
        if (scanHistory) {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td style="color: var(--text-muted); font-size:13px;">${timeStr}</td>
                <td style="font-family: monospace; font-size: 14px;">${kizCode}</td>
                <td>${orderIdNum ? '#' + orderIdNum : '-'}</td>
                <td>
                    <span style="color: var(--status-cancelled); display: flex; align-items: center; gap: 6px; font-weight:500;">
                        ❌ ${e.message}
                    </span>
                </td>
            `;
            if (scanHistory.rows.length === 1 && scanHistory.rows[0].cells.length === 1) {
                scanHistory.innerHTML = '';
            }
            scanHistory.prepend(tr);
        }
        showToast('Ошибка маркировки', e.message, 'error');
    }
}

async function triggerCzAction(type) {
    if(!currentSellerId) return showToast('Ошибка', 'Выберите продавца', 'error');
    try {
        const endpoint = type === 'withdraw' ? `/sellers/${currentSellerId}/kiz/withdraw` : `/sellers/${currentSellerId}/kiz/return`;
        const res = await apiFetch(endpoint, { method: 'POST', body: JSON.stringify({}) });
        showToast('Честный Знак', res.message, 'success');
        await loadDashboard();
    } catch (e) {
        showToast('Ошибка', e.message, 'error');
    }
}

/** --- CryptoPro CAdES Browser Plugin --- */

async function initCryptoProPlugin() {
    try {
        if (window.cadesplugin) {
            await checkPluginLoaded();
        } else {
            loadCadesScript();
        }
    } catch (e) {
        console.log("CryptoPro init note:", e);
    }
}

function loadCadesScript() {
    if (document.getElementById('cadesplugin_script')) return;
    const s = document.createElement('script');
    s.id = 'cadesplugin_script';
    s.src = 'https://www.cryptopro.ru/sites/default/files/products/cades/cadesplugin_api.js';
    s.onload = () => {
        setTimeout(checkPluginLoaded, 400);
    };
    s.onerror = () => {
        console.log("External cadesplugin_api.js not reachable, checking native window.cadesplugin");
        checkPluginLoaded();
    };
    document.head.appendChild(s);
}

async function checkPluginLoaded() {
    const badge = document.getElementById('kizSigningPluginBadge');
    if (!window.cadesplugin) {
        if (badge) {
            badge.style.background = 'rgba(148, 163, 184, 0.15)';
            badge.style.color = '#94a3b8';
            badge.style.borderColor = 'rgba(148, 163, 184, 0.3)';
            badge.innerText = 'Плагин не обнаружен';
        }
        return false;
    }
    try {
        await window.cadesplugin;
        isCryptoProAvailable = true;
        if (badge) {
            badge.style.background = 'rgba(16, 185, 129, 0.15)';
            badge.style.color = '#10b981';
            badge.style.borderColor = 'rgba(16, 185, 129, 0.3)';
            badge.innerText = '🟢 Плагин КриптоПро активен';
        }
        await populateCertificatesDropdown();
        return true;
    } catch (err) {
        isCryptoProAvailable = false;
        if (badge) {
            badge.style.background = 'rgba(239, 68, 68, 0.15)';
            badge.style.color = '#f87171';
            badge.style.borderColor = 'rgba(239, 68, 68, 0.3)';
            badge.innerText = '⚠️ Ошибка плагина';
        }
        return false;
    }
}

async function populateCertificatesDropdown() {
    const select = document.getElementById('kizSigningCertSelect');
    const sellerCertSelect = document.getElementById('seller_cert_select');
    
    if (select) select.innerHTML = '<option value="">-- Поиск сертификатов в хранилище... --</option>';
    if (sellerCertSelect) sellerCertSelect.innerHTML = '<option value="">-- Поиск сертификатов в хранилище... --</option>';

    if (!window.cadesplugin) {
        if (select) select.innerHTML = '<option value="">-- КриптоПро плагин не обнаружен (доступна серверная отправка) --</option>';
        if (sellerCertSelect) sellerCertSelect.innerHTML = '<option value="">-- КриптоПро плагин не обнаружен --</option>';
        return;
    }

    try {
        const oStore = await window.cadesplugin.CreateObjectAsync("CAdESCOM.Store");
        await oStore.Open(window.cadesplugin.CAPICOM_CURRENT_USER_STORE, window.cadesplugin.CAPICOM_MY_STORE, window.cadesplugin.CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED);
        const certs = await oStore.Certificates;
        const count = await certs.Count;
        cryptoProCerts = [];

        if (select) select.innerHTML = '<option value="">-- Выберите сертификат для подписания --</option>';
        if (sellerCertSelect) sellerCertSelect.innerHTML = '<option value="">-- Выберите сертификат УКЭП для магазина --</option>';

        for (let i = 1; i <= count; i++) {
            const cert = await certs.Item(i);
            try {
                const subjectName = await cert.SubjectName;
                const validTo = await cert.ValidToDate;
                const thumbprint = await cert.Thumbprint;
                const serial = await cert.SerialNumber;

                let cn = subjectName;
                const cnMatch = subjectName.match(/CN=([^,]+)/);
                if (cnMatch) cn = cnMatch[1];
                let inn = '';
                const innMatch = subjectName.match(/ИНН=([^,]+)/) || subjectName.match(/ИНН ЮЛ=([^,]+)/) || subjectName.match(/INN=([^,]+)/);
                if (innMatch) inn = innMatch[1];

                const certData = {
                    thumbprint: thumbprint,
                    subject: cn,
                    inn: inn,
                    validTo: validTo,
                    serial: serial,
                    rawSubject: subjectName
                };
                cryptoProCerts.push(certData);

                const label = `${cn} ${inn ? `(ИНН: ${inn})` : ''} — до ${new Date(validTo).toLocaleDateString('ru-RU')}`;
                
                if (select) {
                    const opt = document.createElement('option');
                    opt.value = thumbprint;
                    opt.textContent = label;
                    select.appendChild(opt);
                }

                if (sellerCertSelect) {
                    const opt2 = document.createElement('option');
                    opt2.value = thumbprint;
                    opt2.textContent = label;
                    sellerCertSelect.appendChild(opt2);
                }
            } catch (ce) {
                console.log("Error inspecting cert item:", ce);
            }
        }
        await oStore.Close();

        if (cryptoProCerts.length === 0) {
            if (select) select.innerHTML = '<option value="">-- В хранилище "Личные" нет доступных сертификатов --</option>';
            if (sellerCertSelect) sellerCertSelect.innerHTML = '<option value="">-- Сертификаты не найдены --</option>';
        } else {
            if (select && select.options.length > 1) {
                select.selectedIndex = 1;
                onCertSelected();
            }
        }
    } catch (e) {
        console.error("Error reading certs from store:", e);
        if (select) select.innerHTML = `<option value="">-- Ошибка чтения сертификатов: ${e.message} --</option>`;
        if (sellerCertSelect) sellerCertSelect.innerHTML = `<option value="">-- Ошибка чтения: ${e.message} --</option>`;
    }
}

function onCertSelected() {
    const select = document.getElementById('kizSigningCertSelect');
    if (!select) return;
    const thumb = select.value;
    const detailsEl = document.getElementById('kizSigningCertDetails');
    if (!thumb) {
        if (detailsEl) detailsEl.innerHTML = '';
        return;
    }
    const cert = cryptoProCerts.find(c => c.thumbprint === thumb);
    if (cert && detailsEl) {
        detailsEl.innerHTML = `<strong>Владелец:</strong> ${cert.subject}<br><strong>Действителен до:</strong> ${new Date(cert.validTo).toLocaleString('ru-RU')}<br><span style="font-family:monospace; font-size:11px;">Отпечаток (SHA-1): ${cert.thumbprint}</span>`;
    }
}

async function openKizSigningModal(orderIds, action = 'WITHDRAWAL') {
    if (!currentSellerId) return showToast('Ошибка', 'Выберите продавца', 'error');
    if (!orderIds || orderIds.length === 0) return showToast('Инфо', 'Нет выбранных заказов для обработки', 'info');

    currentSigningPayload = {
        seller_id: currentSellerId,
        order_ids: orderIds.map(id => parseInt(id)),
        action: action
    };

    const isWithdrawal = action === 'WITHDRAWAL';
    document.getElementById('kizSigningModalTitle').innerText = isWithdrawal ? '✍️ Вывод маркировки КИЗ из оборота' : '🔄 Возврат маркировки КИЗ в оборот';
    document.getElementById('kizSigningActionName').innerText = isWithdrawal ? 'Вывод из оборота (Дистанционная продажа LK_RECEIPT)' : 'Возврат в оборот (Дистанционная продажа LP_RETURN)';
    document.getElementById('kizSigningActionDesc').innerText = isWithdrawal ? 'Документ дистанционной продажи формируется на VPS и подписывается локальной УКЭП.' : 'Документ возврата в оборот формируется на VPS и подписывается локальной УКЭП.';

    const currentSeller = (currentSellersList || []).find(s => String(s.id) === String(currentSellerId));
    const fiasEl = document.getElementById('kizSigningFiasInfo');
    if (fiasEl) {
        if (currentSeller && currentSeller.mod_fias) {
            fiasEl.innerHTML = `📍 Место деятельности (ФИАС ID): <code style="color:#67e8f9; font-weight:600;">${currentSeller.mod_fias}</code>`;
            fiasEl.style.display = 'block';
        } else {
            fiasEl.innerHTML = `⚠️ <span style="color:#facc15;">ФИАС ID склада не указан в настройках продавца (требуется ГИС МТ при выводе из оборота).</span>`;
            fiasEl.style.display = 'block';
        }
    }

    document.getElementById('kizSigningOrdersCount').innerText = orderIds.length;
    document.getElementById('kizSigningOrdersList').innerHTML = '<div style="color:var(--text-muted); padding:4px;">Загрузка данных заказов...</div>';
    document.getElementById('kizSigningJsonPreview').innerText = 'Подготовка документа на сервере...';
    document.getElementById('kizSigningLogs').style.display = 'none';

    openModal('kizSigningModal');
    await checkPluginLoaded();

    // Prepare document on server
    try {
        const prepRes = await apiFetch(`/sellers/${currentSellerId}/kiz/prepare-document`, {
            method: 'POST',
            body: JSON.stringify({
                action: action,
                order_ids: currentSigningPayload.order_ids
            })
        });

        currentSigningPayload.prepared = prepRes;
        document.getElementById('kizSigningJsonPreview').innerText = JSON.stringify(JSON.parse(prepRes.document_json), null, 2);

        // Populate orders list
        const ordersListEl = document.getElementById('kizSigningOrdersList');
        ordersListEl.innerHTML = prepRes.order_ids.map((oid, idx) => {
            const code = prepRes.kiz_codes[idx] || '';
            return `<div style="display:flex; justify-content:space-between; align-items:center; padding:4px 0; border-bottom:1px solid rgba(255,255,255,0.05);">
                <div><strong>#${oid}</strong></div>
                <div style="font-family:monospace; font-size:11px; color:var(--text-muted);">${code.substring(0, 24)}...</div>
            </div>`;
        }).join('');

    } catch (e) {
        document.getElementById('kizSigningJsonPreview').innerText = `Ошибка подготовки документа: ${e.message}`;
        showToast('Ошибка подготовки', e.message, 'error');
    }
}

async function openBatchKizSigningModal(action = 'WITHDRAWAL') {
    if (!currentSellerId) return showToast('Ошибка', 'Выберите продавца', 'error');

    try {
        const data = await apiFetch(`/sellers/${currentSellerId}/orders`);
        const orders = data.items || [];
        let eligible = [];

        if (action === 'WITHDRAWAL') {
            eligible = orders.filter(o => o.kiz_code && o.kiz_status === 'ATTACHED');
        } else {
            eligible = orders.filter(o => o.kiz_code && (o.status === 'CANCELLED' || o.kiz_status === 'WITHDRAWN'));
        }

        if (eligible.length === 0) {
            const msg = action === 'WITHDRAWAL' ? 'Нет заказов с прикрепленным КИЗ, готовых к выводу из оборота' : 'Нет отмененных заказов для возврата КИЗ в оборот';
            return showToast('Информация', msg, 'info');
        }

        openKizSigningModal(eligible.map(o => o.id), action);
    } catch (e) {
        showToast('Ошибка', 'Не удалось загрузить список заказов: ' + e.message, 'error');
    }
}

async function executeKizClientSigning() {
    if (!currentSigningPayload || !currentSigningPayload.prepared) {
        return showToast('Ошибка', 'Документ еще не подготовлен', 'error');
    }

    const select = document.getElementById('kizSigningCertSelect');
    const thumbprint = select.value;
    const btn = document.getElementById('kizSigningSubmitBtn');
    const logsEl = document.getElementById('kizSigningLogs');
    const statusText = document.getElementById('kizSigningStatusText');

    logsEl.style.display = 'block';
    btn.classList.add('loading');
    statusText.innerHTML = '⏳ 1/3: Подписание документа в плагине КриптоПро...';

    let signature = '';
    try {
        if (isCryptoProAvailable && window.cadesplugin) {
            signature = await signDataWithCryptoPro(currentSigningPayload.prepared.document_base64, thumbprint);
            statusText.innerHTML = '✅ 1/3: Документ успешно подписан локальной ЭЦП.<br>⏳ 2/3: Отправка подписанного пакета в ГИС МТ (Честный Знак)...';
        } else {
            throw new Error("КриптоПро плагин не обнаружен в этом браузере. Вставьте токен с ЭЦП или нажмите 'Серверная отправка'");
        }

        const submitRes = await apiFetch(`/sellers/${currentSellerId}/kiz/submit-signed-document`, {
            method: 'POST',
            body: JSON.stringify({
                document_type: currentSigningPayload.prepared.document_type,
                document_base64: currentSigningPayload.prepared.document_base64,
                signature_base64: signature,
                order_ids: currentSigningPayload.order_ids,
                action: currentSigningPayload.action
            })
        });

        statusText.innerHTML = `🎉 <strong>Успех!</strong> Документ принят ГИС МТ.<br>ID документа: <code>${submitRes.doc_id}</code><br>Статусы заказов и КИЗ обновлены.`;
        showToast('Честный Знак', submitRes.message, 'success');

        setTimeout(() => {
            closeModal('kizSigningModal');
            loadOrders();
            loadDashboard();
        }, 1500);

    } catch (err) {
        statusText.innerHTML = `❌ <span style="color:var(--status-cancelled)">Ошибка: ${err.message}</span>`;
        showToast('Ошибка подписания / отправки', err.message, 'error');
    } finally {
        btn.classList.remove('loading');
    }
}

async function signDataWithCryptoPro(base64Data, thumbprint) {
    if (!window.cadesplugin) throw new Error("КриптоПро ЭЦП Browser Plug-in не доступен");

    const oStore = await window.cadesplugin.CreateObjectAsync("CAdESCOM.Store");
    await oStore.Open(window.cadesplugin.CAPICOM_CURRENT_USER_STORE, window.cadesplugin.CAPICOM_MY_STORE, window.cadesplugin.CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED);
    const certs = await oStore.Certificates;
    const count = await certs.Count;

    if (count === 0) {
        await oStore.Close();
        throw new Error("В личном хранилище сертификатов не найдено ни одного сертификата УКЭП");
    }

    let selectedCert = null;

    // 1. If explicit thumbprint passed
    if (thumbprint) {
        const found = await certs.Find(window.cadesplugin.CAPICOM_CERTIFICATE_FIND_SHA1_HASH, thumbprint);
        if (await found.Count > 0) {
            selectedCert = await found.Item(1);
        }
    }

    // 2. If no thumbprint, check selected cert in UI or matching seller INN
    if (!selectedCert) {
        const uiCertSelect = document.getElementById('kizSigningCertSelect') || document.getElementById('seller_cert_select');
        const selectedVal = uiCertSelect ? uiCertSelect.value : null;
        if (selectedVal) {
            const found = await certs.Find(window.cadesplugin.CAPICOM_CERTIFICATE_FIND_SHA1_HASH, selectedVal);
            if (await found.Count > 0) {
                selectedCert = await found.Item(1);
            }
        }
    }

    // 3. Fallback: match by current seller INN if known
    if (!selectedCert && typeof currentSellersList !== 'undefined' && currentSellerId) {
        const seller = currentSellersList.find(s => String(s.id) === String(currentSellerId));
        if (seller && seller.cz_inn) {
            for (let i = 1; i <= count; i++) {
                const c = await certs.Item(i);
                const sub = await c.SubjectName;
                if (sub && sub.includes(seller.cz_inn)) {
                    selectedCert = c;
                    break;
                }
            }
        }
    }

    // 4. Default to first cert
    if (!selectedCert && count > 0) {
        selectedCert = await certs.Item(1);
    }

    if (!selectedCert) {
        await oStore.Close();
        throw new Error("Не выбран действующий сертификат для подписания");
    }

    const oSigner = await window.cadesplugin.CreateObjectAsync("CAdESCOM.CPSigner");
    await oSigner.propset_Certificate(selectedCert);
    await oSigner.propset_CheckCertificate(false);

    const oSignedData = await window.cadesplugin.CreateObjectAsync("CAdESCOM.CadesSignedData");
    await oSignedData.propset_ContentEncoding(window.cadesplugin.CADESCOM_BASE64_TO_BINARY);
    await oSignedData.propset_Content(base64Data);

    // Detached CAdES-BES signature
    const signature = await oSignedData.SignCades(oSigner, window.cadesplugin.CADESCOM_CADES_BES, true);
    await oStore.Close();
    return signature;
}

// Global aliases for CAdES signing
window.signDataWithCryptoPro = signDataWithCryptoPro;
window.signBase64WithCades = signDataWithCryptoPro;

async function submitKizViaServerFallback() {
    if (!currentSigningPayload) return;
    const statusText = document.getElementById('kizSigningStatusText');
    const logsEl = document.getElementById('kizSigningLogs');

    logsEl.style.display = 'block';
    statusText.innerHTML = '⏳ Отправка команды фоновому серверному агенту Celery...';

    try {
        const endpoint = currentSigningPayload.action === 'WITHDRAWAL' ? '/kiz/withdraw' : '/kiz/return';
        const res = await apiFetch(`/sellers/${currentSellerId}${endpoint}`, {
            method: 'POST',
            body: JSON.stringify({
                order_ids: currentSigningPayload.order_ids
            })
        });
        statusText.innerHTML = `✅ ${res.message}`;
        showToast('Фоновый агент', res.message, 'success');
        setTimeout(() => {
            closeModal('kizSigningModal');
            loadOrders();
        }, 1200);
    } catch (e) {
        statusText.innerHTML = `❌ Ошибка: ${e.message}`;
        showToast('Ошибка фонового агента', e.message, 'error');
    }
}


/** --- WB Archive Processing (.xlsx) --- */

let currentArchiveData = null;

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
                <td><span class="badge ${w.db_status === 'DELIVERED' ? 'badge-delivered' : 'badge-neutral'}">${w.db_status}</span></td>
                <td><span class="badge ${w.needs_withdrawal ? 'badge-warning' : 'badge-delivered'}">${w.needs_withdrawal ? '⚠️ Требует выбытия' : '✅ Выведен'}</span></td>
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
                </td>
                <td><span class="badge ${r.needs_cz_return ? 'badge-warning' : 'badge-neutral'}">${r.action_recommended}</span></td>
                <td><span class="badge ${r.db_status === 'CANCELLED' ? 'badge-cancelled' : 'badge-neutral'}">${r.db_status}</span></td>
                <td>${r.db_cz_status ? `<span class="badge badge-info">${r.db_cz_status}</span>` : '<span style="color:var(--text-muted)">-</span>'}</td>
            </tr>
        `).join('');
    } else {
        returnsBody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 20px;">Нет данных о возвратах</td></tr>';
    }

    switchArchiveTab('sales');
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

