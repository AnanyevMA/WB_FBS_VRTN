# Мультиагентный Слой, PoLP и Распределенный Арбитраж

> **Категория**: `solutions_and_recipes` | **Документ ID**: `sol_02_multiagent_coordination`  
> **Спецификация**: [`agents_config.json`](file:///D:/PyCharm_Projects/WB%20FBS/agents_config.json) / [`docs/ARCHITECTURE_PLAYBOOK.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/ARCHITECTURE_PLAYBOOK.md)

---

## 1. Топология Очередей и Приоритетов

Для исключения взаимных блокировок и исчерпания ресурсов воркеров Celery, задачи распределены по изолированным очередям со строгими весами арбитража:

```
[Высокий приоритет]  100: notifications    (notifier, morning_digest)
                      95: cz_token_refresher (сессионные токены ГИС МТ)
                      90: cz_operations    (cz_withdrawal, cz_return)
                      80: orders           (order_poller)
                      70: supplies         (supply_agent)
                      50: archive          (archive_processor)
                      30: qa_testing       (qa_test_agent)
[Фоновый приоритет]   10: maintenance      (cleanup, kb_sync_agent)
```

---

## 2. Распределенные Блокировки Redis (`redis_lock`)

Для предотвращения состояния гонки (Race Condition), когда несколько воркеров одновременно пытаются обработать один и тот же заказ или закрыть одну поставку, применяется распределенная блокировка на базе Redis:

```python
# Паттерн распределенной блокировки
lock_key = f"wb_fbs_lock:supply:{supply_id}"
with redis_client.lock(lock_key, timeout=60):
    # Критическая секция: добавление заказов и закрытие поставки
    ...
```

---

## 3. Фреймворк Принципа Минимальных Привилегий (PoLP)

Каждый агент ограничен декларативной матрицей доступа в [`agents_config.json`](file:///D:/PyCharm_Projects/WB%20FBS/agents_config.json):

1. **Файловая система**:
   - Агенты имеют доступ только к своим подкаталогам в `storage/` и файлам логов в `logs/`.
   - Глобальный запрет доступа к `.env`, закрытым ключам `*.key`, `*.pem` и коду `frontend/`.
2. **База данных**:
   - `SELECT`, `INSERT`, `UPDATE`, `DELETE` разграничены на уровне таблиц (например, агент `cleanup` имеет право на `DELETE` только в `audit_logs`).
3. **Криптографические ключи (`crypto_key_access`)**:
   - `NONE`: Агент не имеет доступа к криптографии (например, `cleanup`, `archive_processor`).
   - `READ_DECRYPT_TOKENS`: Агент может расшифровывать API-токены WB/TG в памяти (`order_poller`, `supply_agent`).
   - `USE_UKEP_SIGNATURE`: Агент имеет право подписи документов в КриптоПро (`cz_withdrawal`, `cz_return`, `cz_token_refresher`).

Валидация выполняется перед выполнением операций классом `PoLPEnforcer` из [`app/agent_manifest.py`](file:///D:/PyCharm_Projects/WB%20FBS/app/agent_manifest.py).
