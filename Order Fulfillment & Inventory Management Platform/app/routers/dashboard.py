from datetime import datetime, time, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.constants import OrderStatus, Role
from app.core.errors import DomainError
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models import Inventory, Order, User

router = APIRouter(prefix="/dashboard")


@router.get("/admin")
def admin_dashboard(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != Role.ADMIN.value:
        raise DomainError("FORBIDDEN", "Administrator role required", 403)
    count = lambda status: db.scalar(select(func.count(Order.id)).where(Order.status == status)) or 0
    return {
        "total_orders": db.scalar(select(func.count(Order.id))) or 0,
        "pending_orders": count(OrderStatus.PENDING.value),
        "processing_orders": count(OrderStatus.PROCESSING.value),
        "completed_orders": count(OrderStatus.DELIVERED.value),
        "cancelled_orders": count(OrderStatus.CANCELLED.value),
        "total_revenue": db.scalar(select(func.coalesce(func.sum(Order.total_amount), 0)).where(Order.payment_status == "paid")),
        "low_stock_products": db.scalar(select(func.count(Inventory.id)).where(Inventory.available_quantity <= Inventory.reorder_level)) or 0,
        "out_of_stock_products": db.scalar(select(func.count(Inventory.id)).where(Inventory.available_quantity == 0)) or 0,
    }


@router.get("/warehouse")
def warehouse_dashboard(warehouse_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role not in (Role.ADMIN.value, Role.WAREHOUSE_MANAGER.value):
        raise DomainError("FORBIDDEN", "Administrator or warehouse manager role required", 403)
    if user.role != Role.ADMIN.value and warehouse_id not in {warehouse.id for warehouse in user.warehouses}:
        raise DomainError("WAREHOUSE_ACCESS_DENIED", "You are not authorized for this warehouse", 403)
    count = lambda status: db.scalar(
        select(func.count(Order.id)).where(Order.warehouse_id == warehouse_id, Order.status == status)
    ) or 0
    start = datetime.combine(datetime.now(timezone.utc).date(), time.min, timezone.utc)
    end = datetime.combine(datetime.now(timezone.utc).date(), time.max, timezone.utc)
    return {
        "warehouse_id": warehouse_id,
        "warehouse_inventory": db.scalar(select(func.count(Inventory.id)).where(Inventory.warehouse_id == warehouse_id)) or 0,
        "pending_fulfillment": count(OrderStatus.PENDING.value),
        "processing_orders": count(OrderStatus.PROCESSING.value),
        "low_stock_items": db.scalar(select(func.count(Inventory.id)).where(
            Inventory.warehouse_id == warehouse_id, Inventory.available_quantity <= Inventory.reorder_level
        )) or 0,
        "todays_shipments": db.scalar(select(func.count(Order.id)).where(
            Order.warehouse_id == warehouse_id, Order.status == OrderStatus.SHIPPED.value,
            Order.updated_at >= start, Order.updated_at <= end,
        )) or 0,
    }


@router.get("/customer")
def customer_dashboard(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role != Role.CUSTOMER.value:
        raise DomainError("FORBIDDEN", "Customer role required", 403)
    count = lambda status: db.scalar(
        select(func.count(Order.id)).where(Order.customer_id == user.id, Order.status == status)
    ) or 0
    active_statuses = [OrderStatus.PENDING.value, OrderStatus.CONFIRMED.value, OrderStatus.PROCESSING.value,
                       OrderStatus.PACKED.value, OrderStatus.SHIPPED.value]
    active = db.scalar(select(func.count(Order.id)).where(Order.customer_id == user.id, Order.status.in_(active_statuses))) or 0
    return {
        "total_orders": db.scalar(select(func.count(Order.id)).where(Order.customer_id == user.id)) or 0,
        "active_orders": active,
        "completed_orders": count(OrderStatus.DELIVERED.value),
        "cancelled_orders": count(OrderStatus.CANCELLED.value),
        "total_spending": db.scalar(select(func.coalesce(func.sum(Order.total_amount), Decimal("0"))).where(
            Order.customer_id == user.id, Order.payment_status == "paid"
        )),
    }
