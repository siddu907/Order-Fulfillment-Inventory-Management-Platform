from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from threading import Barrier

import pytest
from jose import jwt
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from sqlalchemy import create_engine

from app.config import settings
from app.core.security import ALGORITHM
from app.database import Base
from app.models import (
    Inventory,
    InventoryTransaction,
    Notification,
    AuditLog,
    OrderAssignment,
    Order,
    OrderItem,
    OrderStatusHistory,
    Product,
    RoleRecord,
    TaxConfiguration,
    User,
    Warehouse,
)
from app.services.orders import create_order as create_order_service


def create_order(client, headers, _warehouse_id, product_id, quantity=1, key="order-1", **line_fields):
    line = {"product_id": product_id, "quantity": quantity, **line_fields}
    return client.post(
        "/orders",
        headers={**headers, "Idempotency-Key": key},
        json={"items": [line]},
    )


def get_inventory(context, warehouse_id=None):
    with Session(context["engine"]) as db:
        query = select(Inventory).where(Inventory.product_id == context["product_id"])
        if warehouse_id is not None:
            query = query.where(Inventory.warehouse_id == warehouse_id)
        return db.scalars(query.order_by(Inventory.warehouse_id)).all()


def test_user_role_change_endpoint_is_removed(test_context):
        client = test_context["client"]
        assert "/users/{user_id}/role" not in client.get("/openapi.json").json()["paths"]
        response = client.put(
            "/users/1/role",
            headers=test_context["admin_headers"],
            json={"role": "admin"},
        )
        assert response.status_code == 404


def test_user_activation_endpoints_are_explicit(test_context):
        client = test_context["client"]
        paths = client.get("/openapi.json").json()["paths"]
        assert "/users/{user_id}/activate" not in paths
        assert "/users/{user_id}/deactivate" not in paths
        assert "/users/{user_id}/{action}" not in paths

        user_id = test_context["customer_id"]
        deactivated = client.put(
            f"/users/{user_id}",
            headers=test_context["admin_headers"],
            json={"is_active": False},
        )
        assert deactivated.status_code == 200
        assert deactivated.json()["is_active"] is False

        activated = client.put(
            f"/users/{user_id}",
            headers=test_context["admin_headers"],
            json={"is_active": True},
        )
        assert activated.status_code == 200
        assert activated.json()["is_active"] is True


def test_admin_can_create_user_with_documented_request_body(test_context):
        client = test_context["client"]
        response = client.post(
            "/users",
            headers=test_context["admin_headers"],
            json={
                "email": "warehouse-created@example.com",
                "full_name": "Warehouse User",
                "password": "secure-password-123",
                "role": "warehouse_manager",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["email"] == "warehouse-created@example.com"
        assert response.json()["full_name"] == "Warehouse User"
        assert response.json()["role"] == "warehouse_manager"

        openapi = client.get("/openapi.json").json()
        request_schema = openapi["paths"]["/users"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert "UserCreateInput" in request_schema["$ref"]
        user_schema = openapi["components"]["schemas"]["UserCreateInput"]
        assert {"email", "full_name", "password", "role"} <= set(user_schema["properties"])


def test_admin_can_create_category_with_documented_request_body(test_context):
    client = test_context["client"]
    response = client.post(
        "/categories",
        headers=test_context["admin_headers"],
        json={
            "name": "Home Appliances",
            "description": "Appliances for home use",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["name"] == "Home Appliances"
    assert response.json()["description"] == "Appliances for home use"

    openapi = client.get("/openapi.json").json()
    create_schema = openapi["paths"]["/categories"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert "CategoryInput" in create_schema["$ref"]
    category_schema = openapi["components"]["schemas"]["CategoryInput"]
    assert {"name", "description"} <= set(category_schema["properties"])

    update_response = client.put(
        f"/categories/{response.json()['id']}",
        headers=test_context["admin_headers"],
        json={"name": "Large Home Appliances"},
    )
    assert update_response.status_code == 200, update_response.text
    assert update_response.json()["name"] == "Large Home Appliances"

    deactivate_response = client.put(
        f"/categories/{response.json()['id']}",
        headers=test_context["admin_headers"],
        json={"is_active": False},
    )
    assert deactivate_response.status_code == 200, deactivate_response.text
    assert deactivate_response.json()["is_active"] is False

    activate_response = client.put(
        f"/categories/{response.json()['id']}",
        headers=test_context["admin_headers"],
        json={"is_active": True},
    )
    assert activate_response.status_code == 200, activate_response.text
    assert activate_response.json()["is_active"] is True

    assert "/categories/{category_id}/{action}" not in openapi["paths"]
    update_schema_ref = openapi["paths"]["/categories/{category_id}"]["put"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    assert "CategoryUpdateInput" in update_schema_ref
    update_schema = openapi["components"]["schemas"]["CategoryUpdateInput"]
    assert {"name", "description", "is_active"} <= set(update_schema["properties"])


def test_order_request_schema_has_simple_customer_example(test_context):
    openapi = test_context["client"].get("/openapi.json").json()
    order_schema = openapi["components"]["schemas"]["OrderInput"]
    assert order_schema["example"] == {
        "items": [{"product_id": 1, "quantity": 2}],
    }
    assert "warehouse_id" not in order_schema["properties"]
    assert order_schema["additionalProperties"] is False
    assert "expected_delivery_at" not in order_schema["properties"]
    assert "discount" in openapi["components"]["schemas"]["OrderLineInput"]["properties"]
    assert "tax" not in openapi["components"]["schemas"]["OrderLineInput"]["properties"]
    assert openapi["components"]["schemas"]["OrderLineInput"]["additionalProperties"] is False


def test_admin_configures_tax_and_backend_calculates_order_tax(test_context):
    c = test_context
    client = c["client"]
    default_rate = client.get("/settings/tax-rate", headers=c["customer_headers"])
    assert default_rate.status_code == 200
    assert float(default_rate.json()["rate_percent"]) == 0

    denied = client.put(
        "/settings/tax-rate", headers=c["customer_headers"], json={"rate_percent": 18}
    )
    assert denied.status_code == 403

    configured = client.put(
        "/settings/tax-rate", headers=c["admin_headers"], json={"rate_percent": "12.35"}
    )
    assert configured.status_code == 200, configured.text
    assert float(configured.json()["rate_percent"]) == 12.35

    created = client.post(
        "/orders",
        headers={**c["customer_headers"], "Idempotency-Key": "calculated-tax-order"},
        json={"items": [{"product_id": c["product_id"], "quantity": 1}]},
    )
    assert created.status_code == 201, created.text
    item = created.json()["items"][0]
    assert float(item["tax"]) == 1.24
    assert float(item["tax_rate"]) == 12.35
    assert float(item["total"]) == 11.24
    assert float(created.json()["total_amount"]) == 11.24

    customer_tax = client.post(
        "/orders",
        headers={**c["customer_headers"], "Idempotency-Key": "customer-supplied-tax"},
        json={"items": [{"product_id": c["product_id"], "quantity": 1, "tax": 0}]},
    )
    assert customer_tax.status_code == 422

    client.put("/settings/tax-rate", headers=c["admin_headers"], json={"rate_percent": 0})
    with Session(c["engine"]) as db:
        saved_item = db.scalar(select(OrderItem).where(OrderItem.order_id == created.json()["id"]))
        assert saved_item is not None
        assert float(saved_item.tax_rate) == 12.35
        assert float(saved_item.tax) == 1.24
        assert db.get(TaxConfiguration, 1).rate_percent == 0


def test_order_automatically_selects_warehouse_with_stock(test_context):
    c = test_context
    with Session(c["engine"]) as db:
        first_warehouse_inventory = db.scalar(
            select(Inventory).where(
                Inventory.product_id == c["product_id"],
                Inventory.warehouse_id == c["warehouse_a"],
            )
        )
        assert first_warehouse_inventory is not None
        first_warehouse_inventory.available_quantity = 0
        db.commit()

    request_started = datetime.now(timezone.utc)
    response = c["client"].post(
        "/orders",
        headers={**c["customer_headers"], "Idempotency-Key": "auto-warehouse-selection"},
        json={"items": [{"product_id": c["product_id"], "quantity": 2}]},
    )
    assert response.status_code == 201, response.text
    assert response.json()["warehouse_id"] == c["warehouse_b"]
    expected_delivery = datetime.fromisoformat(response.json()["expected_delivery_at"].replace("Z", "+00:00"))
    assert timedelta(days=5) <= expected_delivery - request_started <= timedelta(days=5, seconds=2)
    assert get_inventory(c, c["warehouse_b"])[0].available_quantity == 1


def test_fulfillment_roles_can_update_delivery_date_and_notify_customer(test_context):
    c = test_context
    client = c["client"]
    created = create_order(
        client,
        c["customer_headers"],
        c["warehouse_a"],
        c["product_id"],
        key="delivery-date-update",
    )
    assert created.status_code == 201, created.text
    order_id = created.json()["id"]
    expected_delivery_at = (datetime.now(timezone.utc) + timedelta(days=8)).replace(microsecond=0)

    updated = client.put(
        f"/orders/{order_id}",
        headers=c["manager_headers"],
        json={"expected_delivery_at": expected_delivery_at.isoformat()},
    )
    assert updated.status_code == 200, updated.text
    assert datetime.fromisoformat(updated.json()["expected_delivery_at"].replace("Z", "+00:00")) == expected_delivery_at

    latest_expected_delivery_at = expected_delivery_at
    for role in ("agent", "admin"):
        latest_expected_delivery_at += timedelta(days=1)
        role_update = client.put(
            f"/orders/{order_id}",
            headers=c[f"{role}_headers"],
            json={"expected_delivery_at": latest_expected_delivery_at.isoformat()},
        )
        assert role_update.status_code == 200, role_update.text
        assert datetime.fromisoformat(
            role_update.json()["expected_delivery_at"].replace("Z", "+00:00")
        ) == latest_expected_delivery_at

    customer_view = client.get(f"/orders/{order_id}", headers=c["customer_headers"])
    assert customer_view.status_code == 200
    customer_expected_delivery = datetime.fromisoformat(
        customer_view.json()["expected_delivery_at"].replace("Z", "+00:00")
    )
    assert customer_expected_delivery.replace(tzinfo=None) == latest_expected_delivery_at.replace(tzinfo=None)

    with Session(c["engine"]) as db:
        notification_count = len(db.scalars(
            select(Notification).where(
                Notification.user_id == c["customer_id"],
                Notification.event_type == "order_delivery_date_updated",
            )
        ).all())
        audit_entry = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "order_delivery_date_updated",
                AuditLog.entity_id == str(order_id),
            )
        )
        assert notification_count == 3
        assert audit_entry is not None

    forbidden_update = client.put(
        f"/orders/{order_id}",
        headers=c["customer_headers"],
        json={"expected_delivery_at": (datetime.now(timezone.utc) + timedelta(days=9)).isoformat()},
    )
    assert forbidden_update.status_code == 403

    assert client.put(
        f"/orders/{order_id}",
        headers=c["manager_headers"],
        json={"expected_delivery_at": "2020-01-01T00:00:00Z"},
    ).status_code == 422


def test_order_status_update_accepts_title_case_from_swagger(test_context):
    c = test_context
    created = create_order(
        c["client"], c["customer_headers"], c["warehouse_a"], c["product_id"],
        key="title-case-status-update",
    )
    assert created.status_code == 201, created.text

    response = c["client"].put(
        f"/orders/{created.json()['id']}",
        headers=c["admin_headers"],
        json={"status": "Confirmed"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"


def test_warehouse_update_accepts_partial_fields(test_context):
    client = test_context["client"]
    warehouse_id = test_context["warehouse_a"]
    openapi_paths = client.get("/openapi.json").json()["paths"]
    assert "get" in openapi_paths["/warehouses/{warehouse_id}/users"]
    assert "post" in openapi_paths["/warehouses/{warehouse_id}/users"]
    assert "delete" in openapi_paths["/warehouses/{warehouse_id}/users/{user_id}"]

    listed = client.get("/warehouses", headers=test_context["admin_headers"])
    assert listed.status_code == 200
    assert all("users" not in warehouse for warehouse in listed.json())
    fetched = client.get(
        f"/warehouses/{warehouse_id}", headers=test_context["admin_headers"]
    )
    assert fetched.status_code == 200
    assert "users" not in fetched.json()

    response = client.put(
        f"/warehouses/{warehouse_id}",
        headers=test_context["manager_headers"],
        json={"name": "North Distribution Center"},
    )
    assert response.status_code == 200, response.text
    assert "users" not in response.json()
    assert response.json()["name"] == "North Distribution Center"
    assert response.json()["location"] == "North"
    assert response.json()["capacity"] == 1000

    openapi = client.get("/openapi.json").json()
    assert "/warehouses/{warehouse_id}/{action}" not in openapi["paths"]
    request_schema_ref = openapi["paths"]["/warehouses/{warehouse_id}"]["put"]["requestBody"][
        "content"
    ]["application/json"]["schema"]["$ref"]
    assert "WarehouseUpdateInput" in request_schema_ref
    update_schema = openapi["components"]["schemas"]["WarehouseUpdateInput"]
    assert {"name", "location", "capacity", "is_active"} <= set(update_schema["properties"])
    assert not update_schema.get("required")

    deactivated = client.put(
        f"/warehouses/{warehouse_id}",
        headers=test_context["manager_headers"],
        json={"is_active": False},
    )
    assert deactivated.status_code == 200, deactivated.text
    assert deactivated.json()["is_active"] is False
    assert deactivated.json()["name"] == "North Distribution Center"

    activated = client.put(
        f"/warehouses/{warehouse_id}",
        headers=test_context["manager_headers"],
        json={"is_active": True},
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["is_active"] is True

    empty_update = client.put(
        f"/warehouses/{warehouse_id}",
        headers=test_context["manager_headers"],
        json={},
    )
    assert empty_update.status_code == 422
    null_update = client.put(
        f"/warehouses/{warehouse_id}",
        headers=test_context["manager_headers"],
        json={"name": None},
    )
    assert null_update.status_code == 422


def test_admin_manages_warehouse_membership_for_order_assignment(test_context):
    c = test_context
    client = c["client"]
    order = create_order(
        client, c["customer_headers"], c["warehouse_a"], c["product_id"],
        key="warehouse-membership-order",
    )
    assert order.status_code == 201, order.text
    order_id = order.json()["id"]
    agent_id = c["inactive_agent_id"]

    forbidden = client.post(
        f"/warehouses/{c['warehouse_a']}/users",
        headers=c["customer_headers"],
        json={"user_id": agent_id},
    )
    assert forbidden.status_code == 403

    activated = client.put(
        f"/users/{agent_id}", headers=c["admin_headers"], json={"is_active": True}
    )
    assert activated.status_code == 200

    unassigned = client.post(
        f"/orders/{order_id}/reassign",
        headers=c["admin_headers"],
        json={"agent_id": agent_id},
    )
    assert unassigned.status_code == 403

    added = client.post(
        f"/warehouses/{c['warehouse_a']}/users",
        headers=c["admin_headers"],
        json={"user_id": agent_id},
    )
    assert added.status_code == 201, added.text
    assert added.json()["id"] == agent_id
    assert "password_hash" not in added.json()

    members = client.get(
        f"/warehouses/{c['warehouse_a']}/users", headers=c["admin_headers"]
    )
    assert members.status_code == 200
    assert agent_id in {member["id"] for member in members.json()}

    assigned = client.post(
        f"/orders/{order_id}/reassign",
        headers=c["admin_headers"],
        json={"agent_id": agent_id},
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["agent_id"] == agent_id

    removed = client.delete(
        f"/warehouses/{c['warehouse_a']}/users/{agent_id}",
        headers=c["admin_headers"],
    )
    assert removed.status_code == 200, removed.text
    members_after_removal = client.get(
        f"/warehouses/{c['warehouse_a']}/users", headers=c["admin_headers"]
    )
    assert agent_id not in {member["id"] for member in members_after_removal.json()}


def progress_order(context, order_id, statuses=("accept", "start-processing", "pack", "ship", "deliver")):
    client = context["client"]
    assignment = client.post(
        f"/orders/{order_id}/assign",
        headers=context["manager_headers"],
        json={"agent_id": context["agent_id"]},
    )
    assert assignment.status_code == 200, assignment.text
    for action in statuses:
        response = client.post(
            f"/fulfillment/orders/{order_id}/{action}", headers=context["agent_headers"]
        )
        assert response.status_code == 200, response.text


def test_auth_registration_login_invalid_credentials_and_inactive_user(test_context):
    client = test_context["client"]
    registered = client.post(
        "/auth/register",
        json={"email": "new@example.com", "full_name": "New Customer", "password": "strong-password-123"},
    )
    assert registered.status_code == 201
    assert registered.json()["role"] == "customer"
    login = client.post("/auth/login", json={"email": "new@example.com", "password": "strong-password-123"})
    assert login.status_code == 200
    assert login.json()["access_token"] and login.json()["refresh_token"]
    assert login.json()["user"] == {
        "id": registered.json()["id"],
        "email": "new@example.com",
        "full_name": "New Customer",
        "role": "customer",
        "is_active": True,
    }
    assert login.json()["expires_at"].endswith((" AM UTC", " PM UTC"))
    assert 1 <= int(login.json()["expires_at"].split()[1].split(":")[0]) <= 12
    assert client.get(
        "/auth/me", headers={"Authorization": "Bearer " + login.json()["access_token"]}
    ).status_code == 200
    assert client.post(
        "/auth/login", json={"email": "new@example.com", "password": "incorrect-password"}
    ).status_code == 401
    assert client.post(
        "/auth/register",
        json={"email": "new@example.com", "full_name": "Duplicate", "password": "strong-password-123"},
    ).status_code == 409

    deactivated = client.put(
        f"/users/{registered.json()['id']}",
        headers=test_context["admin_headers"],
        json={"is_active": False},
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["is_active"] is False
    assert client.post(
        "/auth/login", json={"email": "new@example.com", "password": "strong-password-123"}
    ).status_code == 401


def test_expired_access_token_is_rejected(test_context):
    expired = jwt.encode(
        {
            "sub": str(test_context["customer_id"]),
            "role": "customer",
            "type": "access",
            "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
        },
        settings.secret_key,
        algorithm=ALGORITHM,
    )
    response = test_context["client"].get(
        "/auth/me", headers={"Authorization": "Bearer " + expired}
    )
    assert response.status_code == 401


def test_refresh_token_rotation_and_logout_revoke_tokens(test_context):
    client = test_context["client"]
    login = client.post(
        "/auth/login",
        json={"email": "customer@example.com", "password": "customer-password-123"},
    )
    first_refresh = login.json()["refresh_token"]
    rotated = client.post("/auth/refresh", json={"refresh_token": first_refresh})
    assert rotated.status_code == 200
    assert rotated.json()["user"]["email"] == "customer@example.com"
    assert rotated.json()["expires_at"].endswith((" AM UTC", " PM UTC"))
    assert 1 <= int(rotated.json()["expires_at"].split()[1].split(":")[0]) <= 12
    second_refresh = rotated.json()["refresh_token"]
    assert second_refresh != first_refresh
    assert client.post("/auth/refresh", json={"refresh_token": first_refresh}).status_code == 401
    logout = client.post(
        "/auth/logout",
        headers={"Authorization": "Bearer " + rotated.json()["access_token"]},
    )
    assert logout.status_code == 200
    assert logout.json() == {"message": "Logged out successfully"}
    assert client.get(
        "/auth/me",
        headers={"Authorization": "Bearer " + rotated.json()["access_token"]},
    ).status_code == 401
    assert client.get(
        "/products",
        headers={"Authorization": "Bearer " + rotated.json()["access_token"]},
    ).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": second_refresh}).status_code == 401


def test_change_password_accepts_only_new_password_and_revokes_sessions(test_context):
    client = test_context["client"]
    login = client.post(
        "/auth/login",
        json={"email": "customer@example.com", "password": "customer-password-123"},
    )
    access_token = login.json()["access_token"]
    refresh_token = login.json()["refresh_token"]
    changed = client.post(
        "/auth/change-password",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"new_password": "new-strong-password"},
    )
    assert changed.status_code == 200
    assert changed.json() == {"message": "Password changed successfully"}
    assert client.get(
        "/auth/me", headers={"Authorization": f"Bearer {access_token}"}
    ).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": refresh_token}).status_code == 401
    assert client.post(
        "/auth/login",
        json={"email": "customer@example.com", "password": "new-strong-password"},
    ).status_code == 200


def test_role_permissions_and_unauthorized_customer_resources(test_context):
    c = test_context
    client = c["client"]
    assert client.post(
        "/products",
        headers=c["customer_headers"],
        json={"sku": "FORBIDDEN", "name": "Forbidden", "price": 1, "cost_price": 0},
    ).status_code == 403
    assert client.post("/orders", headers=c["manager_headers"], json={
        "items": [{"product_id": c["product_id"], "quantity": 1}],
    }).status_code == 403
    assert client.get("/audit-logs", headers=c["customer_headers"]).status_code == 403
    order = create_order(
        client, c["customer_headers"], c["warehouse_a"], c["product_id"], key="private-order"
    ).json()
    assert client.get(
        f"/orders/{order['id']}", headers=c["other_customer_headers"]
    ).status_code == 404
    assert client.post(
        f"/orders/{order['id']}/assign",
        headers=c["customer_headers"],
        json={"agent_id": c["agent_id"]},
    ).status_code == 403


def test_warehouse_level_access_denial(test_context):
    c = test_context
    client = c["client"]
    assert client.get(
        f"/inventory/{c['product_id']}?warehouse_id={c['warehouse_unassigned']}", headers=c["manager_headers"]
    ).status_code == 403
    assert client.get(
        f"/inventory/{c['product_id']}", headers=c["customer_headers"]
    ).status_code == 403
    assert client.post(
        "/inventory/adjust",
        headers=c["manager_headers"],
        json={
            "product_id": c["product_id"], "warehouse_id": c["warehouse_unassigned"], "adjustment": 1,
            "reason": "out of scope",
        },
    ).status_code == 403


def test_product_crud_duplicate_and_inactive_order_rejection(test_context):
    c = test_context
    client = c["client"]
    created = client.post(
        "/products",
        headers=c["admin_headers"],
        json={
            "sku": "SKU-2001", "name": "Desk Lamp", "description": "Lamp",
            "price": "25.50", "cost_price": "12.00",
        },
    )
    assert created.status_code == 201, created.text
    product_id = created.json()["id"]
    listed = client.get("/products", headers=c["customer_headers"])
    assert any(product["id"] == product_id for product in listed.json())
    assert client.get(f"/products/{product_id}", headers=c["customer_headers"]).status_code == 200
    duplicate = client.post(
        "/products",
        headers=c["admin_headers"],
        json={"sku": "SKU-2001", "name": "Duplicate", "price": 1, "cost_price": 0},
    )
    assert duplicate.status_code == 409

    changed = client.put(
        f"/products/{product_id}",
        headers=c["admin_headers"],
        json={"name": "Updated Lamp"},
    )
    assert changed.status_code == 200
    assert changed.json()["name"] == "Updated Lamp"
    assert changed.json()["sku"] == "SKU-2001"
    assert changed.json()["price"] == 25.5
    product_paths = client.get("/openapi.json").json()["paths"]
    assert "/products/{product_id}/{action}" not in product_paths
    deactivated = client.put(
        f"/products/{product_id}",
        headers=c["admin_headers"],
        json={"is_active": False},
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["is_active"] is False
    product_schema_ref = product_paths["/products/{product_id}"]["put"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    assert "ProductUpdateInput" in product_schema_ref
    update_schema = client.get("/openapi.json").json()["components"]["schemas"]["ProductUpdateInput"]
    assert set(update_schema["properties"]) == {
        "sku", "name", "description", "category_id", "price", "cost_price", "is_active"
    }
    assert not update_schema.get("required")
    assert client.put(f"/products/{product_id}", headers=c["admin_headers"], json={}).status_code == 422
    assert client.put(
        f"/products/{product_id}", headers=c["admin_headers"], json={"sku": None}
    ).status_code == 422

    # Add a balance for this product so the order fails for the intended inactive-product rule.
    with Session(c["engine"]) as db:
        db.add(Inventory(
            product_id=product_id, warehouse_id=c["warehouse_a"],
            available_quantity=5, reorder_level=1,
        ))
        db.commit()
    inactive_order = create_order(client, c["customer_headers"], c["warehouse_a"], product_id, key="inactive")
    assert inactive_order.status_code == 409
    assert inactive_order.json()["error"]["code"] == "PRODUCT_INACTIVE"
    activated = client.put(
        f"/products/{product_id}",
        headers=c["admin_headers"],
        json={"is_active": True},
    )
    assert activated.status_code == 200
    assert activated.json()["is_active"] is True


def test_inventory_adjustment_low_stock_out_of_stock_and_invalid_adjustment(test_context):
    c = test_context
    client = c["client"]
    headers = {**c["manager_headers"], "Idempotency-Key": "adjust-1"}
    payload = {
        "product_id": c["product_id"], "warehouse_id": c["warehouse_a"],
        "adjustment": -8, "reason": "Cycle count", "reorder_level": 2,
    }
    adjusted = client.post("/inventory/adjust", headers=headers, json=payload)
    assert adjusted.status_code == 200, adjusted.text
    assert adjusted.json()["available_quantity"] == 2
    replay = client.post("/inventory/adjust", headers=headers, json=payload)
    assert replay.json() == adjusted.json()
    assert client.get("/inventory/low-stock", headers=c["admin_headers"]).status_code == 200
    low_items = client.get("/inventory/low-stock", headers=c["admin_headers"]).json()
    assert any(item["product_id"] == c["product_id"] and item["warehouse_id"] == c["warehouse_a"] for item in low_items)
    with Session(c["engine"]) as db:
        assert db.scalar(select(Notification).where(
            Notification.user_id == c["manager_id"], Notification.event_type == "low_stock"
        ))

    failed = client.post(
        "/inventory/adjust",
        headers={**c["manager_headers"], "Idempotency-Key": "adjust-too-much"},
        json={**payload, "adjustment": -3},
    )
    assert failed.status_code == 400
    assert get_inventory(c, c["warehouse_a"])[0].available_quantity == 2

    drained = client.post(
        "/inventory/adjust",
        headers={**c["manager_headers"], "Idempotency-Key": "adjust-drain"},
        json={**payload, "adjustment": -2},
    )
    assert drained.status_code == 200
    out_items = client.get("/inventory/out-of-stock", headers=c["admin_headers"]).json()
    assert any(item["product_id"] == c["product_id"] and item["warehouse_id"] == c["warehouse_a"] for item in out_items)
    with Session(c["engine"]) as db:
        assert db.scalar(select(Notification).where(
            Notification.user_id == c["manager_id"],
            Notification.event_type == "out_of_stock",
        ))
    transactions = client.get(
        f"/inventory/transactions?warehouse_id={c['warehouse_a']}", headers=c["manager_headers"]
    )
    assert transactions.status_code == 200
    adjustment = next(row for row in transactions.json() if row["transaction_type"] == "damage")
    assert adjustment["metadata_json"]["reason"] == "Cycle count"


def test_order_reservation_sends_out_of_stock_notification(test_context):
    c = test_context
    first_order = create_order(
        c["client"],
        c["customer_headers"],
        c["warehouse_a"],
        c["product_id"],
        quantity=10,
        key="reserve-all-stock-first-cycle",
    )
    assert first_order.status_code == 201, first_order.text

    first_out_of_stock = c["client"].get(
        "/notifications?unread_only=false", headers=c["manager_headers"]
    )
    assert first_out_of_stock.status_code == 200
    assert "out_of_stock" in {item["event_type"] for item in first_out_of_stock.json()}

    cancelled = c["client"].post(
        f"/orders/{first_order.json()['id']}/cancel",
        headers=c["customer_headers"],
    )
    assert cancelled.status_code == 200, cancelled.text
    with Session(c["engine"]) as db:
        restored_notification = db.scalar(select(Notification).where(
            Notification.user_id == c["manager_id"],
            Notification.event_type == "stock_restored",
        ))
        assert restored_notification is not None
        assert "10 units" in restored_notification.message

    second_order = create_order(
        c["client"],
        c["other_customer_headers"],
        c["warehouse_a"],
        c["product_id"],
        quantity=10,
        key="reserve-all-stock-second-cycle",
    )
    assert second_order.status_code == 201, second_order.text
    second_notifications = c["client"].get(
        "/notifications?unread_only=false", headers=c["manager_headers"]
    )
    assert second_notifications.status_code == 200
    event_types = [item["event_type"] for item in second_notifications.json()]
    assert event_types.count("low_stock") == 2
    assert event_types.count("out_of_stock") == 2
    assert event_types.count("stock_restored") == 1


def test_inventory_adjustment_creates_missing_inventory_record(test_context):
    c = test_context
    client = c["client"]
    warehouse_id = c["warehouse_unassigned"]
    headers = {**c["admin_headers"], "Idempotency-Key": "initial-stock-1"}
    payload = {
        "product_id": c["product_id"],
        "warehouse_id": warehouse_id,
        "adjustment": 200,
        "reason": "Initial stock",
        "reorder_level": 20,
    }

    response = client.post("/inventory/adjust", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["available_quantity"] == 200

    with Session(c["engine"]) as db:
        inventory = db.scalar(
            select(Inventory).where(
                Inventory.product_id == c["product_id"],
                Inventory.warehouse_id == warehouse_id,
            )
        )
        assert inventory is not None
        assert inventory.available_quantity == 200
        assert inventory.reorder_level == 20
        transaction = db.get(InventoryTransaction, response.json()["transaction_id"])
        assert transaction is not None
        assert transaction.transaction_type == "adjustment"
        assert transaction.metadata_json["previous_quantity"] == 0
        assert transaction.metadata_json["new_quantity"] == 200

    replay = client.post("/inventory/adjust", headers=headers, json=payload)
    assert replay.status_code == 200
    assert replay.json() == response.json()
    assert get_inventory(c, warehouse_id)[0].available_quantity == 200


def test_inventory_transfer_success_failure_and_idempotency(test_context):
    c = test_context
    client = c["client"]
    payload = {
        "product_id": c["product_id"], "source_warehouse_id": c["warehouse_a"],
        "destination_warehouse_id": c["warehouse_b"], "quantity": 4,
    }
    headers = {**c["admin_headers"], "Idempotency-Key": "transfer-1"}
    response = client.post("/inventory/transfer", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    replay = client.post("/inventory/transfer", headers=headers, json=payload)
    assert replay.json() == response.json()
    rows = get_inventory(c)
    assert [row.available_quantity for row in rows] == [6, 7]
    with Session(c["engine"]) as db:
        transaction_types = set(db.scalars(select(InventoryTransaction.transaction_type)))
        assert {"transfer_out", "transfer_in"} <= transaction_types

    failed = client.post(
        "/inventory/transfer",
        headers={**c["admin_headers"], "Idempotency-Key": "transfer-insufficient"},
        json={**payload, "quantity": 100},
    )
    assert failed.status_code == 409
    assert [row.available_quantity for row in get_inventory(c)] == [6, 7]
    invalid = client.post(
        "/inventory/transfer",
        headers={**c["admin_headers"], "Idempotency-Key": "transfer-same"},
        json={**payload, "destination_warehouse_id": c["warehouse_a"]},
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "INVALID_INVENTORY_TRANSFER"


def test_inventory_transfer_creates_missing_destination_inventory(test_context):
    c = test_context
    client = c["client"]
    destination_id = c["warehouse_unassigned"]
    headers = {**c["admin_headers"], "Idempotency-Key": "transfer-create-destination"}
    response = client.post(
        "/inventory/transfer",
        headers=headers,
        json={
            "product_id": c["product_id"],
            "source_warehouse_id": c["warehouse_a"],
            "destination_warehouse_id": destination_id,
            "quantity": 5,
            "reason": "Rebalance stock",
        },
    )
    assert response.status_code == 200, response.text

    with Session(c["engine"]) as db:
        source = db.scalar(select(Inventory).where(
            Inventory.product_id == c["product_id"],
            Inventory.warehouse_id == c["warehouse_a"],
        ))
        destination = db.scalar(select(Inventory).where(
            Inventory.product_id == c["product_id"],
            Inventory.warehouse_id == destination_id,
        ))
        assert source is not None and source.available_quantity == 5
        assert destination is not None and destination.available_quantity == 5
        transfer_in = db.get(InventoryTransaction, response.json()["transaction_ids"][1])
        assert transfer_in is not None
        assert transfer_in.transaction_type == "transfer_in"

    replay = client.post(
        "/inventory/transfer",
        headers=headers,
        json={
            "product_id": c["product_id"],
            "source_warehouse_id": c["warehouse_a"],
            "destination_warehouse_id": destination_id,
            "quantity": 5,
            "reason": "Rebalance stock",
        },
    )
    assert replay.status_code == 200
    assert replay.json() == response.json()
    assert get_inventory(c, destination_id)[0].available_quantity == 5


def test_order_reservation_calculation_release_cancel_and_idempotency(test_context):
    c = test_context
    client = c["client"]
    request = {
        "items": [{"product_id": c["product_id"], "quantity": 3, "discount": "2.00"}],
    }
    headers = {**c["customer_headers"], "Idempotency-Key": "order-snapshot"}
    created = client.post("/orders", headers=headers, json=request)
    assert created.status_code == 201, created.text
    order = created.json()
    assert float(order["total_amount"]) == 28
    assert float(order["items"][0]["unit_price"]) == 10
    replay = client.post("/orders", headers=headers, json=request)
    assert replay.status_code == 201 and replay.json()["id"] == order["id"]
    mismatch = client.post(
        "/orders", headers=headers,
        json={"items": [{"product_id": c["product_id"], "quantity": 2}]},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "DUPLICATE_IDEMPOTENCY_KEY"
    assert (get_inventory(c, c["warehouse_a"])[0].available_quantity,
            get_inventory(c, c["warehouse_a"])[0].reserved_quantity) == (7, 3)
    cancelled = client.post(f"/orders/{order['id']}/cancel", headers=c["customer_headers"])
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    balance = get_inventory(c, c["warehouse_a"])[0]
    assert (balance.available_quantity, balance.reserved_quantity) == (10, 0)
    with Session(c["engine"]) as db:
        assert db.scalar(select(InventoryTransaction).where(InventoryTransaction.transaction_type == "release"))
        assert db.scalar(select(OrderStatusHistory).where(
            OrderStatusHistory.order_id == order["id"], OrderStatusHistory.to_status == "cancelled"
        ))
        assert db.scalar(select(Notification).where(
            Notification.user_id == c["customer_id"], Notification.event_type == "order_cancelled"
        ))
        product = db.get(Product, c["product_id"])
        product.price = 99
        db.commit()
    fetched = client.get(f"/orders/{order['id']}", headers=c["customer_headers"])
    assert float(fetched.json()["items"][0]["unit_price"]) == 10
    assert client.post(f"/orders/{order['id']}/cancel", headers=c["customer_headers"]).status_code == 409


def test_insufficient_inventory_and_failed_order_rolls_back_all_changes(test_context):
    c = test_context
    client = c["client"]
    too_many = create_order(client, c["customer_headers"], c["warehouse_a"], c["product_id"],
                            quantity=11, key="too-many")
    assert too_many.status_code == 409
    assert too_many.json()["error"]["code"] == "INSUFFICIENT_INVENTORY"
    balance = get_inventory(c, c["warehouse_a"])[0]
    assert (balance.available_quantity, balance.reserved_quantity) == (10, 0)
    with Session(c["engine"]) as db:
        extra_product = Product(sku="SKU-NO-STOCK", name="No stock", price=2, cost_price=1)
        db.add(extra_product)
        db.commit()
        extra_product_id = extra_product.id
    partial_failure = client.post(
        "/orders",
        headers={**c["customer_headers"], "Idempotency-Key": "partial-rollback"},
        json={
            "items": [
                {"product_id": c["product_id"], "quantity": 2},
                {"product_id": extra_product_id, "quantity": 1},
            ],
        },
    )
    assert partial_failure.status_code == 409
    assert partial_failure.json()["error"]["code"] == "INSUFFICIENT_INVENTORY"
    balance_after_rollback = get_inventory(c, c["warehouse_a"])[0]
    assert (balance_after_rollback.available_quantity, balance_after_rollback.reserved_quantity) == (10, 0)
    with Session(c["engine"]) as db:
        assert db.scalar(select(InventoryTransaction).where(
            InventoryTransaction.product_id == c["product_id"],
            InventoryTransaction.transaction_type == "reservation",
        )) is None
    rejected_warehouse_selection = client.post(
        "/orders",
        headers=c["customer_headers"],
        json={
            "warehouse_id": 9999,
            "items": [{"product_id": c["product_id"], "quantity": 1}],
        },
    )
    assert rejected_warehouse_selection.status_code == 422


def test_sequential_competing_reservations_enforce_inventory_limit(test_context):
    c = test_context
    client = c["client"]
    first = create_order(
        client, c["customer_headers"], c["warehouse_a"], c["product_id"], quantity=7, key="first"
    )
    second = create_order(
        client, c["other_customer_headers"], c["warehouse_a"], c["product_id"], quantity=7, key="second"
    )
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "INSUFFICIENT_INVENTORY"
    balance = get_inventory(c, c["warehouse_a"])[0]
    assert (balance.available_quantity, balance.reserved_quantity) == (3, 7)


def test_order_transitions_assignment_reassignment_and_cancel_boundaries(test_context):
    c = test_context
    client = c["client"]
    order = create_order(
        client, c["customer_headers"], c["warehouse_a"], c["product_id"], key="state-order"
    ).json()
    unassigned_attempt = client.post(
        f"/fulfillment/orders/{order['id']}/accept", headers=c["agent_headers"]
    )
    assert unassigned_attempt.status_code == 403
    # The current agent must have the assigned warehouse, and inactive agents are rejected.
    inactive = client.post(
        f"/orders/{order['id']}/assign", headers=c["manager_headers"],
        json={"agent_id": c["inactive_agent_id"]},
    )
    assert inactive.status_code == 422
    progress_order(c, order["id"], statuses=("accept", "start-processing"))
    assert client.post(f"/orders/{order['id']}/cancel", headers=c["customer_headers"]).status_code == 409
    assert client.put(
        f"/orders/{order['id']}", headers=c["admin_headers"], json={"status": "delivered"}
    ).status_code == 409
    valid = client.post(f"/fulfillment/orders/{order['id']}/pack", headers=c["agent_headers"])
    assert valid.status_code == 200
    reassigned = client.post(
        f"/orders/{order['id']}/reassign", headers=c["manager_headers"],
        json={"agent_id": c["agent_id"]},
    )
    assert reassigned.status_code == 200
    with Session(c["engine"]) as db:
        assignments = db.scalars(select(OrderAssignment).where(OrderAssignment.order_id == order["id"])).all()
        assert len(assignments) == 2
    assigned_to_me = client.get(
        "/fulfillment/orders/assigned-to-me", headers=c["agent_headers"]
    )
    assert assigned_to_me.status_code == 200, assigned_to_me.text
    assert order["id"] in {assigned_order["id"] for assigned_order in assigned_to_me.json()}
    assert client.post(f"/fulfillment/orders/{order['id']}/ship", headers=c["agent_headers"]).status_code == 200
    assert client.post(f"/fulfillment/orders/{order['id']}/ship", headers=c["agent_headers"]).status_code == 409


def test_product_price_snapshot_and_shipment_consumes_reservation(test_context):
    c = test_context
    client = c["client"]
    order = create_order(
        client, c["customer_headers"], c["warehouse_a"], c["product_id"], key="ship-order"
    ).json()
    with Session(c["engine"]) as db:
        product = db.get(Product, c["product_id"])
        product.price = 50
        db.commit()
    assert float(client.get(
        f"/orders/{order['id']}", headers=c["customer_headers"]
    ).json()["items"][0]["unit_price"]) == 10
    progress_order(c, order["id"], statuses=("accept", "start-processing", "pack", "ship"))
    balance = get_inventory(c, c["warehouse_a"])[0]
    assert (balance.available_quantity, balance.reserved_quantity) == (9, 0)
    with Session(c["engine"]) as db:
        assert db.scalar(select(InventoryTransaction).where(
            InventoryTransaction.transaction_type == "sale"
        ))


def test_return_create_approve_receive_refund_and_invalid_transitions(test_context):
    c = test_context
    client = c["client"]
    order = create_order(
        client, c["customer_headers"], c["warehouse_a"], c["product_id"],
        quantity=2, key="return-approve",
    ).json()
    progress_order(c, order["id"])
    item_id = order["items"][0]["id"]
    body = {
        "order_id": order["id"], "reason": "Damaged",
        "items": [{"order_item_id": item_id, "quantity": 2}],
    }
    created = client.post("/returns", headers=c["customer_headers"], json=body)
    assert created.status_code == 201
    returned = created.json()
    assert client.post(
        f"/returns/{returned['id']}/receive", headers=c["manager_headers"]
    ).status_code == 409
    approved = client.post(f"/returns/{returned['id']}/approve", headers=c["manager_headers"])
    assert approved.status_code == 200
    received = client.post(f"/returns/{returned['id']}/receive", headers=c["manager_headers"])
    assert received.status_code == 200
    assert client.post(
        f"/returns/{returned['id']}/approve", headers=c["manager_headers"]
    ).status_code == 409
    refund = client.post(f"/returns/{returned['id']}/refund", headers=c["manager_headers"])
    assert refund.status_code == 200
    assert client.post(
        f"/returns/{returned['id']}/refund", headers=c["manager_headers"]
    ).status_code == 409
    balance = get_inventory(c, c["warehouse_a"])[0]
    assert balance.available_quantity == 10
    with Session(c["engine"]) as db:
        assert db.scalar(select(InventoryTransaction).where(
            InventoryTransaction.transaction_type == "return"
        ))


def test_return_rejection_and_cannot_reopen_rejected_request(test_context):
    c = test_context
    client = c["client"]
    order = create_order(
        client, c["customer_headers"], c["warehouse_a"], c["product_id"], key="return-reject"
    ).json()
    progress_order(c, order["id"])
    created = client.post("/returns", headers=c["customer_headers"], json={
        "order_id": order["id"], "reason": "Changed mind",
        "items": [{"order_item_id": order["items"][0]["id"], "quantity": 1}],
    })
    assert created.status_code == 201
    return_id = created.json()["id"]
    assert client.post(f"/returns/{return_id}/reject", headers=c["manager_headers"]).status_code == 200
    assert client.post(f"/returns/{return_id}/approve", headers=c["manager_headers"]).status_code == 409
    assert client.post(f"/returns/{return_id}/receive", headers=c["manager_headers"]).status_code == 409


def test_return_resource_authorization_and_invalid_items(test_context):
    c = test_context
    client = c["client"]
    order = create_order(
        client, c["customer_headers"], c["warehouse_a"], c["product_id"], key="return-private"
    ).json()
    progress_order(c, order["id"])
    bad = client.post("/returns", headers=c["customer_headers"], json={
        "order_id": order["id"], "reason": "invalid order item",
        "items": [{"order_item_id": 99999, "quantity": 1}],
    })
    assert bad.status_code == 422
    created = client.post("/returns", headers=c["customer_headers"], json={
        "order_id": order["id"], "reason": "return",
        "items": [{"order_item_id": order["items"][0]["id"], "quantity": 1}],
    })
    assert created.status_code == 201
    assert client.get(
        f"/returns/{created.json()['id']}", headers=c["other_customer_headers"]
    ).status_code == 404
    assert client.post(
        f"/returns/{created.json()['id']}/approve", headers=c["customer_headers"]
    ).status_code == 403


def test_local_demo_payment_confirmation_is_idempotent(test_context):
    c = test_context
    client = c["client"]
    order = create_order(
        client, c["customer_headers"], c["warehouse_a"], c["product_id"], key="payment-order"
    ).json()
    headers = {**c["customer_headers"], "Idempotency-Key": "payment-confirm-1"}

    confirmed = client.post(f"/payments/{order['id']}/confirm", headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["payment_status"] == "paid"
    assert confirmed.json()["provider"] == "local_demo"
    assert client.post(f"/payments/{order['id']}/confirm", headers=headers).json() == confirmed.json()

    with Session(c["engine"]) as db:
        persisted = db.get(Order, order["id"])
        assert persisted.payment_status == "paid"
        notifications = db.scalars(
            select(Notification).where(Notification.user_id == c["customer_id"])
        ).all()
        assert sum(item.event_type == "payment_confirmed" for item in notifications) == 1

    payment = client.get(f"/payments/{order['id']}", headers=c["customer_headers"])
    assert payment.json()["payment_status"] == "paid"


def test_payment_confirmation_without_key_is_safe_after_order_without_key(test_context):
    c = test_context
    client = c["client"]
    created = client.post(
        "/orders",
        headers=c["customer_headers"],
        json={"items": [{"product_id": c["product_id"], "quantity": 1}]},
    )
    assert created.status_code == 201, created.text
    order_id = created.json()["id"]

    first = client.post(f"/payments/{order_id}/confirm", headers=c["customer_headers"])
    assert first.status_code == 200, first.text
    assert first.json()["payment_status"] == "paid"
    retry = client.post(f"/payments/{order_id}/confirm", headers=c["customer_headers"])
    assert retry.status_code == 200, retry.text
    assert retry.json() == first.json()

    with Session(c["engine"]) as db:
        assert len(db.scalars(select(Notification).where(
            Notification.user_id == c["customer_id"],
            Notification.event_type == "payment_confirmed",
        )).all()) == 1
        assert len(db.scalars(select(AuditLog).where(
            AuditLog.user_id == c["customer_id"],
            AuditLog.action == "payment_confirmed",
        )).all()) == 1


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRESQL_URL"),
    reason="Append-only database triggers are installed only on PostgreSQL",
)
def test_postgres_database_rejects_inventory_and_audit_mutations(test_context):
    c = test_context
    with Session(c["engine"]) as db:
        transaction = InventoryTransaction(
            product_id=c["product_id"], warehouse_id=c["warehouse_a"],
            transaction_type="adjustment", quantity=1, user_id=c["admin_id"],
        )
        audit_log = AuditLog(
            user_id=c["admin_id"], action="test", entity="inventory", entity_id="1",
            metadata_json={},
        )
        db.add_all([transaction, audit_log])
        db.commit()
        transaction_id, audit_id = transaction.id, audit_log.id

        for table, record_id, table_column in (
            ("inventory_transactions", transaction_id, "id"),
            ("audit_logs", audit_id, "id"),
        ):
            with pytest.raises(DBAPIError):
                db.execute(text(f"UPDATE {table} SET created_at = created_at WHERE {table_column} = :id")
                           if table == "inventory_transactions"
                           else text(f"UPDATE {table} SET action = action WHERE {table_column} = :id"),
                           {"id": record_id})
            db.rollback()
            with pytest.raises(DBAPIError):
                db.execute(text(f"DELETE FROM {table} WHERE {table_column} = :id"), {"id": record_id})
            db.rollback()


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRESQL_URL"),
    reason="Set TEST_POSTGRESQL_URL to an isolated disposable PostgreSQL test database for true concurrency coverage",
)
def test_concurrent_reservations_postgresql():
    engine = create_engine(os.environ["TEST_POSTGRESQL_URL"], poolclass=NullPool, pool_pre_ping=True)
    try:
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            roles = [
                RoleRecord(name="admin", description="Admin"),
                RoleRecord(name="warehouse_manager", description="Manager"),
                RoleRecord(name="fulfillment_agent", description="Agent"),
                RoleRecord(name="customer", description="Customer"),
            ]
            db.add_all(roles)
            users = [
                User(email=f"race-{number}@example.com", full_name=f"Customer {number}",
                     password_hash="unused", role="customer")
                for number in (1, 2)
            ]
            warehouse = Warehouse(name="Concurrency Test", location="isolated", capacity=10)
            product = Product(sku="RACE-1", name="Single item", price=1, cost_price=1)
            db.add_all([*users, warehouse, product])
            db.flush()
            db.add(Inventory(product_id=product.id, warehouse_id=warehouse.id,
                             available_quantity=1, reorder_level=0))
            user_ids, warehouse_id, product_id = [user.id for user in users], warehouse.id, product.id
            db.commit()

        barrier = Barrier(2)

        def attempt_reservation(customer_id):
            with Session(engine) as db:
                customer = db.get(User, customer_id)
                barrier.wait(timeout=10)
                try:
                    order = create_order_service(
                        db, customer, warehouse_id, [{"product_id": product_id, "quantity": 1}]
                    )
                    db.commit()
                    return ("reserved", order.id)
                except Exception as error:
                    db.rollback()
                    return ("rejected", getattr(error, "code", type(error).__name__))

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt_reservation, user_ids))
        assert sum(result[0] == "reserved" for result in results) == 1
        assert sum(result[0] == "rejected" for result in results) == 1
        with Session(engine) as db:
            balance = db.scalar(select(Inventory).where(Inventory.product_id == product_id))
            assert (balance.available_quantity, balance.reserved_quantity) == (0, 1)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
