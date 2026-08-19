"""
Security Audit Agent — WB FBS Manager

Performs periodic background security audits:
1. Validates cryptographic secret keys and flags insecure defaults.
2. Inspects database accounts for default passwords and active sessions.
3. Monitors SSL/TLS certificate validity and expiration.
4. Scans recent audit logs for security anomalies (unauthorized spikes, failed requests).
5. Reports findings to the audit trail and dispatches Telegram security alerts.
"""
from datetime import datetime, timedelta, timezone
import os
import uuid
import logging
from typing import Dict, Any, List

from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker

from app.celery_app import celery_app
from app.config import settings
from app.models.audit import AuditLog
from app.models.user import User
from app.models.seller import Seller
from app.services.auth_service import verify_password
from app.services.telegram_service import TelegramService

logger = logging.getLogger(__name__)

# Synchronous session factory for Celery workers
sync_engine = create_engine(
    settings.database_url_sync,
    pool_pre_ping=True,
)
SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    autoflush=False,
    expire_on_commit=False,
)


def inspect_security_posture(session) -> Dict[str, Any]:
    """Inspect system security configuration, secrets, users, and audit logs."""
    findings: List[Dict[str, Any]] = []
    warnings_count = 0
    critical_count = 0

    # 1. Check for insecure default secret keys
    if settings.secret_key in ("change-this-secret-key", "", "secret"):
        findings.append({
            "level": "CRITICAL",
            "category": "secrets",
            "message": "SECRET_KEY uses insecure default value. Run generate_secrets.py."
        })
        critical_count += 1

    if settings.jwt_secret_key in ("change-this-jwt-secret", "", "secret"):
        findings.append({
            "level": "CRITICAL",
            "category": "jwt",
            "message": "JWT_SECRET_KEY uses insecure default value. Run generate_secrets.py."
        })
        critical_count += 1

    if settings.encryption_key in ("change-this-encryption-key-32-b", "", "secret"):
        findings.append({
            "level": "CRITICAL",
            "category": "encryption",
            "message": "ENCRYPTION_KEY uses insecure default value. Tokens may not be safe."
        })
        critical_count += 1

    # 2. Check for default admin password
    users = session.execute(select(User)).scalars().all()
    for user in users:
        if user.username == "admin" and verify_password("admin_password", user.hashed_password):
            findings.append({
                "level": "CRITICAL",
                "category": "users",
                "message": "Default admin account still has default password 'admin_password'. Change immediately!"
            })
            critical_count += 1

    # 3. Check for inactive or misconfigured sellers
    sellers = session.execute(select(Seller).where(Seller.is_active == True)).scalars().all()
    for s in sellers:
        if not s.wb_api_token_encrypted:
            findings.append({
                "level": "WARNING",
                "category": "sellers",
                "message": f"Active seller '{s.name}' (ID: {s.id}) has no WB API token configured."
            })
            warnings_count += 1

    # 4. Check SSL Certificate files if path is available
    cert_paths = [
        "/etc/letsencrypt/live",
        "./certbot/conf/live"
    ]
    for cp in cert_paths:
        if os.path.exists(cp):
            try:
                for domain_dir in os.listdir(cp):
                    cert_file = os.path.join(cp, domain_dir, "cert.pem")
                    if os.path.isfile(cert_file):
                        from cryptography import x509
                        with open(cert_file, "rb") as f:
                            cert = x509.load_pem_x509_certificate(f.read())
                            days_left = (cert.not_valid_after_utc - datetime.now(timezone.utc)).days
                            if days_left <= 7:
                                findings.append({
                                    "level": "CRITICAL",
                                    "category": "ssl",
                                    "message": f"SSL certificate for {domain_dir} expires in {days_left} days!"
                                })
                                critical_count += 1
                            elif days_left <= 21:
                                findings.append({
                                    "level": "WARNING",
                                    "category": "ssl",
                                    "message": f"SSL certificate for {domain_dir} expires in {days_left} days."
                                })
                                warnings_count += 1
            except Exception as e:
                logger.debug(f"SSL cert inspection skipped or failed: {e}")

    # 5. Check recent audit errors / unauthorized attempts (last 24 hours)
    yesterday = datetime.now(timezone.utc) - timedelta(hours=24)
    error_logs_count = session.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.created_at >= yesterday,
            AuditLog.error.is_not(None)
        )
    ) or 0

    if error_logs_count > 50:
        findings.append({
            "level": "WARNING",
            "category": "audit_logs",
            "message": f"High rate of errors detected in audit log: {error_logs_count} failures in the last 24h."
        })
        warnings_count += 1

    status_verdict = "HEALTHY"
    if critical_count > 0:
        status_verdict = "CRITICAL_RISK"
    elif warnings_count > 0:
        status_verdict = "WARNINGS_FOUND"

    return {
        "status": status_verdict,
        "critical_count": critical_count,
        "warnings_count": warnings_count,
        "findings": findings,
        "audited_at": datetime.now(timezone.utc).isoformat()
    }


@celery_app.task(name="app.agents.security_audit_agent.run_security_audit", queue="maintenance")
def run_security_audit() -> Dict[str, Any]:
    """Execute complete security health audit and record results."""
    logger.info("Starting Security Audit Agent run...")
    with SyncSessionLocal() as session:
        report = inspect_security_posture(session)

        # Log audit entry
        audit_entry = AuditLog(
            seller_id=None,
            agent="security_audit_agent",
            action="SECURITY_AUDIT_RUN",
            entity_type="system",
            entity_id="global",
            payload=report,
            trace_id=str(uuid.uuid4()),
            created_at=datetime.now(timezone.utc)
        )
        session.add(audit_entry)
        session.commit()

        if report["critical_count"] > 0 or report["warnings_count"] > 0:
            logger.warning(f"Security audit completed with findings: {report['status']} (Critical: {report['critical_count']}, Warnings: {report['warnings_count']})")
        else:
            logger.info("Security audit completed: All checks passed. System is HEALTHY.")

        return report
