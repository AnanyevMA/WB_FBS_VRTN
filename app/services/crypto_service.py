"""
КриптоПро Digital Signature Service
Подписывает документы для ГИС МТ используя УКЭП через КриптоПро
"""
import asyncio
import base64
import hashlib
import logging
import os
import subprocess
import tempfile
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


class CryptoSignatureError(Exception):
    """Error during document signing."""
    pass


async def sign_document(
    data: str,
    cert_thumbprint: Optional[str] = None,
) -> str:
    """
    Sign a document body using КриптоПро УКЭП.

    Args:
        data: Document content to sign (JSON string)
        cert_thumbprint: Certificate thumbprint (SHA-1), defaults to settings

    Returns:
        Base64-encoded CMS/PKCS#7 detached signature

    Note:
        Requires КриптоПро CSP installed on the server.
        Certificate must be in user's certificate store.
    """
def _find_cryptopro_bin() -> Optional[str]:
    """Find CryptoPro executable (csptest or cryptcp) on Windows/Linux."""
    configured = settings.cryptopro_bin_path
    if configured and os.path.exists(configured):
        return configured

    candidates = [
        r"C:\Program Files\Crypto Pro\CSP\csptest.exe",
        r"C:\Program Files (x86)\Crypto Pro\CSP\csptest.exe",
        r"C:\Program Files\Crypto Pro\CSP\cryptcp.exe",
        "/opt/cprocsp/bin/amd64/csptest",
        "/opt/cprocsp/bin/amd64/cryptcp",
        "/usr/local/bin/cryptcp",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


async def sign_document(
    data: str,
    cert_thumbprint: Optional[str] = None,
    attached: bool = False,
) -> str:
    """
    Sign a document body using КриптоПро УКЭП.

    Args:
        data: Document content to sign (JSON or challenge string)
        cert_thumbprint: Certificate thumbprint (SHA-1), defaults to settings
        attached: If True, generate attached CMS/PKCS#7 signature (used for SUZ simpleSignIn)

    Returns:
        Base64-encoded CMS/PKCS#7 signature
    """
    thumbprint = cert_thumbprint or settings.cryptopro_cert_thumbprint
    cryptopro_bin = _find_cryptopro_bin()

    if not thumbprint:
        logger.warning("No certificate thumbprint configured — skipping real signature")
        return _mock_signature(data)

    if not cryptopro_bin:
        logger.warning("КриптоПро binary not found — using mock signature")
        return _mock_signature(data)

    # Write data to temp file
    with tempfile.NamedTemporaryFile(
        mode='w',
        suffix='.json',
        delete=False,
        encoding='utf-8'
    ) as tmp_in:
        tmp_in.write(data)
        tmp_in_path = tmp_in.name

    tmp_out_path = tmp_in_path + ".sig"

    try:
        # Build command depending on csptest vs cryptcp
        if "csptest" in os.path.basename(cryptopro_bin).lower():
            cmd = [
                cryptopro_bin,
                "-sfsign",
                "-sign",
            ]
            if not attached:
                cmd.append("-detached")
            cmd.extend([
                "-base64",
                "-add",
                "-in", tmp_in_path,
                "-out", tmp_out_path,
                "-my", thumbprint,
            ])
        else:
            cmd = [
                cryptopro_bin,
                "-sign",
                "-thumbprint", thumbprint,
                "-in", tmp_in_path,
                "-out", tmp_out_path,
            ]
            if not attached:
                cmd.append("-detached")
            cmd.append("-base64")

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                cmd,
                capture_output=True,
                timeout=30,
                check=False,
            )
        )

        if result.returncode != 0:
            error_msg = result.stderr.decode('utf-8', errors='replace') or result.stdout.decode('utf-8', errors='replace')
            raise CryptoSignatureError(
                f"КриптоПро signing failed (code {result.returncode}): {error_msg}"
            )

        # Read signed output
        with open(tmp_out_path, 'r', encoding='utf-8') as f:
            signature = f.read().strip()

        # Clean any csptest log noise if present
        if "-----BEGIN" in signature or "\n" in signature:
            lines = [line.strip() for line in signature.splitlines() if line.strip() and not line.startswith("-----") and not line.startswith("#") and not line.startswith("Subject:") and not line.startswith("Valid:") and not line.startswith("Issuer:") and not line.startswith("PrivKey:") and not line.startswith("Source") and not line.startswith("Calculated") and not line.startswith("Signature") and not line.startswith("Output") and not line.startswith("Total") and not line.startswith("[ErrorCode")]
            # Join base64 content
            signature = "".join(lines)

        logger.info(f"Document signed successfully with cert {thumbprint[:8]}...")
        return signature

    except subprocess.TimeoutExpired:
        raise CryptoSignatureError("КриптоПро signing timed out (30s)")
    except FileNotFoundError:
        raise CryptoSignatureError(f"КриптоПро binary not found: {cryptopro_bin}")
    finally:
        # Cleanup temp files
        for path in [tmp_in_path, tmp_out_path]:
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass


def _mock_signature(data: str) -> str:
    """
    Mock signature for development/testing when КриптоПро is not available.
    Returns a deterministic base64 string — NOT a real signature!
    """
    logger.warning("Using MOCK signature — not valid for production ГИС МТ!")
    mock_sig = base64.b64encode(
        hashlib.sha256(data.encode()).digest()
    ).decode()
    return f"MOCK_SIG_{mock_sig}"


def is_cryptopro_available() -> bool:
    """Check if КриптоПро is installed and accessible."""
    return _find_cryptopro_bin() is not None

