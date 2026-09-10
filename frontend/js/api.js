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
                                        errorDetail.includes('True API');

        if (!isCzOrExternalAuthError) {
            handleUnauthorized();
            throw new Error(errorDetail || 'Требуется авторизация (401)');
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
