from fastapi import FastAPI

from app.api.routes.health import router as health_router
from app.api.routes.payments import router as payments_router
from app.api.routes.simulation import router as simulation_router
from app.core.config import get_settings

settings = get_settings()
app = FastAPI(
    title=settings.app_name,
    description="Educational payment orchestration simulation using synthetic data only.",
    version="0.1.0",
)
app.include_router(health_router)
app.include_router(payments_router)
app.include_router(simulation_router)
