# Развертывание КриптоПро CSP и Сертификатов в Docker/Linux

> **Категория**: `solutions_and_recipes` | **Документ ID**: `sol_03_deployment_and_crypto`  
> **Среда**: Docker / Ubuntu 22.04 LTS / КриптоПро CSP 5.0 R2/R3

---

## 1. Архитектура Криптографического Контейнера

Подписание документов для ГИС МТ (Честный Знак) требует наличия СКЗИ «КриптоПро CSP» с поддержкой ГОСТ Р 34.10-2012.

### Схема развертывания:
1. Базовый образ: `python:3.12-slim-bookworm` или `ubuntu:22.04`.
2. Установка пакетов `cprocsp-rdr-pcsc`, `cprocsp-pki-cades`, `lsb-cprocsp-devel`.
3. Установка библиотеки `pycades` для вызова СКЗИ из Python.

---

## 2. Установка Корневых и Пользовательских Сертификатов

### 2.1 Установка корневых сертификатов Минцифры и ЦРПТ
```bash
# Корневой сертификат Головного удостоверяющего центра (ГУЦ)
/opt/cprocsp/bin/amd64/certmgr -inst -store mRoot -file guc.cer

# Сертификат промежуточного УЦ ФНС / ЦРПТ
/opt/cprocsp/bin/amd64/certmgr -inst -store mCA -file ca_fns.cer
```

### 2.2 Установка личного сертификата продавца с привязкой закрытого ключа
```bash
# Установка контейнера закрытого ключа с флешки/токена/директории
/opt/cprocsp/bin/amd64/csptest -keyset -enum_cont -verifycontext -fqcn

# Привязка сертификата к закрытому ключу в хранилище uMy
/opt/cprocsp/bin/amd64/certmgr -inst -store uMy -file cert_seller.cer -cont '\\.\HDIMAGE\seller_key'
```

### 2.3 Получение отпечатка (Thumbprint) сертификата
```bash
/opt/cprocsp/bin/amd64/certmgr -list -store uMy
```
*Полученный SHA-1 отпечаток (например, `a1b2c3d4e5f60718293a4b5c6d7e8f9012345678`) указывается в поле `cryptopro_cert_thumbprint` селлера.*

---

## 3. Переменные Окружения (`.env`)

```ini
# --- КриптоПро CSP ---
CRYPTOPRO_ENABLED=true
CRYPTOPRO_CERT_THUMBPRINT=a1b2c3d4e5f60718293a4b5c6d7e8f9012345678
CRYPTOPRO_CSPTEST_PATH=/opt/cprocsp/bin/amd64/csptest

# --- Честный Знак ГИС МТ ---
CZ_API_URL=https://markirovka.crpt.ru
CZ_OMS_ID=4b68e4bf-98f2-49aa-b51c-76e9efc991e2
CZ_STAND=PRODUCTION
```
