from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.constants import Role


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RegisterInput(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=10, max_length=128)


class UserCreateInput(RegisterInput):
    role: Role = Role.CUSTOMER


class LoginInput(BaseModel):
    email: EmailStr="admin@gmail.com"
    password: str="Newpass@123"


class RefreshInput(BaseModel):
    refresh_token: str


class ChangePasswordInput(BaseModel):
    new_password: str = Field(min_length=10, max_length=128)


class UserOutput(ORMModel):
    id: int
    email: EmailStr
    full_name: str
    role: str
    is_active: bool


class TokenOutput(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserOutput


class UserUpdateInput(BaseModel):
    email: EmailStr | None = None
    full_name: str | None = Field(default=None, min_length=1, max_length=160)
    is_active: bool | None = None


class ProductInput(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    category_id: int | None = None
    price: Decimal = Field(ge=0)
    cost_price: Decimal = Field(default=Decimal("0"), ge=0)
    is_active: bool = True


class ProductUpdateInput(BaseModel):
    sku: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    category_id: int | None = None
    price: Decimal | None = Field(default=None, ge=0)
    cost_price: Decimal | None = Field(default=None, ge=0)
    is_active: bool | None = None

    @field_validator("sku", "name", "price", "cost_price", "is_active", mode="before")
    @classmethod
    def reject_null_for_non_nullable_fields(cls, value: object) -> object:
        if value is None:
            raise ValueError("This field cannot be null")
        return value


class CategoryInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)


class CategoryUpdateInput(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None


class WarehouseInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    location: str = Field(min_length=1, max_length=300)
    capacity: int = Field(default=0, ge=0)


class WarehouseOutput(ORMModel):
    id: int
    name: str
    location: str
    capacity: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class WarehouseUserInput(BaseModel):
    user_id: int = Field(gt=0)


class WarehouseUpdateInput(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    location: str | None = Field(default=None, min_length=1, max_length=300)
    capacity: int | None = Field(default=None, ge=0)
    is_active: bool | None = None

    @field_validator("name", "location", "capacity", "is_active", mode="before")
    @classmethod
    def reject_null_for_non_nullable_fields(cls, value: object) -> object:
        if value is None:
            raise ValueError("This field cannot be null")
        return value


class InventoryAdjustmentInput(BaseModel):
    product_id: int
    warehouse_id: int
    adjustment: int
    reason: str = Field(min_length=1, max_length=500)
    reorder_level: int | None = Field(default=None, ge=0)


class InventoryTransferInput(BaseModel):
    product_id: int
    source_warehouse_id: int
    destination_warehouse_id: int
    quantity: int = Field(gt=0)
    reason: str | None = Field(default=None, max_length=500)


class OrderLineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int
    quantity: int = Field(gt=0)
    discount: Decimal = Field(default=Decimal("0"), ge=0)


class OrderInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "items": [{"product_id": 1, "quantity": 2}],
            }
        }
    )

    items: list[OrderLineInput] = Field(min_length=1)


class OrderStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str | None = None
    expected_delivery_at: datetime | None = None

    @field_validator("status", mode="before")
    @classmethod
    def normalize_order_status(cls, value: object) -> object:
        if value is None:
            raise ValueError("This field cannot be null")
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("expected_delivery_at", mode="before")
    @classmethod
    def reject_null_delivery_date(cls, value: object) -> object:
        if value is None:
            raise ValueError("This field cannot be null")
        return value


class AssignOrderInput(BaseModel):
    agent_id: int


class TaxRateInput(BaseModel):
    rate_percent: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)


class ReturnLineInput(BaseModel):
    order_item_id: int
    quantity: int = Field(gt=0)


class ReturnInput(BaseModel):
    order_id: int
    reason: str = Field(min_length=1, max_length=1000)
    items: list[ReturnLineInput] = Field(min_length=1)
