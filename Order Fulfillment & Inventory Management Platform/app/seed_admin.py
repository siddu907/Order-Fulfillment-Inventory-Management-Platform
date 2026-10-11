
import os
from getpass import getpass

from app.database import SessionLocal
from app.core.security import hash_password
from app.models import RoleRecord, User


def create_admin(email: str, name: str, password: str) -> None:
    email = email.strip().lower()
    name = name.strip()
    if not email or not name or not password:
        raise ValueError("Email, name, and password are required.")

    db = SessionLocal()

    try:
        existing_admin = db.query(User).filter(User.role == "admin").first()
        if existing_admin:
            raise ValueError("An admin already exists. Only one admin is allowed.")

        existing_user = db.query(User).filter(User.email == email).first()
        if existing_user:
            raise ValueError(f"User with email '{email}' already exists.")

        admin_role = db.get(RoleRecord, "admin")
        if admin_role is None:
            admin_role = RoleRecord(name="admin", description="Platform administrator")
            db.add(admin_role)
            db.flush()

        admin = User(
            full_name=name,
            email=email,
            password_hash=hash_password(password),
            role=admin_role.name,
            is_active=True,
        )

        db.add(admin)
        db.commit()
        db.refresh(admin)

        print(f"Admin created successfully: {admin.email}")
        print(f"Admin ID: {admin.id}")
        print(f"Role: {admin.role}")

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


if __name__ == "__main__":
    admin_name = (
        os.getenv("ADMIN_NAME", "").strip()
        or input("Enter admin full name: ").strip()
    )

    admin_email = (
        os.getenv("ADMIN_EMAIL", "").strip().lower()
        or input("Enter admin email: ").strip().lower()
    )

    admin_password = (
        os.getenv("ADMIN_PASSWORD", "")
        or getpass("Enter admin password: ")
    )

    if not admin_name or not admin_email or not admin_password:
        raise SystemExit(
            "ADMIN_NAME, ADMIN_EMAIL, and ADMIN_PASSWORD must be provided."
        )

    try:
        create_admin(
            admin_email,
            admin_name,
            admin_password,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
