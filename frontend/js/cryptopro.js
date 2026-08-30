/**
 * WB FBS Manager — CryptoPro CAdES Browser Plugin Integration
 * Подписание документов УКЭП (КриптоПро) в браузере.
 */

let isCryptoProAvailable = false;
let cryptoProCerts = [];

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
        let cades = window.cadesplugin;
        if (cades && typeof cades.then === 'function') {
            cades = await cades;
        }
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

function renderCertsToSelectElements(customTargetId = null) {
    const selectElements = [];
    if (customTargetId) {
        const el = document.getElementById(customTargetId);
        if (el) selectElements.push(el);
    }
    const defaultSelects = ['seller_cert_select', 'kizSigningCertSelect', 'batchCertSelect'];
    defaultSelects.forEach(id => {
        const el = document.getElementById(id);
        if (el && !selectElements.includes(el)) selectElements.push(el);
    });

    selectElements.forEach(sel => {
        const currentVal = sel.value;
        sel.innerHTML = '<option value="">-- Выберите сертификат УКЭП --</option>';
        if (!cryptoProCerts || cryptoProCerts.length === 0) {
            sel.innerHTML = '<option value="">-- В хранилище "Личные" нет сертификатов --</option>';
            return;
        }
        cryptoProCerts.forEach(cert => {
            const opt = document.createElement('option');
            opt.value = cert.thumbprint;
            opt.textContent = `${cert.subject} ${cert.inn ? `(ИНН: ${cert.inn})` : ''} — до ${new Date(cert.validTo).toLocaleDateString('ru-RU')}`;
            sel.appendChild(opt);
        });
        if (currentVal) {
            const match = Array.from(sel.options).find(o => o.value && o.value.toLowerCase() === currentVal.toLowerCase());
            if (match) sel.value = match.value;
        }
    });
}

async function populateCertificatesDropdown(customTargetId = null) {
    if (cryptoProCerts && cryptoProCerts.length > 0) {
        renderCertsToSelectElements(customTargetId);
    }

    if (!window.cadesplugin) {
        if (typeof loadCadesScript === 'function') loadCadesScript();
        await new Promise(r => setTimeout(r, 400));
    }

    if (!window.cadesplugin) {
        const defaultSelects = ['seller_cert_select', 'kizSigningCertSelect', 'batchCertSelect'];
        defaultSelects.forEach(id => {
            const el = document.getElementById(id);
            if (el && (!cryptoProCerts || cryptoProCerts.length === 0)) {
                el.innerHTML = '<option value="">-- КриптоПро плагин не обнаружен в браузере --</option>';
            }
        });
        return;
    }

    try {
        let cades = window.cadesplugin;
        if (cades && typeof cades.then === 'function') {
            cades = await cades;
        }
        if (!cades) return;

        const CURRENT_USER = (cades.CAPICOM_CURRENT_USER_STORE !== undefined) ? cades.CAPICOM_CURRENT_USER_STORE : 2;
        const MY_STORE = (cades.CAPICOM_MY_STORE !== undefined) ? cades.CAPICOM_MY_STORE : "My";
        const STORE_OPEN_MAX = (cades.CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED !== undefined) ? cades.CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED : 2;

        const oStore = await cades.CreateObjectAsync("CAdESCOM.Store");
        await oStore.Open(CURRENT_USER, MY_STORE, STORE_OPEN_MAX);
        const certs = await oStore.Certificates;
        const count = await certs.Count;
        const loadedCerts = [];

        for (let i = 1; i <= count; i++) {
            try {
                const cert = await certs.Item(i);
                let subjectName = "";
                let validTo = "";
                let thumbprint = "";
                let serial = "";

                try { subjectName = typeof cert.SubjectName !== 'undefined' ? await cert.SubjectName : (typeof cert.get_SubjectName === 'function' ? await cert.get_SubjectName() : ''); } catch (e) {}
                try { validTo = typeof cert.ValidToDate !== 'undefined' ? await cert.ValidToDate : (typeof cert.get_ValidToDate === 'function' ? await cert.get_ValidToDate() : ''); } catch (e) {}
                try { thumbprint = typeof cert.Thumbprint !== 'undefined' ? await cert.Thumbprint : (typeof cert.get_Thumbprint === 'function' ? await cert.get_Thumbprint() : ''); } catch (e) {}
                try { serial = typeof cert.SerialNumber !== 'undefined' ? await cert.SerialNumber : (typeof cert.get_SerialNumber === 'function' ? await cert.get_SerialNumber() : ''); } catch (e) {}

                if (!thumbprint) continue;

                let cn = subjectName;
                const cnMatch = subjectName.match(/CN=([^,]+)/);
                if (cnMatch) cn = cnMatch[1];
                let inn = '';
                const innMatch = subjectName.match(/ИНН=([^,]+)/) || subjectName.match(/ИНН ЮЛ=([^,]+)/) || subjectName.match(/INN=([^,]+)/);
                if (innMatch) inn = innMatch[1];

                loadedCerts.push({
                    thumbprint: thumbprint,
                    subject: cn,
                    inn: inn,
                    validTo: validTo,
                    serial: serial,
                    rawSubject: subjectName
                });
            } catch (ce) {
                console.log("Error reading cert item:", ce);
            }
        }
        await oStore.Close();
        cryptoProCerts = loadedCerts;
        renderCertsToSelectElements(customTargetId);

        // Auto select callback if any
        if (!customTargetId && typeof onCertSelected === 'function') {
            onCertSelected();
        }
    } catch (e) {
        console.error("Error reading certs from store:", e);
        const defaultSelects = ['seller_cert_select', 'kizSigningCertSelect', 'batchCertSelect'];
        defaultSelects.forEach(id => {
            const el = document.getElementById(id);
            if (el && (!cryptoProCerts || cryptoProCerts.length === 0)) {
                el.innerHTML = `<option value="">-- Ошибка чтения сертификатов: ${e.message} --</option>`;
            }
        });
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

let isSilentCzRefreshRunning = false;
let lastSilentCzCheckTimestamp = 0;

/**
 * Бесшовное авто-продление токена Честного Знака в фоновом режиме через плагин КриптоПро.
 * Проверяет срок действия токена текущего продавца и при необходимости продлевает его без блокировки интерфейса.
 */
async function silentCheckAndRefreshCzToken() {
    const now = Date.now();
    // Защита от параллельных запусков и слишком частых проверок (не чаще 1 раза в 3 минуты)
    if (isSilentCzRefreshRunning || (now - lastSilentCzCheckTimestamp < 3 * 60 * 1000)) {
        return;
    }
    if (!currentSellerId || !window.cadesplugin) {
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

        // 1. Получаем строку аутентификации от ГИС МТ
        const challenge = await apiFetch(`/sellers/${currentSellerId}/cz-challenge`);
        if (!challenge || !challenge.uuid || !challenge.data) {
            return;
        }

        // 2. Инициализируем cadesplugin
        let cades = window.cadesplugin;
        if (cades && typeof cades.then === 'function') {
            cades = await cades;
        }
        if (!cades) return;

        const CURRENT_USER = (cades.CAPICOM_CURRENT_USER_STORE !== undefined) ? cades.CAPICOM_CURRENT_USER_STORE : 2;
        const MY_STORE = (cades.CAPICOM_MY_STORE !== undefined) ? cades.CAPICOM_MY_STORE : "My";
        const STORE_OPEN_MAX = (cades.CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED !== undefined) ? cades.CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED : 2;

        const oStore = await cades.CreateObjectAsync("CAdESCOM.Store");
        await oStore.Open(CURRENT_USER, MY_STORE, STORE_OPEN_MAX);
        const certs = await oStore.Certificates;
        const count = await certs.Count;

        if (count === 0) {
            await oStore.Close();
            return;
        }

        let selectedCert = null;
        const targetThumb = status.thumbprint ? status.thumbprint.toLowerCase() : null;
        const inn = status.cz_inn;

        for (let i = 1; i <= count; i++) {
            try {
                const c = await certs.Item(i);
                let thumb = "";
                let sub = "";
                try { thumb = typeof c.Thumbprint !== 'undefined' ? await c.Thumbprint : (typeof c.get_Thumbprint === 'function' ? await c.get_Thumbprint() : ''); } catch (e) {}
                try { sub = typeof c.SubjectName !== 'undefined' ? await c.SubjectName : (typeof c.get_SubjectName === 'function' ? await c.get_SubjectName() : ''); } catch (e) {}

                if (targetThumb && thumb && thumb.toLowerCase() === targetThumb) {
                    selectedCert = c;
                    break;
                }
                if (inn && sub && sub.includes(inn)) {
                    selectedCert = c;
                    break;
                }
            } catch (e) {}
        }

        if (!selectedCert && count > 0) {
            selectedCert = await certs.Item(1);
        }

        if (!selectedCert) {
            await oStore.Close();
            return;
        }

        // 3. Создаем присоединенную подпись строки challenge
        const oSigner = await cades.CreateObjectAsync("CAdESCOM.CPSigner");
        await oSigner.propset_Certificate(selectedCert);

        const oSignedData = await cades.CreateObjectAsync("CAdESCOM.CadesSignedData");
        await oSignedData.propset_Content(challenge.data);

        const signature = await oSignedData.SignCades(oSigner, cades.CADESCOM_CADES_BES, false);
        await oStore.Close();

        // 4. Отправляем подписанный challenge на сервер
        const signinRes = await apiFetch(`/sellers/${currentSellerId}/cz-signin`, {
            method: 'POST',
            body: JSON.stringify({
                uuid: challenge.uuid,
                data: signature
            })
        });

        console.log(`[Auto-Refresh CZ] ✅ Token for seller ${currentSellerId} refreshed silently:`, signinRes.token_preview || 'OK');
        
        // Обновляем плашку статуса в окне настроек, если оно открыто
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
window.populateCertificatesDropdown = populateCertificatesDropdown;
window.onCertSelected = onCertSelected;
window.signDataWithCryptoPro = signDataWithCryptoPro;
window.signBase64WithCades = signDataWithCryptoPro;
window.silentCheckAndRefreshCzToken = silentCheckAndRefreshCzToken;
