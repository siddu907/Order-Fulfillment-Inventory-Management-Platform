from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.constants import Role
from app.core.errors import DomainError, not_found
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.permissions import require_warehouse_access
from app.models import Inventory, InventoryTransaction, User
from app.schemas import InventoryAdjustmentInput, InventoryTransferInput
from app.services.idempotency import find_replay, save_key
from app.services.inventory import adjust_inventory, transfer_inventory

router = APIRouter(prefix="/inventory")


def require_manager_or_admin(user: User) -> None:
    if user.role not in (Role.ADMIN.value, Role.WAREHOUSE_MANAGER.value):
        raise DomainError("FORBIDDEN", "Only administrators and warehouse managers can change inventory", 403)


def require_inventory_reader(user: User) -> None:
    if user.role not in (Role.ADMIN.value, Role.WAREHOUSE_MANAGER.value, Role.FULFILLMENT_AGENT.value):
        raise DomainError("FORBIDDEN", "Warehouse inventory access is restricted to warehouse roles", 403)


@router.get("")
def list_inventory(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    warehouse_id: int | None = None, product_id: int | None = None,
    low_stock: bool = False, out_of_stock: bool = False,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    require_inventory_reader(user)
    query = select(Inventory).options(selectinload(Inventory.product), selectinload(Inventory.warehouse))
    if warehouse_id is not None:
        if user.role != Role.ADMIN.value:
            require_warehouse_access(db, user, warehouse_id)
        query = query.where(Inventory.warehouse_id == warehouse_id)
    elif user.role in (Role.WAREHOUSE_MANAGER.value, Role.FULFILLMENT_AGENT.value):
        warehouse_ids = [w.id for w in user.warehouses]
        query = query.where(Inventory.warehouse_id.in_(warehouse_ids))
    if product_id is not None:
        query = query.where(Inventory.product_id == product_id)
    if low_stock:
        query = query.where(Inventory.available_quantity <= Inventory.reorder_level)
    if out_of_stock:
        query = query.where(Inventory.available_quantity == 0)
    return db.scalars(query.order_by(Inventory.id).offset((page - 1) * page_size).limit(page_size)).all()


@router.get("/low-stock")
def low_stock(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    require_inventory_reader(user)
    query = select(Inventory).options(selectinload(Inventory.product), selectinload(Inventory.warehouse)).where(
        Inventory.available_quantity <= Inventory.reorder_level
    )
    if user.role != Role.ADMIN.value:
        ids = [w.id for w in user.warehouses]
        query = query.where(Inventory.warehouse_id.in_(ids))
    return db.scalars(query.order_by(Inventory.available_quantity)).all()


@router.get("/out-of-stock")
def out_of_stock(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    require_inventory_reader(user)
    query = select(Inventory).options(selectinload(Inventory.product), selectinload(Inventory.warehouse)).where(
        Inventory.available_quantity == 0
    )
    if user.role != Role.ADMIN.value:
        query = query.where(Inventory.warehouse_id.in_([w.id for w in user.warehouses]))
    return db.scalars(query.order_by(Inventory.id)).all()


@router.get("/transactions")
def list_transactions(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    warehouse_id: int | None = None, product_id: int | None = None,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    require_inventory_reader(user)
    query = select(InventoryTransaction)
    if warehouse_id is not None:
        if user.role != Role.ADMIN.value:
            require_warehouse_access(db, user, warehouse_id)
        query = query.where(InventoryTransaction.warehouse_id == warehouse_id)
    elif user.role in (Role.WAREHOUSE_MANAGER.value, Role.FULFILLMENT_AGENT.value):
        query = query.where(InventoryTransaction.warehouse_id.in_([w.id for w in user.warehouses]))
    if product_id is not None:
        query = query.where(InventoryTransaction.product_id == product_id)
    return db.scalars(query.order_by(InventoryTransaction.created_at.desc()).offset((page - 1) * page_size).limit(page_size)).all()


@router.get("/transactions/{transaction_id}")
def get_transaction(transaction_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    require_inventory_reader(user)
    transaction = db.get(InventoryTransaction, transaction_id)
    if transaction is None:
        raise not_found("Inventory transaction")
    if user.role != Role.ADMIN.value:
        require_warehouse_access(db, user, transaction.warehouse_id)
    return transaction


@router.get("/{product_id}")
def get_product_inventory(
    product_id: int, warehouse_id: int | None = None,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    require_inventory_reader(user)
    query = select(Inventory).options(selectinload(Inventory.product), selectinload(Inventory.warehouse)).where(
        Inventory.product_id == product_id
    )
    if warehouse_id is not None:
        if user.role != Role.ADMIN.value:
            require_warehouse_access(db, user, warehouse_id)
        query = query.where(Inventory.warehouse_id == warehouse_id)
    elif user.role in (Role.WAREHOUSE_MANAGER.value, Role.FULFILLMENT_AGENT.value):
        query = query.where(Inventory.warehouse_id.in_([w.id for w in user.warehouses]))
    items = db.scalars(query).all()
    if not items:
        raise not_found("Inventory")
    return items[0] if warehouse_id is not None else items


@router.post("/adjust")
def adjust(
    payload: InventoryAdjustmentInput,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    require_manager_or_admin(user)
    data = payload.model_dump(mode="json")
    replay = find_replay(db, user.id, "POST:/inventory/adjust", idempotency_key, data)
    if replay is not None:
        return replay.response_json
    inventory, transaction = adjust_inventory(
        db, user, payload.product_id, payload.warehouse_id, payload.adjustment, payload.reason, payload.reorder_level
    )
    db.flush()
    result = {
        "inventory_id": inventory.id, "product_id": inventory.product_id, "warehouse_id": inventory.warehouse_id,
        "available_quantity": inventory.available_quantity, "reserved_quantity": inventory.reserved_quantity,
        "transaction_id": transaction.id,
    }
    save_key(db, user.id, "POST:/inventory/adjust", idempotency_key, data, transaction.id, result)
    db.commit()
    return result


@router.post("/transfer")
def transfer(
    payload: InventoryTransferInput,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    require_manager_or_admin(user)
    data = payload.model_dump(mode="json")
    replay = find_replay(db, user.id, "POST:/inventory/transfer", idempotency_key, data)
    if replay is not None:
        return replay.response_json
    source, destination, transfer_out, transfer_in = transfer_inventory(
        db, user, payload.product_id, payload.source_warehouse_id, payload.destination_warehouse_id,
        payload.quantity, payload.reason,
    )
    db.flush()
    result = {
        "source_available_quantity": source.available_quantity,
        "destination_available_quantity": destination.available_quantity,
        "transaction_ids": [transfer_out.id, transfer_in.id],
    }
    save_key(db, user.id, "POST:/inventory/transfer", idempotency_key, data, transfer_out.id, result)
    db.commit()
    return result
