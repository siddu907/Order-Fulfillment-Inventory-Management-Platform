from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constants import Role
from app.core.errors import forbidden
from app.models import User, Warehouse


def require_roles(*roles: Role) -> Callable:
    allowed = {role.value for role in roles}

    def check(user: User) -> User:
        if user.role not in allowed:
            raise forbidden()
        return user

    return check


def can_access_warehouse(db: Session, user: User, warehouse_id: int) -> bool:
    if user.role == Role.ADMIN.value:
        return True
    return db.scalar(
        select(Warehouse.id)
        .join(Warehouse.users)
        .where(Warehouse.id == warehouse_id, User.id == user.id)
    ) is not None


def require_warehouse_access(db: Session, user: User, warehouse_id: int) -> None:
    if user.role not in (Role.ADMIN.value, Role.WAREHOUSE_MANAGER.value, Role.FULFILLMENT_AGENT.value):
        raise forbidden("WAREHOUSE_ACCESS_DENIED", "This role cannot access warehouse resources")
    if not can_access_warehouse(db, user, warehouse_id):
        raise forbidden("WAREHOUSE_ACCESS_DENIED", "You are not authorized for this warehouse")
