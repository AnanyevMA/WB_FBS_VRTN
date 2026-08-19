from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from contextlib import asynccontextmanager
import logging

# Import all routers
from app.api import sellers, orders, supplies, kiz, audit, debug, qa
from app.database import init_db
from app.config import settings

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting up WB FBS Manager API...")
    await init_db()
    yield
    # Shutdown
    logger.info("Shutting down WB FBS Manager API...")

app = FastAPI(
    title="WB FBS Manager API",
    description="Мультитенантный сервис управления FBS заказами WB + Честный Знак",
    version=settings.app_version,
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # allow all origins for now
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(sellers.router, prefix="/api/v1")
app.include_router(orders.router, prefix="/api/v1")
app.include_router(supplies.router, prefix="/api/v1")
app.include_router(kiz.router, prefix="/api/v1")
app.include_router(audit.router, prefix="/api/v1")
app.include_router(debug.router, prefix="/api/v1")
app.include_router(qa.router, prefix="/api/v1")

# Mount /static for frontend files
try:
    app.mount("/static", StaticFiles(directory="./frontend"), name="static")
except RuntimeError:
    logger.warning("Directory './frontend' does not exist, static files not mounted.")

@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/static/index.html")

@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "version": settings.app_version,
        "service": settings.app_name,
    }
