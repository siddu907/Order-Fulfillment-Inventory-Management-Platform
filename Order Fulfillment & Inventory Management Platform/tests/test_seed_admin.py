import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import seed_admin
from app.core.security import verify_password
from app.database import Base
from app.models import RoleRecord, User


def test_create_admin_uses_current_user_and_role_models(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        seed_admin,
        "SessionLocal",
        lambda: Session(engine, expire_on_commit=False),
    )

    try:
        seed_admin.create_admin(" ADMIN@example.com ", " Platform Admin ", "strong-password")

        with Session(engine) as db:
            admin = db.query(User).filter_by(email="admin@example.com").one()
            assert admin.full_name == "Platform Admin"
            assert admin.role == "admin"
            assert verify_password("strong-password", admin.password_hash)
            assert db.get(RoleRecord, "admin") is not None

        with pytest.raises(ValueError, match="An admin already exists"):
            seed_admin.create_admin("another@example.com", "Second Admin", "strong-password")
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
