from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models import AuditLog, RefreshToken, User
from app.schemas import ChangePasswordInput, LoginInput, RefreshInput, RegisterInput, TokenOutput, UserOutput
from app.services.common import audit

router = APIRouter()


def issue_tokens(db: Session, user: User) -> TokenOutput:
    access, expires = create_access_token(user.id, user.role, user.token_version)
    refresh, refresh_expires = create_refresh_token()
    db.add(RefreshToken(user_id=user.id, token_hash=hash_token(refresh), expires_at=refresh_expires))
    return TokenOutput(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires.strftime("%Y-%m-%d %I:%M %p UTC"),
        user=UserOutput.model_validate(user),
    )


@router.post("/register", response_model=UserOutput, status_code=201)
def register(payload: RegisterInput, request: Request, db: Session = Depends(get_db)):
    if db.scalar(select(User.id).where(User.email == payload.email.lower())) is not None:
        raise DomainError("EMAIL_ALREADY_REGISTERED", "Email address is already registered", 409)
    user = User(email=payload.email.lower(), full_name=payload.full_name, password_hash=hash_password(payload.password), role="customer")
    db.add(user)
    db.flush()
    audit(db, user.id, "user_registered", "user", user.id, ip_address=request.client.host if request.client else None)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=TokenOutput)
def login(payload: LoginInput, request: Request, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    ip = request.client.host if request.client else None
    if user is None or not verify_password(payload.password, user.password_hash):
        db.add(AuditLog(user_id=user.id if user else None, action="login_failed", entity="user",
                        entity_id=str(user.id) if user else None, ip_address=ip, metadata_json={"email": payload.email}))
        db.commit()
        raise DomainError("INVALID_CREDENTIALS", "Invalid email or password", 401)
    if not user.is_active:
        audit(db, user.id, "login_failed", "user", user.id, {"reason": "inactive"}, ip)
        db.commit()
        raise DomainError("ACCOUNT_INACTIVE", "Inactive users cannot authenticate", 401)
    audit(db, user.id, "login", "user", user.id, ip_address=ip)
    result = issue_tokens(db, user)
    db.commit()
    return result


@router.post("/refresh", response_model=TokenOutput)
def refresh(payload: RefreshInput, db: Session = Depends(get_db)):
    token_record = db.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(payload.refresh_token)).with_for_update()
    )
    now = datetime.now(timezone.utc)
    if token_record is None or token_record.revoked_at is not None:
        raise DomainError("INVALID_REFRESH_TOKEN", "Refresh token is invalid or revoked", 401)
    expiry = token_record.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry <= now:
        token_record.revoked_at = now
        db.commit()
        raise DomainError("INVALID_REFRESH_TOKEN", "Refresh token has expired", 401)
    user = db.get(User, token_record.user_id)
    if user is None or not user.is_active:
        token_record.revoked_at = now
        db.commit()
        raise DomainError("ACCOUNT_INACTIVE", "Inactive users cannot authenticate", 401)
    token_record.revoked_at = now
    result = issue_tokens(db, user)
    db.commit()
    return result


@router.post("/logout")
def logout(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tokens = db.scalars(
        select(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .with_for_update()
    )
    now = datetime.now(timezone.utc)
    for token in tokens:
        token.revoked_at = now
    user.token_version += 1
    audit(db, user.id, "logout", "user", user.id)
    db.commit()
    return {"message": "Logged out successfully"}

@router.get("/me", response_model=UserOutput)
def me(user: User = Depends(get_current_user)):
    return user


@router.post("/change-password")
def change_password(payload: ChangePasswordInput, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    user.password_hash = hash_password(payload.new_password)
    user.token_version += 1
    for token in db.scalars(select(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))):
        token.revoked_at = datetime.now(timezone.utc)
    audit(db, user.id, "password_changed", "user", user.id)
    db.commit()
    return {"message": "Password changed successfully"}
