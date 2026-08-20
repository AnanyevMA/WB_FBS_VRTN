#!/usr/bin/env python3
"""
Set or Reset Admin Password for WB FBS Manager
Смена или восстановление пароля администратора в базе данных без потери данных.

Использование:
  python3 scripts/set_admin_password.py --username admin --password "новый_пароль"
  docker compose -f docker-compose.prod.yml exec api python scripts/set_admin_password.py --password "новый_пароль"
"""
import argparse
import os
import sys
from datetime import datetime, timezone

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

# Add current directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.models.user import User, UserRole
from app.services.auth_service import hash_password


def set_admin_password(username: str, password: str, email: str = "admin@example.com") -> bool:
    """Update or create admin user with the specified password in the database."""
    sync_engine = create_engine(settings.database_url_sync)
    with Session(sync_engine) as session:
        user = session.execute(
            select(User).where(User.username == username)
        ).scalar_one_or_none()

        if user:
            user.hashed_password = hash_password(password)
            user.is_active = True
            user.is_superuser = True
            user.updated_at = datetime.now(timezone.utc)
            session.commit()
            print(f"[OK] Пароль для пользователя '{username}' успешно обновлен!")
            return True
        else:
            # Create user if does not exist
            new_user = User(
                username=username,
                email=email,
                hashed_password=hash_password(password),
                role=UserRole.ADMIN.value,
                is_active=True,
                is_superuser=True,
                created_at=datetime.now(timezone.utc),
            )
            session.add(new_user)
            session.commit()
            print(f"[OK] Пользователь-администратор '{username}' успешно создан с указанным паролем!")
            return True


def main():
    parser = argparse.ArgumentParser(description="Смена пароля администратора WB FBS Manager")
    parser.add_argument("--username", default="admin", help="Имя пользователя (по умолчанию: admin)")
    parser.add_argument("--password", required=True, help="Новый пароль")
    parser.add_argument("--email", default="admin@example.com", help="Email администратора")
    args = parser.parse_args()

    if len(args.password) < 6:
        print("[ERROR] Пароль должен быть не менее 6 символов!")
        sys.exit(1)

    try:
        set_admin_password(args.username, args.password, args.email)
    except Exception as e:
        print(f"[ERROR] Не удалось установить пароль: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()