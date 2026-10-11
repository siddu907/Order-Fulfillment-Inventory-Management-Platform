import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.models import IdempotencyKey


def request_digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def find_replay(db: Session, user_id: int, endpoint: str, key: str | None, payload: dict) -> IdempotencyKey | None:
    if not key:
        return None
    existing = db.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.user_id == user_id,
            IdempotencyKey.endpoint == endpoint,
            IdempotencyKey.key == key,
        )
    )
    if existing is not None and existing.request_hash != request_digest(payload):
        raise DomainError("DUPLICATE_IDEMPOTENCY_KEY", "Idempotency key was already used for a different request", 409)
    return existing


def save_key(db: Session, user_id: int, endpoint: str, key: str | None, payload: dict,
             resource_id: int, response: dict) -> None:
    if key:
        db.add(
            IdempotencyKey(
                user_id=user_id, endpoint=endpoint, key=key,
                request_hash=request_digest(payload), resource_id=resource_id, response_json=response,
            )
        )
