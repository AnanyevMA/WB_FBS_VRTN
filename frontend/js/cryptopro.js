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

async function populateCertificatesDropdown(customTargetId = null) {
    const select = customTargetId ? document.getElementById(customTargetId) : document.getElementById('kizSigningCertSelect');
    const sellerCertSelect = document.getElementById('seller_cert_select');
    
    if (select) select.innerHTML = '<option value="">-- Поиск сертификатов в хранилище... --</option>';
    if (sellerCertSelect && !customTargetId) sellerCertSelect.innerHTML = '<option value="">-- Поиск сертификатов в хранилище... --</option>';

    if (!window.cadesplugin) {
        if (select) select.innerHTML = '<option value="">-- КриптоПро плагин не обнаружен (доступна серверная отправка) --</option>';
        if (sellerCertSelect && !customTargetId) sellerCertSelect.innerHTML = '<option value="">-- КриптоПро плагин не обнаружен --</option>';
        return;
    }

    try {
        const oStore = await window.cadesplugin.CreateObjectAsync("CAdESCOM.Store");
        await oStore.Open(window.cadesplugin.CAPICOM_CURRENT_USER_STORE, window.cadesplugin.CAPICOM_MY_STORE, window.cadesplugin.CAPICOM_STORE_OPEN_MAXIMUM_ALLOWED);
        const certs = await oStore.Certificates;
        const count = await certs.Count;
        cryptoProCerts = [];

        if (select) select.innerHTML = '<option value="">-- Выберите сертификат для подписания --</option>';
        if (sellerCertSelect && !customTargetId) sellerCertSelect.innerHTML = '<option value="">-- Выберите сертификат УКЭП для магазина --</option>';

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

                if (sellerCertSelect && !customTargetId) {
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
            if (sellerCertSelect && !customTargetId) sellerCertSelect.innerHTML = '<option value="">-- Сертификаты не найдены --</option>';
        } else {
            if (select && select.options.length > 1) {
                select.selectedIndex = 1;
                if (!customTargetId) onCertSelected();
            }
        }
    } catch (e) {
        console.error("Error reading certs from store:", e);
        if (select) select.innerHTML = `<option value="">-- Ошибка чтения сертификатов: ${e.message} --</option>`;
        if (sellerCertSelect && !customTargetId) sellerCertSelect.innerHTML = `<option value="">-- Ошибка чтения: ${e.message} --</option>`;
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

// Global window bindings
window.initCryptoProPlugin = initCryptoProPlugin;
window.checkPluginLoaded = checkPluginLoaded;
window.populateCertificatesDropdown = populateCertificatesDropdown;
window.onCertSelected = onCertSelected;
window.signDataWithCryptoPro = signDataWithCryptoPro;
window.signBase64WithCades = signDataWithCryptoPro;
