from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.models import Order, User
from app.services.common import audit, notify


def confirm_demo_payment(db: Session, order: Order, user: User) -> None:
    if order.payment_status == "paid":
        return
    if order.payment_status != "pending":
        raise DomainError("INVALID_PAYMENT_STATUS", "Payment cannot be confirmed in the current state", 409)
    order.payment_status = "paid"
    audit(db, user.id, "payment_confirmed", "order", order.id, {"provider": "local_demo"})
    notify(
        db,
        order.customer_id,
        "payment_confirmed",
        f"Payment for order {order.order_number} was confirmed",
        "order",
        order.id,
    )
