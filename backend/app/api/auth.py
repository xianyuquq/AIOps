"""认证路由。"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.core.security import (
    ALGORITHM,
    authenticate_user,
    create_access_token,
    create_refresh_token,
    get_current_user,
)
from app.db import get_db
from app.models import User
from app.schemas import LoginRequest, TokenResponse, UserCreate, UserOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["认证"])


def _issue_tokens(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(user),
        refresh_token=create_refresh_token(user),
        username=user.username,
        role=user.role,
    )


@router.post("/login", response_model=TokenResponse, summary="登录（表单）")
def login_form(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Annotated[Session, Depends(get_db)],
) -> TokenResponse:
    user = authenticate_user(db, form.username, form.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )
    return _issue_tokens(user)


@router.post("/login/json", response_model=TokenResponse, summary="登录（JSON）")
def login_json(
    payload: LoginRequest,
    db: Annotated[Session, Depends(get_db)],
) -> TokenResponse:
    user = authenticate_user(db, payload.username, payload.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )
    return _issue_tokens(user)


@router.post("/refresh", response_model=TokenResponse, summary="刷新令牌")
def refresh(
    refresh_token: str,
    db: Annotated[Session, Depends(get_db)],
) -> TokenResponse:
    try:
        payload = jwt.decode(refresh_token, __import__("app.config", fromlist=["settings"]).settings.secret_key, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="刷新令牌无效") from None
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="令牌类型错误")
    user = db.get(User, int(payload.get("sub", 0)))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="用户不存在或已禁用")
    return _issue_tokens(user)


@router.get("/me", response_model=UserOut, summary="当前用户")
def me(user: Annotated[User, Depends(get_current_user)]) -> UserOut:
    return UserOut.model_validate(user)


@router.get("/users", response_model=list[UserOut], summary="用户列表")
def list_users(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[UserOut]:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return [UserOut.model_validate(u) for u in db.query(User).all()]


@router.post("/users", response_model=UserOut, summary="创建用户")
def create_user(
    payload: UserCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> UserOut:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    if db.query(User).filter(User.username == payload.username).first():
        raise HTTPException(status_code=400, detail="用户名已存在")

    from app.core.crypto import hash_password

    new_user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=payload.role,
        display_name=payload.display_name or payload.username,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return UserOut.model_validate(new_user)
