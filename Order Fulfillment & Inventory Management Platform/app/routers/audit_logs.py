from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constants import Role
from app.core.errors import DomainError, not_found
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models import AuditLog, User

router = APIRouter(prefix="/audit-logs")


def require_admin(user: User) -> None:
    if user.role != Role.ADMIN.value:
        raise DomainError("FORBIDDEN", "Administrator role required", 403)


@router.get("")
def list_audit_logs(
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
    action: str | None = None, entity: str | None = None,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    require_admin(user)
    query = select(AuditLog)
    if action:
        query = query.where(AuditLog.action == action)
    if entity:
        query = query.where(AuditLog.entity == entity)
    return db.scalars(query.order_by(AuditLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size)).all()


@router.get("/{audit_id}")
def get_audit_log(audit_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    require_admin(user)
    log = db.get(AuditLog, audit_id)
    if log is None:
        raise not_found("Audit log")
    return log
