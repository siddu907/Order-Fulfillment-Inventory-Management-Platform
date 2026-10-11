from sqlalchemy.orm import Session

from app.models import AuditLog, Notification


def audit(
    db: Session,
    user_id: int | None,
    action: str,
    entity: str,
    entity_id: int | str | None,
    metadata: dict | None = None,
    ip_address: str | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id is not None else None,
            metadata_json=metadata or {},
            ip_address=ip_address,
        )
    )


def notify(db: Session, user_id: int, event_type: str, message: str, entity: str | None = None,
           entity_id: int | str | None = None) -> None:
    notification = Notification(
        user_id=user_id,
        event_type=event_type,
        message=message,
        entity_type=entity,
        entity_id=str(entity_id) if entity_id is not None else None,
    )
    db.add(notification)
