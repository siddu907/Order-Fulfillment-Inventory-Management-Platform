from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.constants import Role
from app.core.errors import DomainError, not_found
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.permissions import can_access_warehouse
from app.models import Order, Return, User
from app.schemas import ReturnInput
from app.services.orders import change_return_status, create_return

router = APIRouter(prefix="/returns")


@router.post("", status_code=201)
def request_return(payload: ReturnInput, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != Role.CUSTOMER.value:
        raise DomainError("FORBIDDEN", "Only customers may request returns", 403)
    result = create_return(
        db, user, payload.order_id, payload.reason,
        [line.model_dump() for line in payload.items],
    )
    db.commit()
    db.refresh(result)
    return result


@router.get("")
def list_returns(
    status: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    query = select(Return).options(selectinload(Return.items))
    if user.role == Role.CUSTOMER.value:
        query = query.where(Return.customer_id == user.id)
    elif user.role in (Role.WAREHOUSE_MANAGER.value, Role.FULFILLMENT_AGENT.value):
        warehouse_ids = [w.id for w in user.warehouses]
        query = query.join(Order, Order.id == Return.order_id).where(Order.warehouse_id.in_(warehouse_ids))
    if status:
        query = query.where(Return.status == status)
    return db.scalars(query.order_by(Return.created_at.desc()).offset((page - 1) * page_size).limit(page_size)).all()


@router.get("/{return_id}")
def get_return(return_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    returned = db.scalar(select(Return).options(selectinload(Return.items)).where(Return.id == return_id))
    if returned is None:
        raise not_found("Return")
    if user.role == Role.CUSTOMER.value and returned.customer_id != user.id:
        raise not_found("Return")
    if user.role in (Role.WAREHOUSE_MANAGER.value, Role.FULFILLMENT_AGENT.value):
        order = db.get(Order, returned.order_id)
        if not can_access_warehouse(db, user, order.warehouse_id):
            raise not_found("Return")
    return returned


@router.get("/{return_id}/items")
def return_items(return_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return get_return(return_id, user, db).items


def transition(return_id: int, next_status: str, user: User, db: Session):
    if user.role not in (Role.ADMIN.value, Role.WAREHOUSE_MANAGER.value):
        raise DomainError("FORBIDDEN", "Only administrators and warehouse managers can process returns", 403)
    returned = get_return(return_id, user, db)
    change_return_status(db, user, returned, next_status)
    if next_status == "refunded":
        order = db.get(Order, returned.order_id)
        order.status = "returned"
        if order.payment_status == "paid":
            order.payment_status = "refunded"
    db.commit()
    return returned


@router.post("/{return_id}/approve")
def approve_return(return_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return transition(return_id, "approved", user, db)


@router.post("/{return_id}/reject")
def reject_return(return_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return transition(return_id, "rejected", user, db)


@router.post("/{return_id}/receive")
def receive_return(return_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return transition(return_id, "received", user, db)


@router.post("/{return_id}/refund")
def refund_return(return_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return transition(return_id, "refunded", user, db)
