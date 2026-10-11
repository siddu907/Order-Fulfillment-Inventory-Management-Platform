from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Order


def find_order(db: Session, order_id: int, *, lock: bool = False) -> Order | None:
    query = select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return db.scalar(query)
