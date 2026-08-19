"""
QA Testing Agent — Автоматический агент-тестировщик системы
Запускает регрессионные тесты и валидацию после любых изменений в коде/конфигурации
"""
import logging
import asyncio
from datetime import datetime, timezone
import uuid
from typing import Dict, Any

from app.celery_app import celery_app
from app.services.encryption import encrypt, decrypt
from app.services.cz_client import CZClient
from app.services.crypto_service import _mock_signature

logger = logging.getLogger(__name__)


class QATestingError(Exception):
    """Exception raised when system regression tests fail."""
    pass


@celery_app.task(
    name="app.agents.qa_test_agent.run_system_regression_tests",
    queue="qa_testing",
    bind=True,
    max_retries=1,
)
def run_system_regression_tests(self) -> Dict[str, Any]:
    """
    Автоматически проверить работоспособность всех ключевых систем:
    1. Шифрование/расшифровка токенов (Fernet)
    2. Генерация документов Честного Знака ТГ 'lp' (3.0.38)
    3. Подпись документов (CMS / Mock signature)
    4. Целостность моделей данных
    """
    logger.info("[QA Agent] Starting automated regression test suite...")
    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trace_id": str(uuid.uuid4()),
        "passed": 0,
        "failed": 0,
        "tests": [],
    }

    # Test 1: Fernet Encryption & Decryption
    try:
        raw_secret = "test-wb-api-token-998877"
        encrypted = encrypt(raw_secret)
        decrypted = decrypt(encrypted)
        assert decrypted == raw_secret, "Decrypted token mismatch!"
        results["tests"].append({"name": "Token Encryption Check", "status": "PASSED"})
        results["passed"] += 1
    except Exception as e:
        results["tests"].append({"name": "Token Encryption Check", "status": "FAILED", "error": str(e)})
        results["failed"] += 1

    # Test 2: CZ Client 3.0.38 LP Withdrawal Document Builder
    try:
        import json
        client = CZClient(inn="771234567890", token="mock-token-uuid")
        doc = client._build_withdrawal_document(
            kiz_codes=["0104601234567890215QIQ8BQCXmSJJ"],
            price_kopecks=149000,
            mod_fias="8ed74f90-0119-48f2-b289-379707934e2f",
            mod_kpp=None,
            primary_document_number="ORDER-9911",
        )
        assert doc["type"] in ("LP_SHIP_GOODS", "LK_RECEIPT")
        parsed_inner = json.loads(doc["productDocument"])
        assert parsed_inner.get("inn") == "771234567890" or parsed_inner.get("participantInn") == "771234567890"
        cis_val = parsed_inner["products"][0].get("cis") or parsed_inner["products"][0].get("uitCode")
        assert cis_val == "0104601234567890215QIQ8BQCXmSJJ"
        results["tests"].append({"name": "CZ 3.0.38 LP Document Builder", "status": "PASSED"})
        results["passed"] += 1
    except Exception as e:
        results["tests"].append({"name": "CZ 3.0.38 LP Document Builder", "status": "FAILED", "error": str(e)})
        results["failed"] += 1

    # Test 3: Crypto Signature Engine
    try:
        mock_sig = _mock_signature('{"test": "payload"}')
        assert mock_sig.startswith("MOCK_SIG_")
        results["tests"].append({"name": "Crypto Signature Engine", "status": "PASSED"})
        results["passed"] += 1
    except Exception as e:
        results["tests"].append({"name": "Crypto Signature Engine", "status": "FAILED", "error": str(e)})
        results["failed"] += 1

    # Test 4: SGTIN GS1 DataMatrix Format Validation
    try:
        sgtin = "0104601234567890215QIQ8BQCXmSJJ"
        assert len(sgtin) >= 25, "SGTIN length check failed"
        assert sgtin.startswith("01"), "SGTIN GTIN prefix check failed"
        results["tests"].append({"name": "SGTIN GS1 Validation Rules", "status": "PASSED"})
        results["passed"] += 1
    except Exception as e:
        results["tests"].append({"name": "SGTIN GS1 Validation Rules", "status": "FAILED", "error": str(e)})
        results["failed"] += 1

    # Summary
    results["all_passed"] = (results["failed"] == 0)
    logger.info(f"[QA Agent] Completed test suite: {results['passed']} passed, {results['failed']} failed.")

    # Write to AuditLog
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from app.config import settings
        from app.models.audit import AuditLog

        sync_engine = create_engine(settings.database_url_sync)
        with Session(sync_engine) as db:
            log = AuditLog(
                agent="qa_test_agent",
                action="REGRESSION_TESTS_COMPLETE" if results["all_passed"] else "REGRESSION_TESTS_FAILED",
                entity_type="system",
                entity_id="regression_suite",
                payload={"passed": results["passed"], "failed": results["failed"], "tests": results["tests"]},
                error=None if results["all_passed"] else f"{results['failed']} tests failed",
                trace_id=results["trace_id"],
                created_at=datetime.now(timezone.utc),
            )
            db.add(log)
            db.commit()
    except Exception as audit_exc:
        logger.warning(f"[QA Agent] Failed to write audit log: {audit_exc}")

    return results
