from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.constants import Role
from app.core.errors import DomainError
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models import TaxConfiguration, User
from app.schemas import TaxRateInput
from app.services.common import audit

router = APIRouter(prefix="/settings")


@router.get("/tax-rate", dependencies=[Depends(get_current_user)])
def get_tax_rate(db: Session = Depends(get_db)):
    configuration = db.get(TaxConfiguration, 1)
    return {"rate_percent": configuration.rate_percent if configuration else 0}


@router.put("/tax-rate")
def update_tax_rate(
    payload: TaxRateInput,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role != Role.ADMIN.value:
        raise DomainError("FORBIDDEN", "Only administrators can change the tax rate", 403)
    configuration = db.scalar(
        select(TaxConfiguration).where(TaxConfiguration.id == 1).with_for_update()
    )
    if configuration is None:
        configuration = TaxConfiguration(id=1, rate_percent=payload.rate_percent)
        db.add(configuration)
    else:
        configuration.rate_percent = payload.rate_percent
    audit(
        db,
        user.id,
        "tax_rate_updated",
        "tax_configuration",
        1,
        {"rate_percent": str(payload.rate_percent)},
    )
    db.commit()
    db.refresh(configuration)
    return {"rate_percent": configuration.rate_percent}
