"""
Authentication Service — Password hashing, JWT token handling, and user authentication
"""
import os
import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

from jose import jwt, JWTError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_

try:
    import bcrypt
    HAS_BCRYPT = True
except ImportError:
    HAS_BCRYPT = False

from app.config import settings
from app.models.user import User, UserRole
from app.schemas.auth import TokenPayload

logger = logging.getLogger(__name__)


def hash_password(password: str) -> str:
    """Hash plain text password safely using native bcrypt with PBKDF2 fallback."""
    if not password:
        return ""
    if HAS_BCRYPT:
        try:
            pwd_bytes = password.encode("utf-8")[:72]
            salt = bcrypt.gensalt(rounds=12)
            return bcrypt.hashpw(pwd_bytes, salt).decode("utf-8")
        except Exception as exc:
            logger.warning(f"Native bcrypt hashing failed: {exc}, using PBKDF2 fallback.")

    salt = os.urandom(16).hex()
    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100000
    ).hex()
    return f"pbkdf2_fallback${salt}${key}"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify plain password against hashed password (supports bcrypt, passlib, and pbkdf2)."""
    if not hashed_password or not plain_password:
        return False

    if hashed_password.startswith("pbkdf2_fallback$"):
        try:
            _, salt, expected_key = hashed_password.split("$")
            computed_key = hashlib.pbkdf2_hmac(
                "sha256",
                plain_password.encode("utf-8"),
                salt.encode("utf-8"),
                100000
            ).hex()
            return hmac.compare_digest(computed_key, expected_key)
        except Exception:
            return False

    # Check bcrypt hash format ($2a$, $2b$, $2y$)
    if hashed_password.startswith(("$2a$", "$2b$", "$2y$")) and HAS_BCRYPT:
        try:
            pwd_bytes = plain_password.encode("utf-8")[:72]
            hash_bytes = hashed_password.encode("utf-8")
            return bcrypt.checkpw(pwd_bytes, hash_bytes)
        except Exception as exc:
            logger.debug(f"Native bcrypt verification failed: {exc}")

    # Fallback to passlib if hash was generated with another scheme
    try:
        from passlib.context import CryptContext
        pwd_context = CryptContext(schemes=["bcrypt", "pbkdf2_sha256"], deprecated="auto")
        return pwd_context.verify(plain_password, hashed_password)
    except Exception:
        return False


def create_access_token(
    data: Dict[str, Any],
    expires_delta: Optional[timedelta] = None
) -> str:
    """Generate signed JWT access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_access_token_expire_minutes)

    to_encode.update({"exp": int(expire.timestamp())})
    encoded_jwt = jwt.encode(
        to_encode,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm
    )
    return encoded_jwt


def decode_access_token(token: str) -> Optional[TokenPayload]:
    """Decode and validate JWT access token."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm]
        )
        user_id: str = payload.get("sub")
        username: str = payload.get("username")
        role: str = payload.get("role", UserRole.ADMIN.value)
        is_superuser: bool = payload.get("is_superuser", False)
        exp: Optional[int] = payload.get("exp")

        if user_id is None or username is None:
            return None

        return TokenPayload(
            sub=user_id,
            username=username,
            role=role,
            is_superuser=is_superuser,
            exp=exp
        )
    except (JWTError, ValueError) as e:
        logger.debug(f"JWT decode error: {e}")
        return None


async def get_user_by_username(db: AsyncSession, username_or_email: str) -> Optional[User]:
    """Find user by username or email."""
    result = await db.execute(
        select(User).where(
            or_(
                User.username == username_or_email,
                User.email == username_or_email
            )
        )
    )
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: str) -> Optional[User]:
    """Find user by ID."""
    return await db.get(User, user_id)


async def authenticate_user(
    db: AsyncSession,
    username_or_email: str,
    password: str
) -> Optional[User]:
    """Authenticate user with username/email and password."""
    user = await get_user_by_username(db, username_or_email)
    if not user:
        return None
    if not user.is_active:
        return None
    if not verify_password(password, user.hashed_password):
        return None

    # Update last login timestamp
    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(user)
    return user


async def ensure_initial_admin(db: AsyncSession) -> Optional[User]:
    """Bootstrap default admin account if no users exist in database."""
    res = await db.execute(select(User))
    first_user = res.scalars().first()
    if first_user:
        return first_user

    admin_username = settings.admin_username or "admin"
    admin_password = settings.admin_password or "admin_password"
    admin_email = settings.admin_email or "admin@example.com"

    logger.info(f"Bootstrapping default admin user: '{admin_username}'")
    admin_user = User(
        username=admin_username,
        email=admin_email,
        hashed_password=hash_password(admin_password),
        role=UserRole.ADMIN.value,
        is_active=True,
        is_superuser=True
    )
    db.add(admin_user)
    await db.commit()
    await db.refresh(admin_user)
    return admin_user