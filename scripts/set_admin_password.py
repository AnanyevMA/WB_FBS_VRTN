#!/usr/bin/env python3
"""
Set or Reset Admin Password for WB FBS Manager
Смена или восстановление пароля администратора в базе данных и .env без потери данных.

Использование:
  python3 scripts/set_admin_password.py --username admin --password 'ваш_новый_пароль'
  docker compose -f docker-compose.prod.yml exec api python scripts/set_admin_password.py --username admin
"""
import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure UTF-8 output on Windows / Linux consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

# Add current directory to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings
from app.models.user import User, UserRole
from app.services.auth_service import hash_password, sync_env_admin_password


def is_running_in_docker() -> bool:
    """Check if current execution is inside a Docker container."""
    return os.path.exists("/.dockerenv") or os.environ.get("RUNNING_IN_DOCKER") == "1"


def try_docker_compose_forward(username: str, password: str, email: str) -> bool:
    """If running on host with active Docker containers, forward command into the API container."""
    if is_running_in_docker():
        return False

    docker_bin = shutil.which("docker")
    if not docker_bin:
        return False

    # Check if docker-compose.prod.yml exists and api container is running
    compose_file = PROJECT_ROOT / "docker-compose.prod.yml"
    if not compose_file.exists():
        return False

    try:
        ps_proc = subprocess.run(
            [docker_bin, "compose", "-f", str(compose_file), "ps", "--services", "--filter", "status=running"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(PROJECT_ROOT)
        )
        if "api" in ps_proc.stdout.split():
            print("[INFO] Обнаружен работающий Docker контейнер API. Выполняю смену пароля в PostgreSQL контейнера...")
            exec_proc = subprocess.run(
                [
                    docker_bin, "compose", "-f", str(compose_file), "exec", "-T", "api",
                    "python", "scripts/set_admin_password.py", "--direct",
                    "--username", username, "--password", password, "--email", email
                ],
                text=True,
                cwd=str(PROJECT_ROOT)
            )
            if exec_proc.returncode == 0:
                # Also sync local .env on host
                sync_env_admin_password(password)
                print(f"[OK] Пароль успешно обновлен в работающем Docker контейнере и локальном .env!")
                return True
            else:
                print("[WARN] Не удалось выполнить смену пароля через Docker exec, пробую прямое подключение...")
    except Exception as e:
        print(f"[WARN] Ошибка при проверке Docker Compose: {e}")

    return False


def set_admin_password_direct(username: str, password: str, email: str = "admin@example.com") -> bool:
    """Directly update or create admin user with the specified password in the database."""
    from sqlalchemy import create_engine, select, func, or_
    from sqlalchemy.orm import Session

    cleaned_uname = (username or "").strip()
    cleaned_pwd = (password or "").strip()
    db_url = settings.database_url_sync

    print(f"[INFO] Подключение к базе данных: {db_url.split('@')[-1] if '@' in db_url else db_url}")
    sync_engine = create_engine(db_url)
    with Session(sync_engine) as session:
        user = session.execute(
            select(User).where(
                or_(
                    func.lower(User.username) == cleaned_uname.lower(),
                    func.lower(User.email) == cleaned_uname.lower()
                )
            )
        ).scalar_one_or_none()

        if user:
            user.hashed_password = hash_password(cleaned_pwd)
            user.is_active = True
            user.is_superuser = True
            user.must_change_password = False
            user.updated_at = datetime.now(timezone.utc)
            session.commit()
            print(f"[OK] Пароль для пользователя '{user.username}' успешно обновлен в базе данных!")
        else:
            # Create user if does not exist
            new_user = User(
                username=cleaned_uname,
                email=email,
                hashed_password=hash_password(cleaned_pwd),
                role=UserRole.ADMIN.value,
                is_active=True,
                is_superuser=True,
                must_change_password=False,
                created_at=datetime.now(timezone.utc),
            )
            session.add(new_user)
            session.commit()
            print(f"[OK] Пользователь-администратор '{cleaned_uname}' успешно создан с указанным паролем!")

    # Synchronize .env file so credentials are never out of sync
    if sync_env_admin_password(cleaned_pwd):
        print(f"[OK] Файл конфигурации .env успешно синхронизирован (ADMIN_PASSWORD).")

    return True


def main():
    parser = argparse.ArgumentParser(description="Смена пароля администратора WB FBS Manager")
    parser.add_argument("--username", default="admin", help="Имя пользователя (по умолчанию: admin)")
    parser.add_argument("--password", required=False, help="Новый пароль (если не указан, будет запрошен безопасно)")
    parser.add_argument("--email", default="admin@example.com", help="Email администратора")
    parser.add_argument("--direct", action="store_true", help="Прямое обновление БД без перенаправления в Docker")
    args = parser.parse_args()

    pwd = args.password
    if not pwd:
        try:
            pwd = input(f"Введите новый пароль для '{args.username}': ").strip()
        except Exception:
            print("[ERROR] Не удалось прочитать пароль!")
            sys.exit(1)

    if len(pwd) < 6:
        print("[ERROR] Пароль должен быть не менее 6 символов!")
        sys.exit(1)

    if not args.direct and try_docker_compose_forward(args.username, pwd, args.email):
        return

    try:
        set_admin_password_direct(args.username, pwd, args.email)
    except Exception as e:
        print(f"[ERROR] Не удалось установить пароль: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()