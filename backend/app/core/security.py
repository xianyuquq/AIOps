"""认证与授权：JWT 签发、密码校验、RBAC 依赖。"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.config import settings
from app.core.crypto import verify_password
from app.db import get_db
from app.models import User

ALGORITHM = "HS256"

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)

Role = Literal["admin", "ops", "viewer"]

# 角色权限等级：数值越大权限越高
_ROLE_LEVEL = {"viewer": 1, "ops": 2, "admin": 3}


def create_access_token(user: User) -> str:
    expire = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
        minutes=settings.access_token_expire_minutes
    )
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "type": "access",
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def create_refresh_token(user: User) -> str:
    expire = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
        days=settings.refresh_token_expire_days
    )
    payload = {
        "sub": str(user.id),
        "type": "refresh",
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def authenticate_user(db: Session, username: str, password: str) -> User | None:
    user = db.query(User).filter(User.username == username).first()
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


async def get_current_user(
    token: Annotated[str | None, Depends(oauth2_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="登录状态无效或已过期",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_error
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except JWTError:
        raise credentials_error from None
    if payload.get("type") != "access":
        raise credentials_error
    user_id = payload.get("sub")
    if user_id is None:
        raise credentials_error
    user = db.get(User, int(user_id))
    if user is None or not user.is_active:
        raise credentials_error
    return user


def require_role(minimum: Role):
    """生成一个要求最低角色等级的依赖。

    用法：Depends(require_role("ops"))
    """

    async def _checker(user: Annotated[User, Depends(get_current_user)]) -> User:
        required = _ROLE_LEVEL[minimum]
        actual = _ROLE_LEVEL.get(user.role, 0)
        if actual < required:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"需要 {minimum} 及以上权限",
            )
        return user

    return _checker


CurrentUser = Annotated[User, Depends(get_current_user)]
OperatorUser = Annotated[User, Depends(require_role("ops"))]
AdminUser = Annotated[User, Depends(require_role("admin"))]
