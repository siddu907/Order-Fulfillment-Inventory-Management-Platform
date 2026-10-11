from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constants import Role
from app.core.errors import DomainError, not_found
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.permissions import require_roles
from app.models import Category, Product, User, Warehouse
from app.schemas import (
    CategoryInput,
    CategoryUpdateInput,
    ProductInput,
    ProductUpdateInput,
    UserCreateInput,
    UserOutput,
    UserUpdateInput,
    WarehouseInput,
    WarehouseOutput,
    WarehouseUpdateInput,
    WarehouseUserInput,
)
from app.core.security import hash_password
from app.services.common import audit

users_router = APIRouter()
products_router = APIRouter()
categories_router = APIRouter()
warehouses_router = APIRouter()
admin_required = require_roles(Role.ADMIN)


@products_router.post("/products", status_code=201)
def create_product(payload: ProductInput, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    admin_required(user)
    product = Product(**payload.model_dump())
    db.add(product)
    db.flush()
    audit(db, user.id, "product_created", "product", product.id)
    db.commit()
    db.refresh(product)
    return product


@products_router.get("/products")
def list_products(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    category_id: int | None = None, active: bool | None = None,
    db: Session = Depends(get_db), _user: User = Depends(get_current_user),
):
    query = select(Product)
    if category_id is not None:
        query = query.where(Product.category_id == category_id)
    if active is not None:
        query = query.where(Product.is_active == active)
    return db.scalars(query.order_by(Product.id).offset((page - 1) * page_size).limit(page_size)).all()


@products_router.get("/products/{product_id}")
def get_product(product_id: int, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    product = db.get(Product, product_id)
    if product is None:
        raise not_found("Product")
    return product


@products_router.put("/products/{product_id}")
def update_product(
    product_id: int,
    payload: ProductUpdateInput,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    admin_required(user)
    product = db.get(Product, product_id)
    if product is None:
        raise not_found("Product")
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise DomainError("EMPTY_UPDATE", "Provide at least one product field to update", 422)
    for key, value in fields.items():
        setattr(product, key, value)
    audit(db, user.id, "product_updated", "product", product.id, {"fields": sorted(fields)})
    db.commit()
    db.refresh(product)
    return product


@categories_router.post("/categories", status_code=201)
def create_category(payload: CategoryInput, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    admin_required(user)
    category = Category(**payload.model_dump())
    db.add(category)
    db.commit()
    db.refresh(category)
    return category


@categories_router.get("/categories")
def list_categories(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    return db.scalars(select(Category).order_by(Category.name)).all()


@categories_router.get("/categories/{category_id}")
def get_category(category_id: int, db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    category = db.get(Category, category_id)
    if category is None:
        raise not_found("Category")
    return category


@categories_router.put("/categories/{category_id}")
def update_category(
    category_id: int,
    payload: CategoryUpdateInput,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    admin_required(user)
    category = db.get(Category, category_id)
    if category is None:
        raise not_found("Category")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(category, field, value)
    db.commit()
    return category


@warehouses_router.post("/warehouses", status_code=201, response_model=WarehouseOutput)
def create_warehouse(payload: WarehouseInput, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    admin_required(user)
    warehouse = Warehouse(**payload.model_dump())
    db.add(warehouse)
    db.flush()
    audit(db, user.id, "warehouse_created", "warehouse", warehouse.id)
    db.commit()
    db.refresh(warehouse)
    return warehouse


@warehouses_router.get("/warehouses", response_model=list[WarehouseOutput])
def list_warehouses(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    query = select(Warehouse)
    if user.role != Role.ADMIN.value:
        query = query.where(Warehouse.users.any(User.id == user.id))
    return db.scalars(query.order_by(Warehouse.id)).all()


@warehouses_router.get("/warehouses/{warehouse_id}", response_model=WarehouseOutput)
def get_warehouse(warehouse_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    warehouse = db.get(Warehouse, warehouse_id)
    if warehouse is None:
        raise not_found("Warehouse")
    if user.role != Role.ADMIN.value and not any(w.id == warehouse_id for w in user.warehouses):
        raise not_found("Warehouse")
    return warehouse


@warehouses_router.get("/warehouses/{warehouse_id}/users", response_model=list[UserOutput])
def list_warehouse_users(
    warehouse_id: int,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    admin_required(actor)
    warehouse = db.get(Warehouse, warehouse_id)
    if warehouse is None:
        raise not_found("Warehouse")
    return sorted(warehouse.users, key=lambda user: user.id)


@warehouses_router.post(
    "/warehouses/{warehouse_id}/users",
    status_code=201,
    response_model=UserOutput,
)
def add_warehouse_user(
    warehouse_id: int,
    payload: WarehouseUserInput,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    admin_required(actor)
    warehouse = db.get(Warehouse, warehouse_id)
    if warehouse is None:
        raise not_found("Warehouse")
    member = db.get(User, payload.user_id)
    if member is None:
        raise not_found("User")
    if member.role not in (Role.WAREHOUSE_MANAGER.value, Role.FULFILLMENT_AGENT.value):
        raise DomainError(
            "INVALID_WAREHOUSE_USER",
            "Only warehouse managers and fulfillment agents can be assigned to a warehouse",
            422,
        )
    if not member.is_active:
        raise DomainError("INACTIVE_USER", "An inactive user cannot be assigned to a warehouse", 409)
    if any(existing.id == member.id for existing in warehouse.users):
        raise DomainError("WAREHOUSE_USER_EXISTS", "User is already assigned to this warehouse", 409)
    warehouse.users.append(member)
    audit(
        db,
        actor.id,
        "warehouse_user_added",
        "warehouse",
        warehouse.id,
        {"user_id": member.id},
    )
    db.commit()
    db.refresh(member)
    return member


@warehouses_router.delete(
    "/warehouses/{warehouse_id}/users/{user_id}",
    response_model=UserOutput,
)
def remove_warehouse_user(
    warehouse_id: int,
    user_id: int,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    admin_required(actor)
    warehouse = db.get(Warehouse, warehouse_id)
    if warehouse is None:
        raise not_found("Warehouse")
    member = next((user for user in warehouse.users if user.id == user_id), None)
    if member is None:
        raise not_found("Warehouse user")
    warehouse.users.remove(member)
    audit(
        db,
        actor.id,
        "warehouse_user_removed",
        "warehouse",
        warehouse.id,
        {"user_id": member.id},
    )
    db.commit()
    return member


@warehouses_router.put("/warehouses/{warehouse_id}", response_model=WarehouseOutput)
def update_warehouse(
    warehouse_id: int,
    payload: WarehouseUpdateInput,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role not in (Role.ADMIN.value, Role.WAREHOUSE_MANAGER.value):
        raise DomainError("FORBIDDEN", "Only administrators and warehouse managers can update warehouses", 403)
    warehouse = db.get(Warehouse, warehouse_id)
    if warehouse is None:
        raise not_found("Warehouse")
    if user.role != Role.ADMIN.value and not any(w.id == warehouse_id for w in user.warehouses):
        raise DomainError("WAREHOUSE_ACCESS_DENIED", "You are not authorized for this warehouse", 403)
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise DomainError("EMPTY_UPDATE", "Provide at least one warehouse field to update", 422)
    for key, value in fields.items():
        setattr(warehouse, key, value)
    audit(db, user.id, "warehouse_updated", "warehouse", warehouse.id, {"fields": sorted(fields)})
    db.commit()
    return warehouse


@users_router.post("/users", status_code=201, response_model=UserOutput)
def create_user(payload: UserCreateInput, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    admin_required(user)
    created = User(
        email=payload.email.lower(),
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        role=payload.role.value,
    )
    db.add(created)
    db.commit()
    db.refresh(created)
    return created


@users_router.get("/users", response_model=list[UserOutput])
def list_users(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
               user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    admin_required(user)
    return db.scalars(select(User).order_by(User.id).offset((page - 1) * page_size).limit(page_size)).all()


@users_router.get("/users/{user_id}", response_model=UserOutput)
def get_user(user_id: int, actor: User = Depends(get_current_user), db: Session = Depends(get_db)):
    admin_required(actor)
    target = db.get(User, user_id)
    if target is None:
        raise not_found("User")
    return target


@users_router.put("/users/{user_id}", response_model=UserOutput)
def update_user(
    user_id: int, payload: UserUpdateInput,
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    admin_required(actor)
    target = db.get(User, user_id)
    if target is None:
        raise not_found("User")
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise DomainError("EMPTY_UPDATE", "Provide at least one user field to update", 422)
    if "email" in fields and fields["email"] is not None:
        fields["email"] = fields["email"].lower()
    for key, value in fields.items():
        setattr(target, key, value)
    audit(db, actor.id, "user_updated", "user", target.id, {"fields": sorted(fields)})
    db.commit()
    return target
