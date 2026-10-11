from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Inventory


def find_inventory(db: Session, product_id: int, warehouse_id: int, *, lock: bool = False) -> Inventory | None:
    query = select(Inventory).where(
        Inventory.product_id == product_id,
        Inventory.warehouse_id == warehouse_id,
    )
    if lock:
        query = query.with_for_update()
    return db.scalar(query)
