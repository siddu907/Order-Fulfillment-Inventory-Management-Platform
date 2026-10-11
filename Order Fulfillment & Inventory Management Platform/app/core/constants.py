from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    WAREHOUSE_MANAGER = "warehouse_manager"
    FULFILLMENT_AGENT = "fulfillment_agent"
    CUSTOMER = "customer"


class OrderStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    PROCESSING = "processing"
    PACKED = "packed"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    FAILED = "failed"
    RETURNED = "returned"


class InventoryTransactionType(StrEnum):
    PURCHASE = "purchase"
    SALE = "sale"
    RESERVATION = "reservation"
    RELEASE = "release"
    ADJUSTMENT = "adjustment"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    RETURN = "return"
    DAMAGE = "damage"


class ReturnStatus(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    RECEIVED = "received"
    REFUNDED = "refunded"
