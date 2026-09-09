/**
 * WB FBS Manager — Национальный Каталог (НКТ) Честного Знака
 * Управление карточками товаров: добавление, редактирование, проверка статуса, подписание и публикация.
 */

let nkCardsList = [];
let currentEditingCardId = null;
let currentCardToSign = null;

/**
 * Загрузка списка карточек товаров выбранного продавца
 */
async function loadProductCards(showSilent = false) {
    if (!currentSellerId) {
        renderProductCards([]);
        return;
    }

    const tbody = document.getElementById('nk-cards-table-body');
    if (!showSilent && tbody) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; padding: 24px; color: var(--text-muted);">⏳ Загрузка карточек товаров из НКТ...</td></tr>';
    }

    try {
        const data = await apiFetch(`/sellers/${currentSellerId}/national-catalog/products`);
        nkCardsList = Array.isArray(data) ? data : (data.items || []);
        renderProductCards(nkCardsList);
    } catch (err) {
        console.error('Ошибка загрузки карточек НКТ:', err);
        if (tbody) {
            tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding: 24px; color: var(--status-cancelled);">❌ Ошибка: ${escapeHtml(err.message)}</td></tr>`;
        }
        showToast('Ошибка НКТ', err.message, 'error');
    }
}

/**
 * Отрисовка таблицы карточек с учетом фильтров
 */
function renderProductCards(cards) {
    const tbody = document.getElementById('nk-cards-table-body');
    if (!tbody) return;

    const searchInput = document.getElementById('nkSearchInput');
    const query = searchInput ? searchInput.value.trim().toLowerCase() : '';
    const statusFilter = document.getElementById('nkStatusFilter')?.value || 'all';

    let filtered = cards;
    if (statusFilter !== 'all') {
        filtered = filtered.filter(c => (c.status || '').toLowerCase() === statusFilter.toLowerCase());
    }
    if (query) {
        filtered = filtered.filter(c => {
            const name = (c.name || '').toLowerCase();
            const brand = (c.brand || '').toLowerCase();
            const tnved = (c.tnved || '').toLowerCase();
            const gtin = (c.gtin || '').toLowerCase();
            const goodId = String(c.good_id || '');
            return name.includes(query) || brand.includes(query) || tnved.includes(query) || gtin.includes(query) || goodId.includes(query);
        });
    }

    if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding: 24px; color: var(--text-muted);">
            ${cards.length === 0 ? 'Карточек товаров пока нет. Создайте первую карточку или запустите синхронизацию с НКТ.' : 'По заданным фильтрам карточки не найдены.'}
        </td></tr>`;
        return;
    }

    tbody.innerHTML = filtered.map(c => {
        const gtinDisplay = c.gtin ? `<span style="font-family:monospace; font-weight:600; color: #a5b4fc;">${escapeHtml(c.gtin)}</span>` : '<span style="color:var(--text-muted); font-size:12px;">(Не присвоен)</span>';
        const goodIdBadge = c.good_id ? `<span class="badge" style="background:rgba(59,130,246,0.12); color:#60a5fa; font-size:11px; margin-left:6px;">ID: ${c.good_id}</span>` : '';
        const feedIdBadge = c.feed_id ? `<span class="badge" style="background:rgba(148,163,184,0.1); color:#94a3b8; font-size:10px; margin-top:2px;">feed: ${c.feed_id}</span>` : '';
        const brandBadge = c.brand ? `<span style="background:rgba(255,255,255,0.06); padding:2px 6px; border-radius:4px; font-size:11px; margin-right:6px;">${escapeHtml(c.brand)}</span>` : '';
        const tnvedBadge = c.tnved ? `<span style="color:var(--text-muted); font-size:12px;">ТН ВЭД: ${escapeHtml(c.tnved)}</span>` : '';
        const statusBadge = getStatusBadge(c.status, 'nk');
        const updatedDate = c.updated_at ? new Date(c.updated_at).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : '-';

        // Action buttons based on status
        const canSign = c.status === 'notsigned' || c.status === 'moderation';
        const signBtn = canSign ? `
            <button class="btn btn-sm" style="background: linear-gradient(135deg, #7c3aed, #4f46e5); color: white; padding: 4px 8px; font-size: 11px;" onclick="openSignCardModal('${c.id}')" title="Подписать карточку сертификатом ЭЦП и опубликовать">
                🔏 Подписать
            </button>
        ` : '';

        const checkBtn = (c.good_id || c.feed_id) ? `
            <button class="btn btn-secondary btn-sm" style="padding: 4px 8px; font-size: 11px;" onclick="checkCardStatus('${c.id}')" title="Запросить статус модерации / публикации в НКТ">
                🔄 Статус
            </button>
        ` : '';

        const errorsList = Array.isArray(c.error_details) && c.error_details.length > 0 ? `
            <div style="font-size: 11px; color: #f87171; margin-top: 4px; max-width: 260px; line-height: 1.2;">
                ⚠️ ${escapeHtml(c.error_details.map(e => (typeof e === 'object' ? (e.error_message || e.message || JSON.stringify(e)) : String(e))).join('; '))}
            </div>
        ` : '';

        return `
            <tr>
                <td>${gtinDisplay}</td>
                <td>
                    <div style="font-weight: 600; display: flex; align-items: center; flex-wrap: wrap; gap: 4px;">
                        <span>${escapeHtml(c.name || 'Без названия')}</span>
                        ${goodIdBadge}
                    </div>
                    <div style="font-size: 12px; color: var(--text-muted); margin-top: 2px;">
                        ${brandBadge} ${tnvedBadge}
                    </div>
                </td>
                <td>
                    <div style="font-size: 13px;">${escapeHtml(c.category_name || (c.category_id ? 'Категория #' + c.category_id : 'Не указана'))}</div>
                </td>
                <td>
                    <div>${statusBadge}</div>
                    ${feedIdBadge}
                    ${errorsList}
                </td>
                <td style="font-size: 12px; color: var(--text-muted);">${updatedDate}</td>
                <td>
                    <div style="display: flex; gap: 6px; align-items: center; flex-wrap: wrap;">
                        <button class="btn btn-secondary btn-sm" style="padding: 4px 8px; font-size: 11px;" onclick="editProductCard('${c.id}')" title="Редактировать карточку">
                            ✏️ Ред.
                        </button>
                        ${checkBtn}
                        ${signBtn}
                        <button class="btn btn-danger btn-sm" style="padding: 4px 8px; font-size: 11px;" onclick="deleteProductCard('${c.id}', '${escapeHtml(c.name || '')}')" title="Удалить карточку">
                            🗑️
                        </button>
                    </div>
                </td>
            </tr>
        `;
    }).join('');
}

/**
 * Синхронизация статусов карточек с НКТ
 */
async function syncProductCardsFromNK() {
    if (!currentSellerId) {
        showToast('Внимание', 'Сначала выберите продавца', 'warning');
        return;
    }

    const btn = document.getElementById('nkSyncBtn');
    if (btn) btn.classList.add('loading');
    showToast('Синхронизация...', 'Проверка статусов карточек в Национальном Каталоге...', 'info');

    try {
        let updatedCount = 0;
        // Check status for each card that has feed_id or good_id and is not published
        for (const card of nkCardsList) {
            if ((card.feed_id || card.good_id) && card.status !== 'published') {
                try {
                    await apiFetch(`/sellers/${currentSellerId}/national-catalog/products/${card.id}/check-status`, {
                        method: 'POST'
                    });
                    updatedCount++;
                } catch (e) {
                    console.warn(`Card ${card.id} status sync error:`, e);
                }
            }
        }
        showToast('НКТ Синхронизация', `Синхронизация завершена. Проверено карточек: ${updatedCount}`, 'success');
        await loadProductCards(true);
    } catch (err) {
        showToast('Ошибка НКТ', err.message, 'error');
    } finally {
        if (btn) btn.classList.remove('loading');
    }
}

/**
 * Открытие модального окна добавления новой карточки
 */
function openCreateCardModal() {
    if (!currentSellerId) {
        showToast('Внимание', 'Сначала выберите продавца', 'warning');
        return;
    }

    currentEditingCardId = null;
    document.getElementById('nkCardForm').reset();
    document.getElementById('nkCardModalTitle').innerText = 'Создать карточку товара (НКТ)';
    document.getElementById('nk_card_good_id').value = '';
    document.getElementById('nk_custom_attrs_container').innerHTML = '';
    document.getElementById('nkCardModerationCheck').checked = true;

    // Default category ID for Light Industry (Легпром: 20000003)
    const catInput = document.getElementById('nk_card_category_id');
    if (catInput) catInput.value = '20000003';
    const catName = document.getElementById('nk_card_category_name');
    if (catName) catName.value = 'Одежда и текстиль';

    openModal('productCardModal');
}

/**
 * Генерация GTIN через НКТ
 */
async function generateCardGtin() {
    if (!currentSellerId) return showToast('Внимание', 'Сначала выберите продавца', 'warning');
    const gtinInput = document.getElementById('nk_card_gtin');
    const btn = document.getElementById('btnGenGtin');
    if (btn) btn.disabled = true;

    try {
        showToast('Генерация GTIN...', 'Запрос свободного GTIN в Национальном Каталоге (ГС1 РУС)...', 'info');
        const res = await apiFetch(`/sellers/${currentSellerId}/national-catalog/helpers/generate-gtin`);
        if (res.gtin) {
            gtinInput.value = res.gtin;
            showToast('GTIN получен', `Сгенерирован GTIN: ${res.gtin}`, 'success');
        } else {
            showToast('Внимание', 'Не удалось получить GTIN из ответа НКТ', 'warning');
        }
    } catch (err) {
        showToast('Ошибка генерации GTIN', err.message, 'error');
    } finally {
        if (btn) btn.disabled = false;
    }
}

/**
 * Редактирование существующей карточки
 */
async function editProductCard(cardId) {
    if (!currentSellerId) return;
    currentEditingCardId = cardId;
    document.getElementById('nkCardForm').reset();
    document.getElementById('nk_custom_attrs_container').innerHTML = '';

    try {
        const card = await apiFetch(`/sellers/${currentSellerId}/national-catalog/products/${cardId}`);
        document.getElementById('nkCardModalTitle').innerText = `Редактировать карточку: ${card.name || ('#' + card.id)}`;
        document.getElementById('nk_card_good_id').value = card.good_id || '';
        document.getElementById('nk_card_name').value = card.name || '';
        document.getElementById('nk_card_brand').value = card.brand || '';
        document.getElementById('nk_card_gtin').value = card.gtin || '';
        document.getElementById('nk_card_tnved').value = card.tnved || '';
        document.getElementById('nk_card_category_id').value = card.category_id || '20000003';
        document.getElementById('nk_card_category_name').value = card.category_name || '';

        // Extract known attributes
        const attrs = Array.isArray(card.attributes) ? card.attributes : [];
        let composition = '';
        let country = '';
        let color = '';
        let size = '';
        let desc = '';
        let article = '';

        const customAttrs = [];
        attrs.forEach(a => {
            const attrId = a.attr_id;
            const val = a.attr_value || a.value || '';
            if (attrId === 10609 || attrId === '10609') {
                if (!document.getElementById('nk_card_tnved').value) document.getElementById('nk_card_tnved').value = val;
            } else if (attrId === 10610 || attrId === '10610') composition = val;
            else if (attrId === 10611 || attrId === '10611') country = val;
            else if (attrId === 10612 || attrId === '10612') color = val;
            else if (attrId === 10613 || attrId === '10613') size = val;
            else if (attrId === 10614 || attrId === '10614') desc = val;
            else if (attrId === 10001 || attrId === '10001' || attrId === 2478) article = val;
            else {
                customAttrs.push(a);
            }
        });

        document.getElementById('nk_card_article').value = article;
        document.getElementById('nk_card_composition').value = composition;
        document.getElementById('nk_card_country').value = country;
        document.getElementById('nk_card_color').value = color;
        document.getElementById('nk_card_size').value = size;
        document.getElementById('nk_card_description').value = desc;

        // Render custom attributes if any
        customAttrs.forEach(ca => {
            addCustomAttributeRow(ca.attr_id, ca.attr_value || ca.value || '', ca.attr_value_id);
        });

        openModal('productCardModal');
    } catch (err) {
        showToast('Ошибка', 'Не удалось загрузить карточку: ' + err.message, 'error');
    }
}

/**
 * Добавление строки произвольного атрибута
 */
function addCustomAttributeRow(attrId = '', attrValue = '', attrValueId = '') {
    const container = document.getElementById('nk_custom_attrs_container');
    if (!container) return;

    const rowId = 'custom_attr_' + Date.now() + '_' + Math.floor(Math.random() * 1000);
    const div = document.createElement('div');
    div.id = rowId;
    div.style.display = 'grid';
    div.style.gridTemplateColumns = '120px 1fr 100px 36px';
    div.style.gap = '8px';
    div.style.alignItems = 'center';
    div.style.marginBottom = '8px';

    div.innerHTML = `
        <input type="number" class="form-control" placeholder="ID атриб." value="${escapeHtml(String(attrId))}" style="font-size:12px;" data-field="attr_id">
        <input type="text" class="form-control" placeholder="Значение атрибута" value="${escapeHtml(String(attrValue))}" style="font-size:12px;" data-field="attr_value">
        <input type="number" class="form-control" placeholder="ID знач." value="${escapeHtml(String(attrValueId || ''))}" style="font-size:12px;" data-field="attr_value_id">
        <button type="button" class="btn btn-danger btn-sm" style="padding: 4px; height: 32px; width: 32px;" onclick="document.getElementById('${rowId}').remove()" title="Удалить строку">✕</button>
    `;
    container.appendChild(div);
}

/**
 * Сохранение карточки товара (в НКТ или черновик)
 */
async function saveProductCard(sendToModeration = false) {
    if (!currentSellerId) return showToast('Внимание', 'Сначала выберите продавца', 'warning');

    const name = document.getElementById('nk_card_name').value.trim();
    const article = document.getElementById('nk_card_article').value.trim();
    const brand = document.getElementById('nk_card_brand').value.trim();
    const gtin = document.getElementById('nk_card_gtin').value.trim();
    const catIdRaw = document.getElementById('nk_card_category_id').value.trim();
    const catName = document.getElementById('nk_card_category_name').value.trim();
    const tnved = document.getElementById('nk_card_tnved').value.trim();

    if (!name) return showToast('Ошибка', 'Укажите наименование товара', 'error');

    // Build attributes list
    const attributes = [];
    if (article) attributes.push({ attr_id: 10001, attr_value: article });
    if (tnved) attributes.push({ attr_id: 10609, attr_value: tnved });
    const composition = document.getElementById('nk_card_composition').value.trim();
    if (composition) attributes.push({ attr_id: 10610, attr_value: composition });
    const country = document.getElementById('nk_card_country').value.trim();
    if (country) attributes.push({ attr_id: 10611, attr_value: country });
    const color = document.getElementById('nk_card_color').value.trim();
    if (color) attributes.push({ attr_id: 10612, attr_value: color });
    const size = document.getElementById('nk_card_size').value.trim();
    if (size) attributes.push({ attr_id: 10613, attr_value: size });
    const desc = document.getElementById('nk_card_description').value.trim();
    if (desc) attributes.push({ attr_id: 10614, attr_value: desc });

    // Custom attributes
    const customRows = document.querySelectorAll('#nk_custom_attrs_container > div');
    customRows.forEach(row => {
        const attrIdEl = row.querySelector('[data-field="attr_id"]');
        const attrValEl = row.querySelector('[data-field="attr_value"]');
        const attrValIdEl = row.querySelector('[data-field="attr_value_id"]');
        if (attrIdEl && attrIdEl.value.trim()) {
            const item = {
                attr_id: parseInt(attrIdEl.value.trim(), 10),
                attr_value: attrValEl ? attrValEl.value.trim() : ''
            };
            if (attrValIdEl && attrValIdEl.value.trim()) {
                item.attr_value_id = parseInt(attrValIdEl.value.trim(), 10);
            }
            attributes.push(item);
        }
    });

    const isModeration = sendToModeration || document.getElementById('nkCardModerationCheck')?.checked;

    const saveBtn = document.getElementById('btnSaveNkCard');
    if (saveBtn) saveBtn.classList.add('loading');

    try {
        let res;
        if (currentEditingCardId) {
            const updatePayload = {
                name,
                brand: brand || null,
                tnved: tnved || null,
                category_id: catIdRaw ? parseInt(catIdRaw, 10) : null,
                category_name: catName || null,
                moderation: !!isModeration,
                attributes
            };
            res = await apiFetch(`/sellers/${currentSellerId}/national-catalog/products/${currentEditingCardId}`, {
                method: 'PUT',
                body: JSON.stringify(updatePayload)
            });
            showToast('Карточка сохранена', 'Данные успешно обновлены в НКТ', 'success');
        } else {
            const createPayload = {
                name,
                brand: brand || null,
                gtin: gtin || null,
                tnved: tnved || null,
                category_id: catIdRaw ? parseInt(catIdRaw, 10) : 20000003,
                category_name: catName || null,
                is_tech_gtin: !gtin,
                is_set: false,
                moderation: !!isModeration,
                attributes
            };
            res = await apiFetch(`/sellers/${currentSellerId}/national-catalog/products`, {
                method: 'POST',
                body: JSON.stringify(createPayload)
            });
            showToast('Карточка создана', 'Карточка создана и передана в НКТ', 'success');
        }

        closeModal('productCardModal');
        await loadProductCards(true);
    } catch (err) {
        showToast('Ошибка сохранения', err.message, 'error');
    } finally {
        if (saveBtn) saveBtn.classList.remove('loading');
    }
}

/**
 * Проверка статуса модерации конкретной карточки в НКТ
 */
async function checkCardStatus(cardId) {
    if (!currentSellerId) return;
    try {
        showToast('Проверка статуса...', 'Запрос статуса карточки в НКТ...', 'info');
        const res = await apiFetch(`/sellers/${currentSellerId}/national-catalog/products/${cardId}/check-status`, {
            method: 'POST'
        });
        showToast('Статус НКТ', `Статус карточки: ${res.status}`, res.status === 'error' ? 'warning' : 'success');
        await loadProductCards(true);
    } catch (err) {
        showToast('Ошибка проверки', err.message, 'error');
    }
}

/**
 * Удаление карточки (локально)
 */
async function deleteProductCard(cardId, cardName) {
    if (!currentSellerId) return;
    if (!confirm(`Удалить карточку товара "${cardName}" из базы?`)) return;

    try {
        await apiFetch(`/sellers/${currentSellerId}/national-catalog/products/${cardId}`, { method: 'DELETE' });
        showToast('Удалено', 'Карточка товара удалена из базы данных', 'success');
        await loadProductCards(true);
    } catch (err) {
        showToast('Ошибка удаления', err.message, 'error');
    }
}

/**
 * Открытие модального окна подписания карточки сертификатом ЭЦП
 */
async function openSignCardModal(cardId) {
    if (!currentSellerId) return;
    currentCardToSign = cardId;

    const modalBody = document.getElementById('nkSignModalContent');
    if (modalBody) {
        modalBody.innerHTML = '<div style="text-align:center; padding:30px; color:var(--text-muted);">⏳ Формирование XML документа для подписания в НКТ...</div>';
    }
    openModal('productSignModal');

    // Populate CryptoPro certificates
    await safePopulateCertificatesDropdown('nk_cert_select');

    try {
        const prep = await apiFetch(`/sellers/${currentSellerId}/national-catalog/products/${cardId}/prepare-sign`, {
            method: 'POST'
        });

        if (modalBody) {
            modalBody.innerHTML = `
                <div style="background: rgba(15,23,42,0.6); border: 1px solid var(--border-color); border-radius: 8px; padding: 14px; margin-bottom: 16px;">
                    <div style="display:flex; justify-content:space-between; margin-bottom: 6px;">
                        <span style="font-weight:600; color:var(--text-main);">ID карточки (good_id):</span>
                        <span style="font-family:monospace; color:#60a5fa;">${prep.good_id}</span>
                    </div>
                    <div style="display:flex; justify-content:space-between; margin-bottom: 6px;">
                        <span style="font-weight:600; color:var(--text-main);">GTIN:</span>
                        <span style="font-family:monospace; color:#a5b4fc;">${prep.gtin || '-'}</span>
                    </div>
                    <div style="display:flex; justify-content:space-between;">
                        <span style="font-weight:600; color:var(--text-main);">Формат подписи:</span>
                        <span class="badge" style="background:rgba(168,85,247,0.15); color:#c084fc;">Открепленная CMS / PKCS#7</span>
                    </div>
                </div>

                <div class="form-group">
                    <label class="form-label">Предпросмотр XML документа НКТ</label>
                    <textarea class="form-control" id="nk_doc_to_sign" rows="7" readonly style="font-family:monospace; font-size:11px; background:rgba(0,0,0,0.3); color:#94a3b8;">${escapeHtml(prep.raw_xml || '')}</textarea>
                </div>
            `;
        }
    } catch (err) {
        if (modalBody) {
            modalBody.innerHTML = `<div style="text-align:center; padding:30px; color:var(--status-cancelled);">❌ Ошибка подготовки документа: ${escapeHtml(err.message)}</div>`;
        }
        showToast('Ошибка подготовки подписи', err.message, 'error');
    }
}

/**
 * Подписание сформированного XML и отправка в /nk/feed-product-sign-pkcs
 */
async function signAndPublishCard() {
    if (!currentSellerId || !currentCardToSign) return;

    const docEl = document.getElementById('nk_doc_to_sign');
    const docData = docEl ? docEl.value.trim() : '';
    if (!docData) {
        return showToast('Ошибка', 'Нет документа для подписания', 'error');
    }

    const certSelect = document.getElementById('nk_cert_select');
    const selectedThumbprint = certSelect ? certSelect.value.trim() : '';

    const btn = document.getElementById('btnSubmitNkSign');
    const statusBox = document.getElementById('nk_sign_status_box');
    if (btn) btn.classList.add('loading');
    if (statusBox) {
        statusBox.style.display = 'block';
        statusBox.innerHTML = '<span style="color:var(--primary-hover);">✍️ Подписание XML документа в плагине КриптоПро...</span>';
    }

    try {
        // Sign via CryptoPro detached CMS
        const detachedSignature = await signDataWithCryptoPro(docData, true, selectedThumbprint);
        if (!detachedSignature) {
            throw new Error('Подпись не была сформирована плагином');
        }

        if (statusBox) {
            statusBox.innerHTML = '<span style="color:var(--primary-hover);">🚀 Отправка отсоединенной подписи в Национальный Каталог...</span>';
        }

        const res = await apiFetch(`/sellers/${currentSellerId}/national-catalog/products/${currentCardToSign}/publish`, {
            method: 'POST',
            body: JSON.stringify({
                signature: detachedSignature
            })
        });

        showToast('Успех', 'Карточка успешно подписана и опубликована в НКТ!', 'success');
        closeModal('productSignModal');
        await loadProductCards(true);
    } catch (err) {
        showToast('Ошибка подписания', err.message, 'error');
        if (statusBox) {
            statusBox.innerHTML = `<span style="color:var(--status-cancelled);">❌ Ошибка: ${escapeHtml(err.message)}</span>`;
        }
    } finally {
        if (btn) btn.classList.remove('loading');
    }
}

function escapeHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}
