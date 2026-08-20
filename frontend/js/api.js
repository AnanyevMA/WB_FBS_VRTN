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
        handleUnauthorized();
        throw new Error('Требуется авторизация (401)');
    }

    if (!response.ok) {
        let errorText = response.statusText;
        try {
            const errJson = await response.json();
            if (errJson && errJson.detail) {
                errorText = typeof errJson.detail === 'string' ? errJson.detail : JSON.stringify(errJson.detail);
            }
        } catch(e) {}
        throw new Error(errorText || `Ошибка HTTP ${response.status}`);
    }
    return await response.json();
}
