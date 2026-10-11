from collections.abc import Generator
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool, StaticPool

from app.core.security import create_access_token, hash_password
from app.database import Base, get_db
from app.main import app
from app.models import Inventory, Product, RoleRecord, User, Warehouse


@pytest.fixture
def test_context() -> Generator[dict, None, None]:
    postgres_url = os.getenv("TEST_POSTGRESQL_URL")
    engine = (
        create_engine(postgres_url, poolclass=NullPool, pool_pre_ping=True)
        if postgres_url
        else create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    )
    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(text("""
                CREATE OR REPLACE FUNCTION reject_immutable_ledger_mutation()
                RETURNS trigger AS $$
                BEGIN
                    RAISE EXCEPTION 'table % is append-only; % is not permitted', TG_TABLE_NAME, TG_OP
                        USING ERRCODE = '55000';
                END;
                $$ LANGUAGE plpgsql
            """))
            for table in ("inventory_transactions", "audit_logs"):
                connection.execute(text(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}"))
                connection.execute(text(
                    f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                    "FOR EACH ROW EXECUTE FUNCTION reject_immutable_ledger_mutation()"
                ))

    def override_get_db():
        with Session(engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with Session(engine, expire_on_commit=False) as db:
        for role, description in (
            ("admin", "Platform administrator"),
            ("warehouse_manager", "Warehouse manager"),
            ("fulfillment_agent", "Fulfillment agent"),
            ("customer", "Customer"),
        ):
            if db.get(RoleRecord, role) is None:
                db.add(RoleRecord(name=role, description=description))
        admin = User(
            email="admin@example.com", full_name="Admin",
            password_hash=hash_password("admin-password-123"), role="admin",
        )
        customer = User(
            email="customer@example.com", full_name="Customer",
            password_hash=hash_password("customer-password-123"), role="customer",
        )
        other_customer = User(
            email="other@example.com", full_name="Other Customer",
            password_hash=hash_password("other-password-123"), role="customer",
        )
        manager = User(
            email="manager@example.com", full_name="Manager",
            password_hash=hash_password("manager-password-123"), role="warehouse_manager",
        )
        agent = User(
            email="agent@example.com", full_name="Agent",
            password_hash=hash_password("agent-password-123"), role="fulfillment_agent",
        )
        inactive_agent = User(
            email="inactive-agent@example.com", full_name="Inactive Agent",
            password_hash=hash_password("inactive-password-123"), role="fulfillment_agent", is_active=False,
        )
        warehouse_a = Warehouse(name="North", location="North", capacity=1000)
        warehouse_b = Warehouse(name="South", location="South", capacity=1000)
        warehouse_unassigned = Warehouse(name="East", location="East", capacity=1000)
        db.add_all([
            admin, customer, other_customer, manager, agent, inactive_agent,
            warehouse_a, warehouse_b, warehouse_unassigned,
        ])
        db.flush()
        warehouse_a.users.extend([manager, agent])
        warehouse_b.users.append(manager)
        product = Product(sku="SKU-1001", name="Widget", price=10, cost_price=4)
        db.add(product)
        db.flush()
        db.add_all(
            [
                Inventory(product_id=product.id, warehouse_id=warehouse_a.id, available_quantity=10, reorder_level=2),
                Inventory(product_id=product.id, warehouse_id=warehouse_b.id, available_quantity=3, reorder_level=1),
            ]
        )
        db.commit()
        data = {
            "admin_id": admin.id,
            "customer_id": customer.id,
            "other_customer_id": other_customer.id,
            "manager_id": manager.id,
            "agent_id": agent.id,
            "inactive_agent_id": inactive_agent.id,
            "warehouse_a": warehouse_a.id,
            "warehouse_b": warehouse_b.id,
            "warehouse_unassigned": warehouse_unassigned.id,
            "product_id": product.id,
        }

    client = TestClient(app)
    data["client"] = client
    data["engine"] = engine
    for role in ("admin", "customer", "other_customer", "manager", "agent", "inactive_agent"):
        token_role = role if role not in ("manager", "inactive_agent") else (
            "warehouse_manager" if role == "manager" else "fulfillment_agent"
        )
        token, _ = create_access_token(data[f"{role}_id"], token_role)
        data[f"{role}_headers"] = {"Authorization": "Bearer " + token}
    try:
        yield data
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()
