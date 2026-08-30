/**
 * WB FBS Manager — CryptoPro CAdES Browser Integration
 * Подписание документов УКЭП (КриптоПро) в браузере.
 *
 * NOTE: isCryptoProAvailable and cryptoProCerts are declared in state.js (loaded first).
 * Do NOT redeclare them here with let/const — it causes SyntaxError and kills this entire file.
 */

/**
 * Update visual plugin badges across the app
 */
function updatePluginBadges(status, text) {
    const badges = ['sellerCertPluginBadge', 'kizSigningPluginBadge', 'batchSigningPluginBadge'];
    badges.forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        if (status === 'success') {
            el.style.background = 'rgba(16, 185, 129, 0.15)';
            el.style.color = '#10b981';
            el.style.borderColor = 'rgba(16, 185, 129, 0.3)';
            el.innerText = text || '🟢 КриптоПро активен';
        } else if (status === 'warning') {
            el.style.background = 'rgba(245, 158, 11, 0.15)';
            el.style.color = '#f59e0b';
            el.style.borderColor = 'rgba(245, 158, 11, 0.3)';
            el.innerText = text || '⚠️ Нет сертификатов';
        } else if (status === 'loading') {
            el.style.background = 'rgba(59, 130, 246, 0.15)';
            el.style.color = '#60a5fa';
            el.style.borderColor = 'rgba(59, 130, 246, 0.3)';
            el.innerText = text || '⏳ Подключение...';
        } else {
            el.style.background = 'rgba(239, 68, 68, 0.15)';
            el.style.color = '#f87171';
            el.style.borderColor = 'rgba(239, 68, 68, 0.3)';
            el.innerText = text || '❌ Плагин не найден';
        }
    });
}

/**
 * Initialize CryptoPro extension
 */
async function initCryptoProPlugin() {
    try {
        if (window.cadesplugin) {
            await checkPluginLoaded();
        } else {
            let tries = 0;
            const interval = setInterval(async () => {
                tries++;
                if (window.cadesplugin || tries > 30) {
                    clearInterval(interval);
                    await checkPluginLoaded();
                }
            }, 100);
        }
    } catch (e) {
        console.debug("CryptoPro init note:", e);
    }
}

async function checkPluginLoaded() {
    updatePluginBadges('loading', '⏳ Сканирование ЭЦП...');
    if (!window.cadesplugin) {
        isCryptoProAvailable = false;
        updatePluginBadges('error', '❌ Плагин не найден');
        return false;
    }
    try {
        await window.cadesplugin;
        const certs = await loadCryptoProCerts();
        if (certs && certs.length > 0) {
            isCryptoProAvailable = true;
            updatePluginBadges('success', `🟢 ЭЦП готова (${certs.length})`);
            return true;
        } else {
            isCryptoProAvailable = true;
            updatePluginBadges('warning', '⚠️ Хранилище пусто');
            return false;
        }
    } catch (err) {
        isCryptoProAvailable = false;
        updatePluginBadges('error', '❌ Ошибка плагина');
        return false;
    }
}

/**
 * Load certificates from CryptoPro store (CurrentUser\My)
 */
async function loadCryptoProCerts() {
    const results = [];

    if (!window.cadesplugin) {
        console.warn("window.cadesplugin is not defined");
        return results;
    }

    try {
        await window.cadesplugin;

        const CURRENT_USER = (window.cadesplugin.CAPICOM_CURRENT_USER_STORE !== undefined) ? window.cadesplugin.CAPICOM_CURRENT_USER_STORE : 2;
        const MY_STORE = (window.cadesplugin.CAPICOM_MY_STORE !== undefined) ? window.cadesplugin.CAPICOM_MY_STORE : "My";
        const STORE_OPEN_MAX = (window.cadesplugin.CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED !== undefined) ? window.cadesplugin.CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED : 2;

        const oStore = await window.cadesplugin.CreateObjectAsync("CAdESCOM.Store");
        await oStore.Open(CURRENT_USER, MY_STORE, STORE_OPEN_MAX);
        const certs = await oStore.Certificates;
        const count = await certs.Count;

        for (let i = 1; i <= count; i++) {
            try {
                const cert = await certs.Item(i);
                let subjectName = "";
                let validTo = "";
                let validFrom = "";
                let thumbprint = "";

                try { subjectName = await cert.SubjectName; } catch (e) {}
                if (!subjectName) { try { subjectName = await cert.get_SubjectName(); } catch (e) {} }

                try { validTo = await cert.ValidToDate; } catch (e) {}
                if (!validTo) { try { validTo = await cert.get_ValidToDate(); } catch (e) {} }

                try { validFrom = await cert.ValidFromDate; } catch (e) {}
                if (!validFrom) { try { validFrom = await cert.get_ValidFromDate(); } catch (e) {} }

                try { thumbprint = await cert.Thumbprint; } catch (e) {}
                if (!thumbprint) { try { thumbprint = await cert.get_Thumbprint(); } catch (e) {} }

                if (!thumbprint) continue;

                let cn = subjectName;
                const cnMatch = subjectName.match(/CN=([^,]+)/);
                if (cnMatch) cn = cnMatch[1];
                let inn = '';
                const innMatch = subjectName.match(/ИНН=([^,]+)/) || subjectName.match(/ИНН ЮЛ=([^,]+)/) || subjectName.match(/INN=([^,]+)/);
                if (innMatch) inn = innMatch[1];

                const isExpired = validTo ? (new Date(validTo).getTime() < Date.now()) : false;

                results.push({
                    thumbprint: thumbprint.toUpperCase(),
                    subject: cn || 'Сертификат УКЭП',
                    inn: inn,
                    validTo: validTo,
                    validFrom: validFrom,
                    rawSubject: subjectName,
                    isExpired: isExpired
                });
            } catch (ce) {
                console.debug("Error inspecting cert item:", ce);
            }
        }
        await oStore.Close();
    } catch (e) {
        console.warn("Direct cadesplugin store read note:", e);
    }

    cryptoProCerts = results;
    return results;
}

/**
 * Populate UI select elements with available certificates
 */
async function populateCertificatesDropdown(customTargetId = null, userTriggered = false) {
    const select = customTargetId ? document.getElementById(customTargetId) : document.getElementById('kizSigningCertSelect');
    const sellerCertSelect = document.getElementById('seller_cert_select');
    const batchCertSelect = document.getElementById('batchCertSelect');

    const updateOptions = (html) => {
        if (select) select.innerHTML = html;
        if (sellerCertSelect && !customTargetId) sellerCertSelect.innerHTML = html;
        if (batchCertSelect && !customTargetId) batchCertSelect.innerHTML = html;
    };

    updateOptions('<option value="">⏳ Поиск сертификатов в хранилище...</option>');
    updatePluginBadges('loading', '⏳ Сканирование...');

    try {
        const certs = await loadCryptoProCerts();

        if (!certs || certs.length === 0) {
            updatePluginBadges('warning', '⚠️ Нет сертификатов');
            updateOptions('<option value="">-- В хранилище "Личные" нет сертификатов --</option>');
            if (userTriggered) {
                showToast('КриптоПро', 'В личном хранилище сертификатов не найдено. Проверьте подключение токена (Рутокен/USB).', 'warning');
            }
            return;
        }

        updatePluginBadges('success', `🟢 Найдено: ${certs.length}`);
        
        const defaultOption = '<option value="">-- Выберите сертификат УКЭП --</option>';
        const optionsHtml = defaultOption + certs.map(cert => {
            const dateStr = cert.validTo ? new Date(cert.validTo).toLocaleDateString('ru-RU') : '';
            const expLabel = cert.isExpired ? ` (истёк ${dateStr})` : ` — до ${dateStr}`;
            return `<option value="${cert.thumbprint}">${cert.subject} ${cert.inn ? `(ИНН: ${cert.inn})` : ''}${expLabel}</option>`;
        }).join('');

        updateOptions(optionsHtml);

        // Auto-select match for active seller if configured
        if (sellerCertSelect && !customTargetId) {
            const certInput = document.getElementById('seller_cert_path');
            const targetThumb = certInput ? certInput.value.trim().toUpperCase() : '';
            if (targetThumb) {
                const match = Array.from(sellerCertSelect.options).find(o => o.value && o.value.toUpperCase() === targetThumb);
                if (match) {
                    sellerCertSelect.value = match.value;
                    if (typeof onSellerCertChanged === 'function') onSellerCertChanged();
                }
            }
        }

        if (select && select.options.length > 1 && !select.value) {
            select.selectedIndex = 1;
            if (typeof onCertSelected === 'function') onCertSelected();
        }

        if (userTriggered) {
            showToast('КриптоПро', `Обновлено! Доступно сертификатов: ${certs.length}`, 'success');
        }
    } catch (err) {
        console.error("populateCertificatesDropdown error:", err);
        updatePluginBadges('error', '❌ Ошибка плагина');
        updateOptions(`<option value="">-- Ошибка: ${err.message || 'Плагин не отвечает'} --</option>`);
        if (userTriggered) {
            showToast('Ошибка КриптоПро', err.message || 'Не удалось связаться с плагином', 'error');
        }
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
    const cert = cryptoProCerts.find(c => c.thumbprint && c.thumbprint.toLowerCase() === thumb.toLowerCase());
    if (cert && detailsEl) {
        detailsEl.innerHTML = `<strong>Владелец:</strong> ${cert.subject}<br><strong>Действителен до:</strong> ${new Date(cert.validTo).toLocaleString('ru-RU')}<br><span style="font-family:monospace; font-size:11px;">Отпечаток (SHA-1): ${cert.thumbprint}</span>`;
    }
}

/**
 * Sign base64 data using CryptoPro (detached signature)
 */
async function signDataWithCryptoPro(base64Data, thumbprint) {
    let targetThumb = thumbprint;

    // If no thumbprint passed, pick from UI or first available
    if (!targetThumb) {
        const uiCertSelect = document.getElementById('kizSigningCertSelect') || document.getElementById('seller_cert_select');
        targetThumb = uiCertSelect ? uiCertSelect.value : null;
    }

    if (!targetThumb && cryptoProCerts.length > 0) {
        targetThumb = cryptoProCerts[0].thumbprint;
    }

    if (!targetThumb) {
        await loadCryptoProCerts();
        if (cryptoProCerts.length > 0) {
            targetThumb = cryptoProCerts[0].thumbprint;
        }
    }

    if (!targetThumb) {
        throw new Error("Не выбран сертификат УКЭП для подписания");
    }

    // Direct CAdES plugin
    if (window.cadesplugin) {
        await window.cadesplugin;
        const CURRENT_USER = (window.cadesplugin.CAPICOM_CURRENT_USER_STORE !== undefined) ? window.cadesplugin.CAPICOM_CURRENT_USER_STORE : 2;
        const MY_STORE = (window.cadesplugin.CAPICOM_MY_STORE !== undefined) ? window.cadesplugin.CAPICOM_MY_STORE : "My";
        const STORE_OPEN_MAX = (window.cadesplugin.CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED !== undefined) ? window.cadesplugin.CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED : 2;

        const oStore = await window.cadesplugin.CreateObjectAsync("CAdESCOM.Store");
        await oStore.Open(CURRENT_USER, MY_STORE, STORE_OPEN_MAX);
        const certs = await oStore.Certificates;
        const found = await certs.Find(window.cadesplugin.CAPICOM_CERTIFICATE_FIND_SHA1_HASH, targetThumb);
        if ((await found.Count) === 0) {
            await oStore.Close();
            throw new Error(`Сертификат с отпечатком ${targetThumb} не найден в хранилище`);
        }
        const cert = await found.Item(1);

        const oSigner = await window.cadesplugin.CreateObjectAsync("CAdESCOM.CPSigner");
        await oSigner.propset_Certificate(cert);
        await oSigner.propset_CheckCertificate(false);

        const oSignedData = await window.cadesplugin.CreateObjectAsync("CAdESCOM.CadesSignedData");
        await oSignedData.propset_ContentEncoding(window.cadesplugin.CADESCOM_BASE64_TO_BINARY);
        await oSignedData.propset_Content(base64Data);

        const signature = await oSignedData.SignCades(oSigner, window.cadesplugin.CADESCOM_CADES_BES, true);
        await oStore.Close();
        return signature;
    }

    throw new Error("Плагин КриптоПро недоступен для создания подписи");
}

let isSilentCzRefreshRunning = false;
let lastSilentCzCheckTimestamp = 0;

/**
 * Бесшовное авто-продление токена Честного Знака в фоновом режиме через плагин КриптоПро.
 */
async function silentCheckAndRefreshCzToken() {
    const now = Date.now();
    if (isSilentCzRefreshRunning || (now - lastSilentCzCheckTimestamp < 3 * 60 * 1000)) {
        return;
    }
    if (!currentSellerId) {
        return;
    }

    try {
        isSilentCzRefreshRunning = true;
        lastSilentCzCheckTimestamp = now;

        const status = await apiFetch(`/sellers/${currentSellerId}/cz-token-status`);
        if (!status || !status.needs_refresh || !status.cz_inn) {
            return;
        }

        console.log(`[Auto-Refresh CZ] Seller ${currentSellerId} token needs refresh (age: ${status.age_seconds || 'n/a'}s). Silently requesting challenge...`);

        // 1. Получаем challenge
        const challenge = await apiFetch(`/sellers/${currentSellerId}/cz-challenge`);
        if (!challenge || !challenge.uuid || !challenge.data) {
            return;
        }

        // 2. Находим thumbprint
        let targetThumb = status.thumbprint;
        if (!targetThumb || !cryptoProCerts.some(c => c.thumbprint.toLowerCase() === targetThumb.toLowerCase())) {
            await loadCryptoProCerts();
            const matchByInn = cryptoProCerts.find(c => c.inn && c.inn === status.cz_inn);
            if (matchByInn) {
                targetThumb = matchByInn.thumbprint;
            } else if (cryptoProCerts.length > 0) {
                targetThumb = cryptoProCerts[0].thumbprint;
            }
        }

        if (!targetThumb) {
            return;
        }

        // 3. Создаем присоединенную подпись
        let signature = null;
        if (window.cryptoPro && typeof window.cryptoPro.createAttachedSignature === 'function') {
            try {
                signature = await window.cryptoPro.createAttachedSignature(targetThumb, challenge.data);
            } catch (e) {
                console.debug("cryptoPro.createAttachedSignature fallback:", e);
            }
        }

        if (!signature && window.cadesplugin) {
            await window.cadesplugin;
            const oStore = await window.cadesplugin.CreateObjectAsync("CAdESCOM.Store");
            await oStore.Open(2, "My", 2);
            const certs = await oStore.Certificates;
            const found = await certs.Find(window.cadesplugin.CAPICOM_CERTIFICATE_FIND_SHA1_HASH, targetThumb);
            if ((await found.Count) > 0) {
                const cert = await found.Item(1);
                const oSigner = await window.cadesplugin.CreateObjectAsync("CAdESCOM.CPSigner");
                await oSigner.propset_Certificate(cert);

                const oSignedData = await window.cadesplugin.CreateObjectAsync("CAdESCOM.CadesSignedData");
                await oSignedData.propset_Content(challenge.data);

                signature = await oSignedData.SignCades(oSigner, window.cadesplugin.CADESCOM_CADES_BES, false);
            }
            await oStore.Close();
        }

        if (!signature) return;

        // 4. Отправляем на сервер
        const signinRes = await apiFetch(`/sellers/${currentSellerId}/cz-signin`, {
            method: 'POST',
            body: JSON.stringify({
                uuid: challenge.uuid,
                data: signature
            })
        });

        console.log(`[Auto-Refresh CZ] ✅ Token for seller ${currentSellerId} refreshed silently:`, signinRes.token_preview || 'OK');

        const czStatusEl = document.getElementById('seller_cz_token_status');
        if (czStatusEl) {
            czStatusEl.innerHTML = `<span style="color:var(--status-delivered); font-weight:600;">✅ Токен активен в БД (${signinRes.token_preview || 'обновлен в фоне'})</span>`;
        }
    } catch (e) {
        console.debug("[Auto-Refresh CZ] Silent refresh note:", e.message || e);
    } finally {
        isSilentCzRefreshRunning = false;
    }
}

// Global window bindings
window.initCryptoProPlugin = initCryptoProPlugin;
window.checkPluginLoaded = checkPluginLoaded;
window.loadCryptoProCerts = loadCryptoProCerts;
window.populateCertificatesDropdown = populateCertificatesDropdown;
window.onCertSelected = onCertSelected;
window.signDataWithCryptoPro = signDataWithCryptoPro;
window.signBase64WithCades = signDataWithCryptoPro;
window.silentCheckAndRefreshCzToken = silentCheckAndRefreshCzToken;


