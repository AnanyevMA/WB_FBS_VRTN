#!/usr/bin/env python3
"""
Generate Secure Keys and Passwords for WB FBS Manager
Генерация криптографически стойких ключей для .env
"""
import os
import sys
import secrets
from pathlib import Path

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

from cryptography.fernet import Fernet


def generate_fernet_key() -> str:
    """Generate 32-byte URL-safe base64-encoded Fernet key."""
    return Fernet.generate_key().decode("utf-8")


def generate_random_token(length: int = 32) -> str:
    """Generate cryptographically secure random hex string."""
    return secrets.token_hex(length)


def generate_password(length: int = 24) -> str:
    """Generate secure alphanumeric password for DB/Redis."""
    # Use hex for password to avoid special character escaping issues in URLs
    return secrets.token_hex(length // 2)


def main():
    print("Генерация секретных ключей для WB FBS Manager...")
    print("=" * 60)

    secret_key = generate_random_token(32)
    jwt_secret = generate_random_token(32)
    encryption_key = generate_fernet_key()
    postgres_password = generate_password(20)
    admin_password = generate_password(16)
    flower_password = generate_password(16)

    print(f"SECRET_KEY:        {secret_key}")
    print(f"JWT_SECRET_KEY:    {jwt_secret}")
    print(f"ENCRYPTION_KEY:    {encryption_key}")
    print(f"POSTGRES_PASSWORD: {postgres_password}")
    print(f"ADMIN_PASSWORD:    {admin_password}")
    print(f"FLOWER_PASSWORD:   {flower_password}")
    print("=" * 60)

    env_path = Path(".env")
    example_path = Path(".env.example")

    if example_path.exists():
        content = example_path.read_text(encoding="utf-8")
        content = content.replace("your-very-secret-key-change-this-in-production", secret_key)
        content = content.replace("your-jwt-secret-key-change-this", jwt_secret)
        content = content.replace("jK4hdZuYiftat9StWo41NqsmE9HHzxj5I6tMNq4LgnA=", encryption_key)
        content = content.replace("wbfbs_password", postgres_password)
        content = content.replace("ADMIN_PASSWORD=admin_password", f"ADMIN_PASSWORD={admin_password}")
        content = content.replace("admin_password", flower_password)
        env_path.write_text(content, encoding="utf-8")
        print("[OK] Файл .env успешно создан / синхронизирован с новыми ключами!")
    else:
        print("[ERROR] Файл .env.example не найден!")


if __name__ == "__main__":
    main()