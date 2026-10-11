from fastapi import FastAPI

from app.config import settings
from app.core.exception_handlers import register_exception_handlers
from app.routers import audit_logs, auth, catalog, dashboard, inventory, notifications, orders, returns
from app.routers import settings as settings_router

app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="Secure order fulfillment and inventory management REST API.",
)
register_exception_handlers(app)

app.include_router(auth.router, prefix="/auth", tags=["Authentication"])
app.include_router(catalog.users_router, tags=["Users"])
app.include_router(catalog.products_router, tags=["Products"])
app.include_router(catalog.categories_router, tags=["Categories"])
app.include_router(catalog.warehouses_router, tags=["Warehouses"])
app.include_router(inventory.router, tags=["Inventory"])
app.include_router(orders.orders_router, tags=["Orders"])
app.include_router(orders.fulfillment_router, tags=["Fulfillment"])
app.include_router(orders.payments_router, tags=["Payments"])
app.include_router(returns.router, tags=["Returns"])
app.include_router(notifications.router, tags=["Notifications"])
app.include_router(dashboard.router, tags=["Dashboard"])
app.include_router(audit_logs.router, tags=["Audit logs"])
app.include_router(settings_router.router, tags=["Settings"])
