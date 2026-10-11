from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.core.security import decode_access_token
from app.database import get_db
from app.models import User

bearer_scheme = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    try:
        payload = decode_access_token(credentials.credentials)
        user_id = int(payload["sub"])
    except (ValueError, KeyError, TypeError, JWTError) as exc:
        raise DomainError("UNAUTHORIZED", "Invalid or expired access token", 401) from exc
    user = db.scalar(select(User).where(User.id == user_id))
    if (
        user is None
        or not user.is_active
        or payload.get("token_version") != user.token_version
    ):
        raise DomainError("UNAUTHORIZED", "User is unavailable", 401)
    return user
