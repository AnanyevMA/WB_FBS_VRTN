"""
Encryption Service — шифрование чувствительных данных (токены API, credentials)
Использует Fernet symmetric encryption
"""
import base64
import logging
try:
    from cryptography.fernet import Fernet, InvalidToken
    HAS_FERNET = True
except (ImportError, ModuleNotFoundError):
    HAS_FERNET = False
    Fernet = None
    InvalidToken = Exception

from app.config import settings

logger = logging.getLogger(__name__)


def _get_fernet() -> Fernet:
    """Create Fernet instance from settings encryption key."""
    key = settings.encryption_key.encode()
    # Ensure key is valid base64 and 32 bytes → pad if needed
    if len(key) < 32:
        key = key.ljust(32, b"=")
    elif len(key) > 32:
        key = key[:32]
    fernet_key = base64.urlsafe_b64encode(key)
    return Fernet(fernet_key)


def encrypt(value: str) -> str:
    """Encrypt a plaintext string. Returns base64-encoded ciphertext."""
    if not value:
        return ""
    if not HAS_FERNET:
        return base64.b64encode(value.encode()).decode()
    try:
        f = _get_fernet()
        return f.encrypt(value.encode()).decode()
    except Exception as e:
        logger.error(f"Encryption failed: {e}")
        raise


def decrypt(value: str) -> str:
    """Decrypt a Fernet-encrypted string. Returns plaintext."""
    if not value:
        return ""
    if not HAS_FERNET:
        try:
            return base64.b64decode(value.encode()).decode()
        except Exception:
            return value
    try:
        f = _get_fernet()
        return f.decrypt(value.encode()).decode()
    except InvalidToken:
        logger.error("Decryption failed: invalid token / wrong key")
        raise ValueError("Cannot decrypt value: invalid token or key mismatch")
    except Exception as e:
        logger.error(f"Decryption failed: {e}")
        raise


class EncryptionService:
    """Class wrapper for Encryption & Decryption service."""

    @staticmethod
    def encrypt(value: str) -> str:
        return encrypt(value)

    @staticmethod
    def decrypt(value: str) -> str:
        return decrypt(value)

