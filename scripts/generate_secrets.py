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
    """Generate secure password for DB/Redis."""
    return secrets.token_urlsafe(length)


def main():
    print("Генерация секретных ключей для WB FBS Manager...")
    print("=" * 60)

    secret_key = generate_random_token(32)
    jwt_secret = generate_random_token(32)
    encryption_key = generate_fernet_key()
    postgres_password = generate_password(20)
    redis_password = generate_password(20)
    flower_password = generate_password(16)

    print(f"SECRET_KEY:        {secret_key}")
    print(f"JWT_SECRET_KEY:    {jwt_secret}")
    print(f"ENCRYPTION_KEY:    {encryption_key}")
    print(f"POSTGRES_PASSWORD: {postgres_password}")
    print(f"REDIS_PASSWORD:    {redis_password}")
    print(f"FLOWER_PASSWORD:   {flower_password}")
    print("=" * 60)

    env_path = Path(".env")
    example_path = Path(".env.example")

    if not env_path.exists() and example_path.exists():
        content = example_path.read_text(encoding="utf-8")
        content = content.replace("your-very-secret-key-change-this-in-production", secret_key)
        content = content.replace("your-jwt-secret-key-change-this", jwt_secret)
        content = content.replace("your-fernet-encryption-key-32-bytes", encryption_key)
        content = content.replace("wbfbs_password", postgres_password)
        content = content.replace("admin_password", flower_password)
        env_path.write_text(content, encoding="utf-8")
        print("[OK] Файл .env успешно создан и заполнен сгенерированными ключами!")
    else:
        print("[INFO] Файл .env уже существует. Скопируйте нужные ключи вручную.")


if __name__ == "__main__":
    main()