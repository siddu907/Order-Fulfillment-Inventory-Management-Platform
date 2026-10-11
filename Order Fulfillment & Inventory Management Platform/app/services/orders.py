import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.constants import OrderStatus, ReturnStatus, Role
from app.core.errors import DomainError, forbidden, not_found
from app.dependencies.permissions import can_access_warehouse
from app.models import (
    IdempotencyKey,
    Inventory,
    Order,
    OrderAssignment,
    OrderItem,
    OrderStatusHistory,
    Product,
    Return,
    ReturnItem,
    TaxConfiguration,
    User,
    Warehouse,
)
from app.repositories.orders import find_order
from app.services.common import audit, notify
from app.services.inventory import record_transaction, release_stock, reserve_stock
from app.core.constants import InventoryTransactionType

ORDER_TRANSITIONS = {
    OrderStatus.PENDING.value: {OrderStatus.CONFIRMED.value, OrderStatus.CANCELLED.value, OrderStatus.FAILED.value},
    OrderStatus.CONFIRMED.value: {OrderStatus.PROCESSING.value, OrderStatus.CANCELLED.value, OrderStatus.FAILED.value},
    OrderStatus.PROCESSING.value: {OrderStatus.PACKED.value, OrderStatus.FAILED.value, OrderStatus.CANCELLED.value},
    OrderStatus.PACKED.value: {OrderStatus.SHIPPED.value, OrderStatus.FAILED.value},
    OrderStatus.SHIPPED.value: {OrderStatus.DELIVERED.value, OrderStatus.RETURNED.value},
    OrderStatus.DELIVERED.value: {OrderStatus.RETURNED.value},
    OrderStatus.CANCELLED.value: set(),
    OrderStatus.FAILED.value: set(),
    OrderStatus.RETURNED.value: set(),
}
RETURN_TRANSITIONS = {
    ReturnStatus.REQUESTED.value: {ReturnStatus.APPROVED.value, ReturnStatus.REJECTED.value},
    ReturnStatus.APPROVED.value: {ReturnStatus.RECEIVED.value},
    ReturnStatus.RECEIVED.value: {ReturnStatus.REFUNDED.value},
    ReturnStatus.REJECTED.value: set(),
    ReturnStatus.REFUNDED.value: set(),
}
EXPECTED_DELIVERY_DAYS = 5


def _select_fulfillment_warehouse(db: Session, lines: list[dict]) -> Warehouse:
    required_by_product: dict[int, int] = {}
    for line in lines:
        product_id = line["product_id"]
        required_by_product[product_id] = required_by_product.get(product_id, 0) + line["quantity"]

    products = {
        product.id: product
        for product in db.scalars(select(Product).where(Product.id.in_(required_by_product))).all()
    }
    for product_id in required_by_product:
        product = products.get(product_id)
        if product is None:
            raise not_found("Product")
        if not product.is_active:
            raise DomainError("PRODUCT_INACTIVE", f"Product {product.sku} is inactive", 409)

    warehouses = db.scalars(
        select(Warehouse).where(Warehouse.is_active.is_(True)).order_by(Warehouse.id)
    ).all()
    for warehouse in warehouses:
        balances = db.scalars(
            select(Inventory)
            .where(
                Inventory.warehouse_id == warehouse.id,
                Inventory.product_id.in_(required_by_product),
            )
            .order_by(Inventory.product_id)
            .with_for_update()
        ).all()
        available_by_product = {balance.product_id: balance.available_quantity for balance in balances}
        if all(
            available_by_product.get(product_id, 0) >= quantity
            for product_id, quantity in required_by_product.items()
        ):
            return warehouse

    raise DomainError(
        "INSUFFICIENT_INVENTORY",
        "No active warehouse has enough available stock for all items in this order",
        409,
    )


def create_order(
    db: Session,
    customer: User,
    warehouse_id: int | None,
    lines: list[dict],
    idempotency_key: str | None = None,
) -> Order:
    payload = json.dumps(
        {"items": lines},
        sort_keys=True, default=str,
    )
    request_hash = hashlib.sha256(payload.encode()).hexdigest()
    if idempotency_key:
        prior = db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.user_id == customer.id,
                IdempotencyKey.endpoint == "POST:/orders",
                IdempotencyKey.key == idempotency_key,
            )
        )
        if prior:
            if prior.request_hash != request_hash:
                raise DomainError("DUPLICATE_IDEMPOTENCY_KEY", "Idempotency key was already used for a different request", 409)
            return db.scalar(select(Order).options(selectinload(Order.items)).where(Order.id == prior.resource_id))

    warehouse = _select_fulfillment_warehouse(db, lines) if warehouse_id is None else db.get(Warehouse, warehouse_id)
    if warehouse is None:
        raise not_found("Warehouse")
    if not warehouse.is_active:
        raise DomainError("WAREHOUSE_INACTIVE", "Orders cannot be placed at an inactive warehouse", 409)
    warehouse_id = warehouse.id
    expected_delivery_at = datetime.now(timezone.utc) + timedelta(days=EXPECTED_DELIVERY_DAYS)
    order = Order(
        order_number=f"ORD-{secrets.token_hex(6).upper()}",
        customer_id=customer.id,
        warehouse_id=warehouse_id,
        total_amount=Decimal("0"),
        status=OrderStatus.PENDING.value,
        expected_delivery_at=expected_delivery_at,
    )
    db.add(order)
    db.flush()
    total = Decimal("0")
    tax_configuration = db.scalar(
        select(TaxConfiguration).where(TaxConfiguration.id == 1).with_for_update()
    )
    tax_rate = tax_configuration.rate_percent if tax_configuration is not None else Decimal("0")
    for line in sorted(lines, key=lambda item: item["product_id"]):
        product = db.scalar(select(Product).where(Product.id == line["product_id"]).with_for_update())
        if product is None:
            raise not_found("Product")
        if not product.is_active:
            raise DomainError("PRODUCT_INACTIVE", f"Product {product.sku} is inactive", 409)
        qty = line["quantity"]
        discount = Decimal(str(line.get("discount", 0)))
        subtotal = product.price * qty
        if discount > subtotal:
            raise DomainError("INVALID_ORDER_AMOUNT", "Discount cannot exceed line subtotal", 422)
        taxable_amount = subtotal - discount
        tax = (taxable_amount * tax_rate / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        line_total = subtotal - discount + tax
        total += line_total
        reserve_stock(db, product.id, warehouse_id, qty, customer.id, order.id)
        db.add(OrderItem(
            order_id=order.id, product_id=product.id, quantity=qty,
            unit_price=product.price, discount=discount, tax=tax, tax_rate=tax_rate, total=line_total,
        ))
    order.total_amount = total
    db.add(OrderStatusHistory(order_id=order.id, from_status=None, to_status=OrderStatus.PENDING.value, user_id=customer.id))
    notify(db, customer.id, "order_created", f"Order {order.order_number} was created", "order", order.id)
    audit(db, customer.id, "order_created", "order", order.id, {"order_number": order.order_number})
    if idempotency_key:
        db.add(IdempotencyKey(
            user_id=customer.id, endpoint="POST:/orders", key=idempotency_key,
            request_hash=request_hash, resource_id=order.id,
        ))
    db.flush()
    return order


def get_order(db: Session, user: User, order_id: int) -> Order:
    order = db.scalar(select(Order).options(selectinload(Order.items)).where(Order.id == order_id))
    if order is None:
        raise not_found("Order")
    if user.role == Role.CUSTOMER.value and order.customer_id != user.id:
        raise not_found("Order")
    if user.role in (Role.WAREHOUSE_MANAGER.value, Role.FULFILLMENT_AGENT.value) and not can_access_warehouse(
        db, user, order.warehouse_id
    ):
        raise not_found("Order")
    return order


def change_order_status(db: Session, user: User, order: Order, next_status: str) -> Order:
    order = find_order(db, order.id, lock=True)
    if order is None:
        raise not_found("Order")
    current = order.status
    if next_status not in ORDER_TRANSITIONS.get(current, set()):
        raise DomainError("INVALID_ORDER_STATUS", f"Cannot change order from {current} to {next_status}", 409)
    if next_status == OrderStatus.CANCELLED.value:
        if user.role == Role.CUSTOMER.value and order.customer_id != user.id:
            raise not_found("Order")
        if current not in (OrderStatus.PENDING.value, OrderStatus.CONFIRMED.value):
            raise DomainError("INVALID_ORDER_STATUS", "This order can no longer be cancelled", 409)
        for item in order.items:
            release_stock(db, item.product_id, order.warehouse_id, item.quantity, user.id, order.id)
        notify(db, order.customer_id, "order_cancelled", f"Order {order.order_number} was cancelled", "order", order.id)
        audit(db, user.id, "order_cancelled", "order", order.id)
    elif next_status == OrderStatus.FAILED.value and current not in (
        OrderStatus.SHIPPED.value, OrderStatus.DELIVERED.value
    ):
        for item in order.items:
            release_stock(db, item.product_id, order.warehouse_id, item.quantity, user.id, order.id)
        notify(db, order.customer_id, "order_failed", f"Order {order.order_number} could not be fulfilled", "order", order.id)
    elif next_status == OrderStatus.SHIPPED.value:
        from app.services.inventory import consume_reserved_stock

        for item in order.items:
            consume_reserved_stock(db, item.product_id, order.warehouse_id, item.quantity, user.id, order.id)
    elif next_status == OrderStatus.CONFIRMED.value:
        notify(db, order.customer_id, "order_confirmed", f"Order {order.order_number} was confirmed", "order", order.id)
    order.status = next_status
    db.add(OrderStatusHistory(order_id=order.id, from_status=current, to_status=next_status, user_id=user.id))
    audit(db, user.id, "order_status_changed", "order", order.id, {"from": current, "to": next_status})
    if next_status in (OrderStatus.SHIPPED.value, OrderStatus.DELIVERED.value):
        notify(db, order.customer_id, f"order_{next_status}", f"Order {order.order_number} is {next_status}", "order", order.id)
    return order


def update_expected_delivery(db: Session, user: User, order: Order, expected_delivery_at) -> Order:
    if expected_delivery_at.tzinfo is None:
        raise DomainError("INVALID_DELIVERY_DATE", "Expected delivery date must include a timezone", 422)
    if expected_delivery_at <= datetime.now(timezone.utc):
        raise DomainError("INVALID_DELIVERY_DATE", "Expected delivery date must be in the future", 422)

    previous_expected_delivery_at = order.expected_delivery_at
    if previous_expected_delivery_at == expected_delivery_at:
        return order

    order.expected_delivery_at = expected_delivery_at
    audit(
        db,
        user.id,
        "order_delivery_date_updated",
        "order",
        order.id,
        {
            "previous_expected_delivery_at": (
                previous_expected_delivery_at.isoformat() if previous_expected_delivery_at else None
            ),
            "expected_delivery_at": expected_delivery_at.isoformat(),
        },
    )
    notify(
        db,
        order.customer_id,
        "order_delivery_date_updated",
        f"Expected delivery for order {order.order_number} is now {expected_delivery_at.isoformat()}",
        "order",
        order.id,
    )
    return order


def assign_order(db: Session, user: User, order: Order, agent_id: int) -> OrderAssignment:
    agent = db.get(User, agent_id)
    if agent is None or agent.role != Role.FULFILLMENT_AGENT.value or not agent.is_active:
        raise DomainError("INVALID_AGENT", "An active fulfillment agent is required", 422)
    if not can_access_warehouse(db, agent, order.warehouse_id):
        raise DomainError("WAREHOUSE_ACCESS_DENIED", "Agent is not authorized for this warehouse", 403)
    if not can_access_warehouse(db, user, order.warehouse_id) and user.role != Role.ADMIN.value:
        raise forbidden("WAREHOUSE_ACCESS_DENIED", "You are not authorized for this warehouse")
    assignment = OrderAssignment(order_id=order.id, agent_id=agent.id, assigned_by_id=user.id)
    db.add(assignment)
    notify(db, agent.id, "order_assigned", f"Order {order.order_number} was assigned to you", "order", order.id)
    audit(db, user.id, "order_assigned", "order", order.id, {"agent_id": agent.id})
    return assignment


def create_return(db: Session, user: User, order_id: int, reason: str, lines: list[dict]) -> Return:
    order = find_order(db, order_id, lock=True)
    if order is None:
        raise not_found("Order")
    if user.role == Role.CUSTOMER.value and order.customer_id != user.id:
        raise not_found("Order")
    if user.role in (Role.WAREHOUSE_MANAGER.value, Role.FULFILLMENT_AGENT.value) and not can_access_warehouse(
        db, user, order.warehouse_id
    ):
        raise not_found("Order")
    if order.customer_id != user.id or order.status not in (OrderStatus.DELIVERED.value, OrderStatus.SHIPPED.value):
        raise DomainError("RETURN_NOT_ELIGIBLE", "Only your shipped or delivered orders can be returned", 409)
    returned = Return(order_id=order.id, customer_id=user.id, reason=reason)
    db.add(returned)
    db.flush()
    order_items = {item.id: item for item in order.items}
    for line in lines:
        item = order_items.get(line["order_item_id"])
        if item is None or line["quantity"] > item.quantity:
            raise DomainError("INVALID_RETURN_ITEMS", "Return items must belong to the order and not exceed purchased quantities", 422)
        previously_returned = db.scalar(
            select(func.coalesce(func.sum(ReturnItem.quantity), 0))
            .join(Return, Return.id == ReturnItem.return_id)
            .where(
                Return.order_id == order.id,
                ReturnItem.order_item_id == item.id,
                Return.status != ReturnStatus.REJECTED.value,
            )
        )
        if previously_returned + line["quantity"] > item.quantity:
            raise DomainError("INVALID_RETURN_ITEMS", "Total return quantity cannot exceed purchased quantity", 422)
        db.add(ReturnItem(return_id=returned.id, order_item_id=item.id, quantity=line["quantity"]))
    notify(db, user.id, "return_requested", f"Return requested for order {order.order_number}", "return", returned.id)
    audit(db, user.id, "return_created", "return", returned.id, {"order_id": order.id})
    return returned


def change_return_status(db: Session, user: User, returned: Return, next_status: str) -> Return:
    returned = db.scalar(
        select(Return).options(selectinload(Return.items)).where(Return.id == returned.id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if returned is None:
        raise not_found("Return")
    if next_status not in RETURN_TRANSITIONS.get(returned.status, set()):
        raise DomainError("INVALID_RETURN_STATUS", f"Cannot change return from {returned.status} to {next_status}", 409)
    if next_status == ReturnStatus.RECEIVED.value:
        order = db.get(Order, returned.order_id)
        for return_item in returned.items:
            item = db.get(OrderItem, return_item.order_item_id)
            inventory = db.scalar(
                select(Inventory)
                .where(Inventory.product_id == item.product_id, Inventory.warehouse_id == order.warehouse_id)
                .with_for_update()
            )
            inventory.available_quantity += return_item.quantity
            record_transaction(
                db, item.product_id, order.warehouse_id, InventoryTransactionType.RETURN,
                return_item.quantity, user.id, "return", returned.id,
            )
    returned.status = next_status
    audit(db, user.id, f"return_{next_status}", "return", returned.id)
    notify(db, returned.customer_id, f"return_{next_status}", f"Your return request is {next_status}", "return", returned.id)
    return returned
