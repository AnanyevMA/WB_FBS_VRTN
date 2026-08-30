/**
 * WB FBS Manager — Main Application Bootstrap & Client-Side Routing
 */

function navigateTo(route) {
    document.querySelectorAll('.nav-item').forEach(el => {
        el.classList.toggle('active', el.dataset.route === route);
    });

    document.querySelectorAll('.page').forEach(el => el.classList.remove('active'));
    const pageEl = document.getElementById(`page-${route}`);
    if (pageEl) {
        pageEl.classList.add('active');
    }

    if (route === '/kiz') {
        const kizInput = document.getElementById('kizScannerInput');
        if (kizInput) setTimeout(() => kizInput.focus(), 100);
    } else if (route === '/') {
        loadDashboard();
    } else if (route === '/orders') {
        loadOrders();
    } else if (route === '/signature-queue') {
        if (typeof loadSignatureBatches === 'function') loadSignatureBatches();
    } else if (route === '/supplies') {
        loadSupplies();
    } else if (route === '/sellers') {
        loadSellers();
    } else if (route === '/audit') {
        loadAuditLogs();
    }

    if (window.innerWidth <= 768) {
        const sidebar = document.getElementById('sidebar');
        if (sidebar) sidebar.classList.remove('open');
    }
    
    window.history.pushState({}, '', '#' + route);
}

async function initApp() {
    updateAuthTopbarUI();

    // Sidebar navigation items
    document.querySelectorAll('.nav-item').forEach(el => {
        el.addEventListener('click', () => navigateTo(el.dataset.route));
    });

    // Mobile sidebar toggle
    const mobileBtn = document.getElementById('mobileMenuBtn');
    if (mobileBtn) {
        mobileBtn.addEventListener('click', () => {
            const sidebar = document.getElementById('sidebar');
            if (sidebar) sidebar.classList.toggle('open');
        });
    }
    
    // Global refresh button
    const refreshBtn = document.getElementById('globalRefreshBtn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', () => {
            const activeNav = document.querySelector('.nav-item.active');
            const currentRoute = activeNav ? activeNav.dataset.route : '/';
            navigateTo(currentRoute);
            showToast('Данные обновлены', 'Информация успешно синхронизирована', 'success');
        });
    }

    // Topbar settings button
    const settingsBtn = document.getElementById('topbarSettingsBtn');
    if (settingsBtn) {
        settingsBtn.addEventListener('click', () => {
            if (currentSellerId) {
                editSeller(currentSellerId);
            } else {
                openAddSellerModal();
            }
        });
    }

    // Topbar profile button
    const profileBtn = document.getElementById('topbarProfileBtn');
    if (profileBtn) {
        profileBtn.addEventListener('click', () => {
            openUserAccountModal();
        });
    }

    // Seed mock data button
    const seedBtn = document.getElementById('seedDemoDataBtn');
    if (seedBtn) {
        seedBtn.addEventListener('click', seedMockData);
    }

    // Orders filters & search listeners
    const orderStatusFilter = document.getElementById('orderStatusFilter');
    if (orderStatusFilter) orderStatusFilter.addEventListener('change', () => loadOrders());

    const orderKizStatusFilter = document.getElementById('orderKizStatusFilter');
    if (orderKizStatusFilter) orderKizStatusFilter.addEventListener('change', () => loadOrders());

    const orderSearchInput = document.getElementById('orderSearchInput');
    if (orderSearchInput) orderSearchInput.addEventListener('input', () => loadOrders());

    // Sync KIZ statuses from Chestny Znak button
    const syncCzOrdersBtn = document.getElementById('syncCzOrdersBtn');
    if (syncCzOrdersBtn) {
        syncCzOrdersBtn.addEventListener('click', async (e) => {
            if (!currentSellerId) return showToast('Ошибка', 'Сначала выберите продавца', 'error');
            const btn = e.currentTarget;
            btn.classList.add('loading');
            try {
                const res = await apiFetch(`/sellers/${currentSellerId}/orders/sync-cz`, { method: 'POST' });
                showToast('Честный Знак', res.message || 'Статусы КИЗ успешно обновлены через Честный Знак', 'success');
                await loadOrders(true);
                await loadDashboard();
            } catch (err) {
                showToast('Ошибка Честного Знака', err.message, 'error');
            } finally {
                btn.classList.remove('loading');
            }
        });
    }

    // Sync orders from WB button
    const syncOrdersBtn = document.getElementById('syncOrdersBtn');
    if (syncOrdersBtn) {
        syncOrdersBtn.addEventListener('click', async (e) => {
            if (!currentSellerId) return showToast('Ошибка', 'Сначала выберите продавца', 'error');
            const btn = e.currentTarget;
            btn.classList.add('loading');
            try {
                const res = await apiFetch(`/sellers/${currentSellerId}/orders/sync`, { method: 'POST' });
                showToast('Успех', res.message || 'Синхронизация заказов с WB запущена', 'success');
                await loadOrders();
                await loadDashboard();
            } catch (err) {
                showToast('Ошибка', err.message, 'error');
            } finally {
                btn.classList.remove('loading');
            }
        });
    }

    // KIZ scanner inputs
    const scannerInput = document.getElementById('kizScannerInput');
    if (scannerInput) {
        scannerInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                processKizScan(scannerInput.value);
                scannerInput.value = '';
            }
        });
    }

    const kizManualBtn = document.getElementById('kizManualSubmitBtn');
    if (kizManualBtn && scannerInput) {
        kizManualBtn.addEventListener('click', () => {
            if (scannerInput.value) {
                processKizScan(scannerInput.value);
                scannerInput.value = '';
            }
        });
    }

    // Window history navigation (back / forward)
    window.addEventListener('popstate', () => {
        const route = window.location.hash.replace('#', '') || '/';
        navigateTo(route);
    });

    if (!authToken) {
        showLoginModal();
        return;
    }

    // Validate token with backend
    try {
        const profile = await apiFetch('/auth/me');
        currentUser = profile;
        localStorage.setItem('wbfbs_current_user', JSON.stringify(profile));
        updateAuthTopbarUI();

        if (currentUser && currentUser.must_change_password) {
            showToast('Внимание', 'Требуется сменить начальный пароль', 'warning');
            showFirstLoginModal();
            return;
        }

        await initAppPostLogin();
    } catch (e) {
        console.warn('Token validation failed, showing login modal:', e);
        handleUnauthorized();
    }
}

async function initAppPostLogin() {
    const route = window.location.hash.replace('#', '') || '/';
    await loadSellersForDropdown();
    navigateTo(route);
    if (typeof updateSignatureBadge === 'function') updateSignatureBadge();

    if (refreshInterval) clearInterval(refreshInterval);
    refreshInterval = setInterval(() => {
        const ordersPage = document.getElementById('page-/orders');
        if (ordersPage && ordersPage.classList.contains('active')) {
            loadOrders(true);
        }
        if (typeof updateSignatureBadge === 'function') updateSignatureBadge();
    }, 30000);

    // Initial background CZ token check & recurring 15-minute background refresh
    setTimeout(() => {
        if (typeof silentCheckAndRefreshCzToken === 'function') {
            silentCheckAndRefreshCzToken();
        }
    }, 2500);

    setInterval(() => {
        if (typeof silentCheckAndRefreshCzToken === 'function') {
            silentCheckAndRefreshCzToken();
        }
    }, 15 * 60 * 1000);
}

// Run init on DOM Ready
document.addEventListener('DOMContentLoaded', () => {
    initApp();
    initCryptoProPlugin();
});
