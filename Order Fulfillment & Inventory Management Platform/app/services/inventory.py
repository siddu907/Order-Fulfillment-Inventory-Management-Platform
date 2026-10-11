from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.constants import InventoryTransactionType
from app.core.errors import DomainError, not_found
from app.dependencies.permissions import require_warehouse_access
from app.models import Inventory, InventoryTransaction, Product, User, Warehouse
from app.repositories.inventory import find_inventory
from app.services.common import audit, notify


def get_inventory(db: Session, product_id: int, warehouse_id: int, lock: bool = False) -> Inventory:
    item = find_inventory(db, product_id, warehouse_id, lock=lock)
    if item is None:
        raise not_found("Inventory")
    return item


def record_transaction(
    db: Session,
    product_id: int,
    warehouse_id: int,
    transaction_type: InventoryTransactionType,
    quantity: int,
    user_id: int | None,
    reference_entity: str | None = None,
    reference_id: int | str | None = None,
    metadata: dict | None = None,
) -> InventoryTransaction:
    transaction = InventoryTransaction(
        product_id=product_id,
        warehouse_id=warehouse_id,
        transaction_type=transaction_type.value,
        quantity=quantity,
        user_id=user_id,
        reference_entity=reference_entity,
        reference_id=str(reference_id) if reference_id is not None else None,
        metadata_json=metadata or {},
    )
    db.add(transaction)
    return transaction


def notify_stock_thresholds(
    db: Session,
    product_id: int,
    warehouse_id: int,
    previous_available: int,
    new_available: int,
    reorder_level: int,
    inventory_id: int | None,
) -> None:
    crossed_low_stock = previous_available > reorder_level >= new_available
    became_out_of_stock = previous_available > 0 and new_available == 0
    if not crossed_low_stock and not became_out_of_stock:
        return
    warehouse = db.scalar(select(Warehouse).where(Warehouse.id == warehouse_id))
    if warehouse is None:
        return
    product = db.get(Product, product_id)
    sku = product.sku if product is not None else str(product_id)
    for manager in warehouse.users:
        if manager.role != "warehouse_manager":
            continue
        if crossed_low_stock:
            notify(
                db,
                manager.id,
                "low_stock",
                f"{sku} has reached its reorder level",
                "inventory",
                inventory_id,
            )
        if became_out_of_stock:
            notify(
                db,
                manager.id,
                "out_of_stock",
                f"{sku} is out of stock at warehouse {warehouse.name}",
                "inventory",
                inventory_id,
            )


def reserve_stock(db: Session, product_id: int, warehouse_id: int, quantity: int, user_id: int, order_id: int) -> None:
    before = db.scalar(
        select(Inventory).where(
            Inventory.product_id == product_id, Inventory.warehouse_id == warehouse_id
        ).with_for_update()
    )
    previous_available = before.available_quantity if before is not None else None
    reorder_level = before.reorder_level if before is not None else None
    result = db.execute(
        update(Inventory)
        .where(
            Inventory.product_id == product_id,
            Inventory.warehouse_id == warehouse_id,
            Inventory.available_quantity >= quantity,
        )
        .values(
            available_quantity=Inventory.available_quantity - quantity,
            reserved_quantity=Inventory.reserved_quantity + quantity,
        )
    )
    if result.rowcount != 1:
        inventory = db.scalar(
            select(Inventory).where(
                Inventory.product_id == product_id, Inventory.warehouse_id == warehouse_id
            )
        )
        if inventory is None:
            raise DomainError("INSUFFICIENT_INVENTORY", "No inventory is available for this product", 409)
        product = db.get(Product, product_id)
        raise DomainError(
            "INSUFFICIENT_INVENTORY",
            f"Insufficient inventory for product {product.sku if product else product_id}",
            409,
        )
    record_transaction(
        db, product_id, warehouse_id, InventoryTransactionType.RESERVATION, quantity, user_id, "order", order_id
    )
    audit(db, user_id, "inventory_reserved", "inventory", before.id if before else None,
          {"product_id": product_id, "warehouse_id": warehouse_id, "quantity": quantity, "order_id": order_id})
    if previous_available is not None and reorder_level is not None:
        notify_stock_thresholds(
            db,
            product_id,
            warehouse_id,
            previous_available,
            previous_available - quantity,
            reorder_level,
            before.id if before else None,
        )


def release_stock(db: Session, product_id: int, warehouse_id: int, quantity: int, user_id: int, order_id: int) -> None:
    inventory = find_inventory(db, product_id, warehouse_id, lock=True)
    if inventory is None:
        raise DomainError("INVENTORY_INVARIANT_VIOLATION", "Reserved stock could not be released", 409)
    previous_available = inventory.available_quantity
    result = db.execute(
        update(Inventory)
        .where(
            Inventory.product_id == product_id,
            Inventory.warehouse_id == warehouse_id,
            Inventory.reserved_quantity >= quantity,
        )
        .values(
            available_quantity=Inventory.available_quantity + quantity,
            reserved_quantity=Inventory.reserved_quantity - quantity,
        )
    )
    if result.rowcount != 1:
        raise DomainError("INVENTORY_INVARIANT_VIOLATION", "Reserved stock could not be released", 409)
    record_transaction(db, product_id, warehouse_id, InventoryTransactionType.RELEASE, quantity, user_id, "order", order_id)
    audit(db, user_id, "inventory_released", "inventory", None,
          {"product_id": product_id, "warehouse_id": warehouse_id, "quantity": quantity, "order_id": order_id})
    warehouse = db.scalar(select(Warehouse).where(Warehouse.id == warehouse_id))
    product = db.get(Product, product_id)
    if warehouse is not None:
        for manager in warehouse.users:
            if manager.role == "warehouse_manager":
                notify(
                    db,
                    manager.id,
                    "stock_restored",
                    f"{quantity} units of {product.sku if product else product_id} were restored; "
                    f"{previous_available + quantity} are now available at {warehouse.name}",
                    "inventory",
                    inventory.id,
                )


def consume_reserved_stock(
    db: Session, product_id: int, warehouse_id: int, quantity: int, user_id: int, order_id: int
) -> None:
    result = db.execute(
        update(Inventory)
        .where(
            Inventory.product_id == product_id,
            Inventory.warehouse_id == warehouse_id,
            Inventory.reserved_quantity >= quantity,
        )
        .values(reserved_quantity=Inventory.reserved_quantity - quantity)
    )
    if result.rowcount != 1:
        raise DomainError("INVENTORY_INVARIANT_VIOLATION", "Reserved stock could not be consumed", 409)
    record_transaction(db, product_id, warehouse_id, InventoryTransactionType.SALE, quantity, user_id, "order", order_id)


def adjust_inventory(
    db: Session, user: User, product_id: int, warehouse_id: int, adjustment: int, reason: str,
    reorder_level: int | None = None,
) -> tuple[Inventory, InventoryTransaction]:
    require_warehouse_access(db, user, warehouse_id)
    inventory = find_inventory(db, product_id, warehouse_id, lock=True)
    if inventory is None:
        if adjustment <= 0:
            raise not_found("Inventory")
        if db.get(Product, product_id) is None:
            raise not_found("Product")
        if db.get(Warehouse, warehouse_id) is None:
            raise not_found("Warehouse")
        inventory = Inventory(
            product_id=product_id,
            warehouse_id=warehouse_id,
            available_quantity=0,
            reorder_level=reorder_level or 0,
        )
        try:
            with db.begin_nested():
                db.add(inventory)
                db.flush()
        except IntegrityError:
            inventory = find_inventory(db, product_id, warehouse_id, lock=True)
            if inventory is None:
                raise
    previous = inventory.available_quantity
    new_quantity = previous + adjustment
    if adjustment == 0 or new_quantity < 0:
        raise DomainError("INVALID_INVENTORY_ADJUSTMENT", "Adjustment must be non-zero and cannot make stock negative", 400)
    inventory.available_quantity = new_quantity
    if reorder_level is not None:
        inventory.reorder_level = reorder_level
    transaction = record_transaction(
        db, product_id, warehouse_id,
        InventoryTransactionType.ADJUSTMENT if adjustment > 0 else InventoryTransactionType.DAMAGE,
        abs(adjustment), user.id, "adjustment", None,
        {"previous_quantity": previous, "new_quantity": new_quantity, "adjustment": adjustment, "reason": reason},
    )
    audit(
        db, user.id, "inventory_adjusted", "inventory", inventory.id,
        {"previous_quantity": previous, "new_quantity": new_quantity, "adjustment": adjustment, "reason": reason},
    )
    notify_stock_thresholds(
        db,
        product_id,
        warehouse_id,
        previous,
        new_quantity,
        inventory.reorder_level,
        inventory.id,
    )
    return inventory, transaction


def transfer_inventory(db: Session, user: User, product_id: int, source_id: int, destination_id: int,
                       quantity: int, reason: str | None = None) -> tuple[Inventory, Inventory, InventoryTransaction, InventoryTransaction]:
    if source_id == destination_id:
        raise DomainError("INVALID_INVENTORY_TRANSFER", "Source and destination warehouses must differ", 400)
    require_warehouse_access(db, user, source_id)
    require_warehouse_access(db, user, destination_id)
    warehouses = db.scalars(
        select(Warehouse).where(Warehouse.id.in_([source_id, destination_id])).order_by(Warehouse.id).with_for_update()
    ).all()
    if len(warehouses) != 2 or any(not w.is_active for w in warehouses):
        raise DomainError("INVALID_INVENTORY_TRANSFER", "Both warehouses must exist and be active", 409)
    source = get_inventory(db, product_id, source_id, lock=True)
    destination = find_inventory(db, product_id, destination_id, lock=True)
    if destination is None:
        destination = Inventory(product_id=product_id, warehouse_id=destination_id)
        try:
            with db.begin_nested():
                db.add(destination)
                db.flush()
        except IntegrityError:
            destination = find_inventory(db, product_id, destination_id, lock=True)
            if destination is None:
                raise
    if source.available_quantity < quantity:
        raise DomainError("INSUFFICIENT_INVENTORY", "Insufficient available stock to transfer", 409)
    source.available_quantity -= quantity
    destination.available_quantity += quantity
    transfer_out = record_transaction(
        db, product_id, source_id, InventoryTransactionType.TRANSFER_OUT, quantity, user.id, "transfer", None,
        {"destination_warehouse_id": destination_id, "reason": reason}
    )
    transfer_in = record_transaction(
        db, product_id, destination_id, InventoryTransactionType.TRANSFER_IN, quantity, user.id, "transfer", None,
        {"source_warehouse_id": source_id, "reason": reason}
    )
    audit(db, user.id, "inventory_transferred", "inventory", product_id,
          {"source_warehouse_id": source_id, "destination_warehouse_id": destination_id, "quantity": quantity})
    for manager in {m.id: m for w in warehouses for m in w.users if m.role == "warehouse_manager"}.values():
        notify(db, manager.id, "inventory_transfer", f"Inventory transfer of {quantity} units for product {product_id}", "product", product_id)
    return source, destination, transfer_out, transfer_in
