"""
Authentication Router & Dependencies — JWT Login, Current User, and User Management
"""
from typing import List, Optional
from datetime import timedelta
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.config import settings
from app.models.user import User, UserRole
from app.schemas.auth import (
    LoginRequest,
    Token,
    UserResponse,
    UserCreate,
    PasswordChangeRequest,
    TokenPayload
)
from app.services.auth_service import (
    authenticate_user,
    create_access_token,
    decode_access_token,
    get_user_by_id,
    get_user_by_username,
    hash_password,
    verify_password
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])
security = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_db)
) -> User:
    """Dependency: Validate JWT Bearer token and return current User instance."""
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token is required",
            headers={"WWW-Authenticate": "Bearer"}
        )

    token_payload: Optional[TokenPayload] = decode_access_token(credentials.credentials)
    if not token_payload or not token_payload.sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token",
            headers={"WWW-Authenticate": "Bearer"}
        )

    user = await get_user_by_id(db, token_payload.sub)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User associated with token not found",
            headers={"WWW-Authenticate": "Bearer"}
        )

    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user)
) -> User:
    """Dependency: Ensure the current user account is active."""
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is deactivated"
        )
    return current_user


async def require_admin(
    current_user: User = Depends(get_current_active_user)
) -> User:
    """Dependency: Restrict endpoint to Admin and Superusers."""
    if current_user.role != UserRole.ADMIN.value and not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required"
        )
    return current_user


@router.post("/login", response_model=Token)
async def login(
    login_data: LoginRequest,
    db: AsyncSession = Depends(get_db)
):
    """Authenticate user with username/email and password, return JWT token."""
    logger.info(f"LOGIN ATTEMPT: username='{login_data.username}', password_len={len(login_data.password)}")
    user = await authenticate_user(db, login_data.username.strip(), login_data.password)
    if not user:
        logger.warning(f"LOGIN FAILED: username='{login_data.username}'")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверное имя пользователя или пароль",
            headers={"WWW-Authenticate": "Bearer"}
        )

    logger.info(f"LOGIN SUCCESS: username='{user.username}', id='{user.id}'")
    access_token_expires = timedelta(minutes=settings.jwt_access_token_expire_minutes)
    token_jwt = create_access_token(
        data={
            "sub": user.id,
            "username": user.username,
            "role": user.role,
            "is_superuser": user.is_superuser,
            "must_change_password": user.must_change_password
        },
        expires_delta=access_token_expires
    )

    return {
        "access_token": token_jwt,
        "token_type": "bearer",
        "expires_in": int(access_token_expires.total_seconds()),
        "user": user
    }




@router.get("/me", response_model=UserResponse)
async def get_current_user_profile(
    current_user: User = Depends(get_current_active_user)
):
    """Get current authenticated user profile."""
    return current_user


@router.post("/change-password")
async def change_password(
    data: PasswordChangeRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """Change current user's password and save permanently in database."""
    old_valid = verify_password(data.old_password, current_user.hashed_password)
    if not old_valid and (current_user.is_superuser or current_user.role == UserRole.ADMIN.value):
        env_admin_pwd = (settings.admin_password or "").strip()
        if env_admin_pwd and data.old_password == env_admin_pwd:
            old_valid = True

    if not old_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Текущий пароль указан неверно"
        )

    current_user.hashed_password = hash_password(data.new_password)
    current_user.must_change_password = False
    await db.commit()
    await db.refresh(current_user)

    # If this is admin, sync .env file and runtime settings so deploy/restart never loses it
    if current_user.is_superuser or current_user.role == UserRole.ADMIN.value:
        from app.services.auth_service import sync_env_admin_password
        settings.admin_password = data.new_password
        sync_env_admin_password(data.new_password)

    return {
        "success": True, 
        "message": "Пароль успешно изменен и сохранен в базе данных",
        "user": current_user
    }


@router.get("/users", response_model=List[UserResponse])
async def list_users(
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin)
):
    """List all registered system users (Admin only)."""
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    return result.scalars().all()


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user_by_admin(
    user_in: UserCreate,
    db: AsyncSession = Depends(get_db),
    admin_user: User = Depends(require_admin)
):
    """Create a new operator / user (Admin only)."""
    existing_user = await get_user_by_username(db, user_in.username)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Пользователь с таким именем уже существует"
        )

    user = User(
        username=user_in.username.strip(),
        email=user_in.email,
        hashed_password=hash_password(user_in.password),
        role=user_in.role,
        is_active=user_in.is_active,
        is_superuser=user_in.is_superuser,
        must_change_password=user_in.must_change_password
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user
