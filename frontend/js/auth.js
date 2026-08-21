/**
 * WB FBS Manager — Authentication, Profile, and Password Management
 */

function updateAuthTopbarUI() {
    const usernameLabel = document.getElementById('topbarUsernameLabel');
    const logoutBtn = document.getElementById('topbarLogoutBtn');
    if (currentUser && authToken) {
        if (usernameLabel) usernameLabel.textContent = currentUser.username || 'Профиль';
        if (logoutBtn) logoutBtn.style.display = 'flex';
    } else {
        if (usernameLabel) usernameLabel.textContent = 'Вход';
        if (logoutBtn) logoutBtn.style.display = 'none';
    }
}

function showLoginModal(errorMessage = '') {
    const modal = document.getElementById('loginModal');
    const errBox = document.getElementById('loginErrorBox');
    if (errBox) {
        if (errorMessage) {
            errBox.textContent = errorMessage;
            errBox.style.display = 'block';
        } else {
            errBox.style.display = 'none';
        }
    }
    if (modal) modal.classList.add('active');
    const userInp = document.getElementById('loginUsername');
    if (userInp) setTimeout(() => userInp.focus(), 100);
}

function handleUnauthorized() {
    authToken = '';
    currentUser = null;
    localStorage.removeItem('wbfbs_auth_token');
    localStorage.removeItem('wbfbs_current_user');
    updateAuthTopbarUI();
    showLoginModal('Сессия истекла или требуется авторизация. Пожалуйста, войдите.');
}

async function handleLoginSubmit(event) {
    event.preventDefault();
    const username = document.getElementById('loginUsername').value.trim();
    const password = document.getElementById('loginPassword').value.trim();
    const btn = document.getElementById('loginSubmitBtn');
    const errBox = document.getElementById('loginErrorBox');

    if (!username || !password) return;

    btn.classList.add('loading');
    btn.disabled = true;
    if (errBox) errBox.style.display = 'none';

    try {
        const res = await fetch(`${API_BASE}/auth/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password })
        });

        if (!res.ok) {
            let errDetail = 'Неверный логин или пароль';
            try {
                const errJson = await res.json();
                if (errJson) {
                    if (typeof errJson.detail === 'string') {
                        errDetail = errJson.detail;
                    } else if (Array.isArray(errJson.detail)) {
                        errDetail = errJson.detail.map(d => d.msg || d.loc?.join('.')).join('; ');
                    } else if (errJson.message) {
                        errDetail = errJson.message;
                    }
                }
            } catch(e) {
                if (res.status === 500) {
                    errDetail = 'Внутренняя ошибка сервера (500). Проверьте логи Docker.';
                } else if (res.status === 404) {
                    errDetail = 'API endpoint не найден (404). Проверьте конфигурацию Nginx.';
                }
            }
            throw new Error(errDetail);
        }

        const data = await res.json();
        authToken = data.access_token;
        currentUser = data.user;
        localStorage.setItem('wbfbs_auth_token', authToken);
        localStorage.setItem('wbfbs_current_user', JSON.stringify(currentUser));

        updateAuthTopbarUI();
        closeModal('loginModal');

        // Check if user must change password upon first login
        if (currentUser.must_change_password) {
            showToast('Внимание', 'Требуется сменить начальный пароль перед началом работы', 'warning');
            showFirstLoginModal(password);
            return;
        }

        showToast('Успешный вход', `Добро пожаловать, ${currentUser.username}!`, 'success');
        await initAppPostLogin();
    } catch (err) {
        if (errBox) {
            errBox.textContent = err.message || 'Ошибка авторизации';
            errBox.style.display = 'block';
        }
        showToast('Ошибка входа', err.message, 'error');
    } finally {
        btn.classList.remove('loading');
        btn.disabled = false;
    }
}

function showFirstLoginModal(initialPassword = '') {
    const modal = document.getElementById('firstLoginModal');
    if (!modal) return;
    
    document.getElementById('firstOldPassword').value = initialPassword || '';
    document.getElementById('firstNewPassword').value = '';
    document.getElementById('firstConfirmPassword').value = '';
    const errBox = document.getElementById('firstLoginErrorBox');
    if (errBox) errBox.style.display = 'none';
    
    openModal('firstLoginModal');
    const newPassInp = document.getElementById('firstNewPassword');
    if (newPassInp) setTimeout(() => newPassInp.focus(), 150);
}

async function handleFirstLoginPasswordSubmit(event) {
    event.preventDefault();
    const old_password = document.getElementById('firstOldPassword').value;
    const new_password = document.getElementById('firstNewPassword').value;
    const confirm_password = document.getElementById('firstConfirmPassword').value;
    const btn = document.getElementById('firstLoginSubmitBtn');
    const errBox = document.getElementById('firstLoginErrorBox');

    if (new_password.length < 6) {
        if (errBox) {
            errBox.textContent = 'Новый пароль должен содержать минимум 6 символов';
            errBox.style.display = 'block';
        }
        return;
    }

    if (new_password !== confirm_password) {
        if (errBox) {
            errBox.textContent = 'Новые пароли не совпадают';
            errBox.style.display = 'block';
        }
        return;
    }

    btn.classList.add('loading');
    btn.disabled = true;
    if (errBox) errBox.style.display = 'none';

    try {
        const res = await apiFetch('/auth/change-password', {
            method: 'POST',
            body: JSON.stringify({ old_password, new_password })
        });

        if (currentUser) {
            currentUser.must_change_password = false;
            localStorage.setItem('wbfbs_current_user', JSON.stringify(currentUser));
        }

        closeModal('firstLoginModal');
        showToast('Успех', 'Пароль успешно установлен и сохранен в базе данных!', 'success');
        await initAppPostLogin();
    } catch (err) {
        if (errBox) {
            errBox.textContent = err.message || 'Ошибка смены пароля';
            errBox.style.display = 'block';
        }
        showToast('Ошибка', err.message, 'error');
    } finally {
        btn.classList.remove('loading');
        btn.disabled = false;
    }
}

function logout() {
    authToken = '';
    currentUser = null;
    localStorage.removeItem('wbfbs_auth_token');
    localStorage.removeItem('wbfbs_current_user');
    updateAuthTopbarUI();
    closeModal('userAccountModal');
    closeModal('firstLoginModal');
    showToast('Выход выполнен', 'Вы успешно вышли из учетной записи', 'info');
    showLoginModal();
}

function openUserAccountModal() {
    if (!authToken || !currentUser) {
        showLoginModal();
        return;
    }
    document.getElementById('accountModalUsername').textContent = currentUser.username || 'admin';
    document.getElementById('accountModalRole').textContent = currentUser.role === 'admin' ? 'Администратор' : 'Оператор';
    document.getElementById('oldPasswordInput').value = '';
    document.getElementById('newPasswordInput').value = '';
    document.getElementById('confirmPasswordInput').value = '';
    openModal('userAccountModal');
}

async function handleChangePasswordSubmit(event) {
    event.preventDefault();
    const old_password = document.getElementById('oldPasswordInput').value;
    const new_password = document.getElementById('newPasswordInput').value;
    const confirm_password = document.getElementById('confirmPasswordInput').value;
    const btn = document.getElementById('changePasswordBtn');

    if (new_password.length < 6) {
        showToast('Ошибка', 'Пароль должен содержать минимум 6 символов', 'error');
        return;
    }

    if (new_password !== confirm_password) {
        showToast('Ошибка', 'Новые пароли не совпадают', 'error');
        return;
    }

    btn.classList.add('loading');
    btn.disabled = true;

    try {
        const res = await apiFetch('/auth/change-password', {
            method: 'POST',
            body: JSON.stringify({ old_password, new_password })
        });

        if (currentUser) {
            currentUser.must_change_password = false;
            localStorage.setItem('wbfbs_current_user', JSON.stringify(currentUser));
        }

        showToast('Успех', res.message || 'Пароль успешно обновлен в базе данных', 'success');
        closeModal('userAccountModal');
    } catch (err) {
        showToast('Ошибка смены пароля', err.message, 'error');
    } finally {
        btn.classList.remove('loading');
        btn.disabled = false;
    }
}
