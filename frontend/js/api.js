/**
 * WB FBS Manager — API Client & Fetch Interceptor
 */

async function apiFetch(endpoint, options = {}) {
    const url = `${API_BASE}${endpoint}`;
    const defaultHeaders = {
        'Content-Type': 'application/json',
        'X-Seller-ID': currentSellerId || ''
    };

    if (authToken) {
        defaultHeaders['Authorization'] = `Bearer ${authToken}`;
    }
    
    options.headers = { ...defaultHeaders, ...options.headers };

    const response = await fetch(url, options);
    if (response.status === 401 && endpoint !== '/auth/login') {
        let errorDetail = '';
        try {
            const clone = response.clone();
            const errJson = await clone.json();
            if (errJson && errJson.detail) {
                errorDetail = typeof errJson.detail === 'string' ? errJson.detail : JSON.stringify(errJson.detail);
            }
        } catch(e) {}

        const isCzOrExternalAuthError = errorDetail.includes('Честн') || 
                                        errorDetail.includes('Знак') || 
                                        errorDetail.includes('ЭЦП') || 
                                        errorDetail.includes('КЭП') || 
                                        errorDetail.includes('ГИС МТ') || 
                                        errorDetail.includes('True API') ||
                                        errorDetail.includes('CZ/SUZ token') ||
                                        errorDetail.includes('token expired');

        if (!isCzOrExternalAuthError) {
            handleUnauthorized();
            throw new Error(errorDetail || 'Требуется авторизация (401)');
        }

        // Automatic transparent token refresh via CryptoPro plugin (transparent retry)
        const isAuthServiceEndpoint = endpoint.includes('/cz-challenge') || 
                                      endpoint.includes('/cz-signin') || 
                                      endpoint.includes('/cz-token-status');

        if (!options._czRetryCount && !isAuthServiceEndpoint && typeof window.forceRefreshCzTokenViaBrowser === 'function' && currentSellerId) {
            console.log(`[apiFetch] 401 CZ session error detected on ${endpoint}. Auto-refreshing token via CryptoPro...`);
            options._czRetryCount = 1;
            if (typeof showToast === 'function') {
                showToast('Честный Знак', 'Сессия ГИС МТ истекла. Авто-продление токена через ЭЦП...', 'warning');
            }
            try {
                const refreshed = await window.forceRefreshCzTokenViaBrowser(currentSellerId);
                if (refreshed) {
                    if (typeof showToast === 'function') {
                        showToast('Честный Знак', 'Токен успешно продлен! Повтор операции...', 'info');
                    }
                    return await apiFetch(endpoint, options);
                }
            } catch (refreshErr) {
                console.warn("[apiFetch] Auto-refresh failed:", refreshErr);
            }
        }

        const czErr = new Error(errorDetail || 'Срок действия сессии Честного Знака истек (401)');
        czErr.status = 401;
        czErr.isCzAuthError = true;
        throw czErr;
    }

    if (!response.ok) {
        let errorText = response.statusText;
        try {
            const errJson = await response.json();
            if (errJson && errJson.detail) {
                errorText = typeof errJson.detail === 'string' ? errJson.detail : JSON.stringify(errJson.detail);
            }
        } catch(e) {}
        const err = new Error(errorText || `Ошибка HTTP ${response.status}`);
        err.status = response.status;
        throw err;
    }
    return await response.json();
}
