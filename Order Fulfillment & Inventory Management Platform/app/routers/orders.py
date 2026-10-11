from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.constants import OrderStatus, Role
from app.core.errors import DomainError, not_found
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.permissions import require_warehouse_access
from app.models import Order, OrderAssignment, OrderStatusHistory, User
from app.schemas import AssignOrderInput, OrderInput, OrderStatusInput
from app.services.idempotency import find_replay, save_key
from app.services.orders import assign_order, change_order_status, get_order, update_expected_delivery
from app.services.payments import confirm_demo_payment

orders_router = APIRouter()
fulfillment_router = APIRouter()
payments_router = APIRouter()


@orders_router.post("/orders", status_code=201)
def create_order(
    payload: OrderInput,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    if user.role != Role.CUSTOMER.value:
        raise DomainError("FORBIDDEN", "Only customers may place orders", 403)
    from app.services.orders import create_order as service_create_order

    order = service_create_order(
        db, user, None,
        [line.model_dump(mode="json") for line in payload.items],
        idempotency_key,
    )
    db.commit()
    return db.scalar(select(Order).options(selectinload(Order.items)).where(Order.id == order.id))


@orders_router.get("/orders")
def list_orders(
    status: str | None = None, customer_id: int | None = None, warehouse_id: int | None = None,
    payment_status: str | None = None, created_from: datetime | None = None, created_to: datetime | None = None,
    min_total: Decimal | None = Query(default=None, ge=0), max_total: Decimal | None = Query(default=None, ge=0),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    sort_by: str = "created_at", sort_order: str = "desc",
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    query = select(Order).options(selectinload(Order.items))
    if user.role == Role.CUSTOMER.value:
        query = query.where(Order.customer_id == user.id)
    elif user.role in (Role.WAREHOUSE_MANAGER.value, Role.FULFILLMENT_AGENT.value):
        query = query.where(Order.warehouse_id.in_([w.id for w in user.warehouses]))
    if status:
        query = query.where(Order.status == status)
    if payment_status:
        query = query.where(Order.payment_status == payment_status)
    if customer_id is not None:
        query = query.where(Order.customer_id == customer_id)
    if warehouse_id is not None:
        if user.role != Role.ADMIN.value:
            require_warehouse_access(db, user, warehouse_id)
        query = query.where(Order.warehouse_id == warehouse_id)
    if created_from:
        query = query.where(Order.created_at >= created_from)
    if created_to:
        query = query.where(Order.created_at <= created_to)
    if min_total is not None:
        query = query.where(Order.total_amount >= min_total)
    if max_total is not None:
        query = query.where(Order.total_amount <= max_total)
    sort_column = {"created_at": Order.created_at, "total_amount": Order.total_amount, "status": Order.status}.get(sort_by)
    if sort_column is None or sort_order not in ("asc", "desc"):
        raise DomainError("INVALID_SORT", "Unsupported order sort field or direction", 422)
    query = query.order_by(sort_column.asc() if sort_order == "asc" else sort_column.desc())
    return db.scalars(query.offset((page - 1) * page_size).limit(page_size)).all()


@orders_router.get("/orders/{order_id}")
def read_order(order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return get_order(db, user, order_id)


@orders_router.get("/orders/{order_id}/items")
def get_order_items(order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = get_order(db, user, order_id)
    return order.items


@orders_router.get("/orders/{order_id}/history")
def get_order_history(order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_order(db, user, order_id)
    return db.scalars(select(OrderStatusHistory).where(OrderStatusHistory.order_id == order_id).order_by(OrderStatusHistory.created_at)).all()


@orders_router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = get_order(db, user, order_id)
    change_order_status(db, user, order, OrderStatus.CANCELLED.value)
    db.commit()
    return order


@orders_router.put("/orders/{order_id}")
def update_order_status(order_id: int, payload: OrderStatusInput, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in (Role.ADMIN.value, Role.FULFILLMENT_AGENT.value, Role.WAREHOUSE_MANAGER.value):
        raise DomainError("FORBIDDEN", "Customers cannot manage fulfillment status", 403)
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise DomainError("EMPTY_UPDATE", "Provide order status or expected delivery date to update", 422)
    order = get_order(db, user, order_id)
    if payload.status is not None:
        change_order_status(db, user, order, payload.status)
    if payload.expected_delivery_at is not None:
        update_expected_delivery(db, user, order, payload.expected_delivery_at)
    db.commit()
    return order


@orders_router.post("/orders/{order_id}/assign")
@orders_router.post("/orders/{order_id}/reassign")
def assign(order_id: int, payload: AssignOrderInput, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in (Role.ADMIN.value, Role.WAREHOUSE_MANAGER.value):
        raise DomainError("FORBIDDEN", "Only administrators and warehouse managers can assign orders", 403)
    order = get_order(db, user, order_id)
    assignment = assign_order(db, user, order, payload.agent_id)
    db.commit()
    db.refresh(assignment)
    return assignment


@fulfillment_router.get("/fulfillment/orders/pending")
def pending_orders(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in (Role.ADMIN.value, Role.WAREHOUSE_MANAGER.value, Role.FULFILLMENT_AGENT.value):
        raise DomainError("FORBIDDEN", "Fulfillment role required", 403)
    query = select(Order).where(Order.status == OrderStatus.PENDING.value)
    if user.role != Role.ADMIN.value:
        query = query.where(Order.warehouse_id.in_([w.id for w in user.warehouses]))
    return db.scalars(query.order_by(Order.created_at)).all()


@fulfillment_router.get("/fulfillment/orders/assigned-to-me")
def assigned_to_me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != Role.FULFILLMENT_AGENT.value:
        raise DomainError("FORBIDDEN", "Fulfillment agent role required", 403)
    latest_assignment = select(func.max(OrderAssignment.id)).where(
        OrderAssignment.order_id == Order.id
    ).correlate(Order).scalar_subquery()
    return db.scalars(
        select(Order).join(OrderAssignment, OrderAssignment.order_id == Order.id)
        .where(OrderAssignment.agent_id == user.id, OrderAssignment.id == latest_assignment)
        .order_by(Order.created_at)
    ).all()


def fulfillment_transition(order_id: int, status: str, user: User, db: Session):
    if user.role not in (Role.ADMIN.value, Role.WAREHOUSE_MANAGER.value, Role.FULFILLMENT_AGENT.value):
        raise DomainError("FORBIDDEN", "Fulfillment role required", 403)
    order = get_order(db, user, order_id)
    if user.role == Role.FULFILLMENT_AGENT.value:
        assigned = db.scalar(
            select(OrderAssignment.id).where(
                OrderAssignment.order_id == order_id,
                OrderAssignment.agent_id == user.id,
                OrderAssignment.id == select(func.max(OrderAssignment.id))
                .where(OrderAssignment.order_id == order_id).scalar_subquery(),
            )
        )
        if assigned is None:
            raise DomainError("FORBIDDEN", "Order is not assigned to you", 403)
    change_order_status(db, user, order, status)
    db.commit()
    return order


@fulfillment_router.post("/fulfillment/orders/{order_id}/accept")
def accept_order(order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return fulfillment_transition(order_id, OrderStatus.CONFIRMED.value, user, db)


@fulfillment_router.post("/fulfillment/orders/{order_id}/start-processing")
def start_processing(order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return fulfillment_transition(order_id, OrderStatus.PROCESSING.value, user, db)


@fulfillment_router.post("/fulfillment/orders/{order_id}/pack")
def pack_order(order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return fulfillment_transition(order_id, OrderStatus.PACKED.value, user, db)


@fulfillment_router.post("/fulfillment/orders/{order_id}/ship")
def ship_order(order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return fulfillment_transition(order_id, OrderStatus.SHIPPED.value, user, db)


@fulfillment_router.post("/fulfillment/orders/{order_id}/deliver")
def deliver_order(order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return fulfillment_transition(order_id, OrderStatus.DELIVERED.value, user, db)


@fulfillment_router.post("/fulfillment/orders/{order_id}/fail")
def fail_order(order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return fulfillment_transition(order_id, OrderStatus.FAILED.value, user, db)


@payments_router.post("/payments/{order_id}/confirm")
def confirm_payment(
    order_id: int,
    idempotency_key: str | None = Header(default=None, min_length=1, max_length=200, alias="Idempotency-Key"),
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    order = get_order(db, user, order_id)
    if user.role not in (Role.ADMIN.value, Role.CUSTOMER.value):
        raise DomainError("FORBIDDEN", "Only the customer or an administrator can confirm payment", 403)
    if user.role == Role.CUSTOMER.value and order.customer_id != user.id:
        raise not_found("Order")
    if idempotency_key is None:
        idempotency_key = f"payment-confirm-order-{order_id}"
    data = {"order_id": order_id, "payment_status": "paid"}
    replay = find_replay(db, user.id, "POST:/payments/{order_id}/confirm", idempotency_key, data)
    if replay is not None:
        return replay.response_json
    from app.repositories.orders import find_order

    order = find_order(db, order_id, lock=True)
    if order is None:
        raise not_found("Order")
    if order.payment_status == "pending":
        confirm_demo_payment(db, order, user)
    elif order.payment_status != "paid":
        raise DomainError("INVALID_PAYMENT_STATUS", "Payment cannot be confirmed in the current state", 409)
    result = {
        "order_id": order.id,
        "payment_status": order.payment_status,
        "provider": "local_demo",
    }
    save_key(db, user.id, "POST:/payments/{order_id}/confirm", idempotency_key, data, order.id, result)
    db.commit()
    return result


@payments_router.get("/payments/{order_id}")
def get_payment(order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = get_order(db, user, order_id)
    return {
        "order_id": order.id,
        "payment_status": order.payment_status,
        "total_amount": order.total_amount,
        "provider": "local_demo",
    }
