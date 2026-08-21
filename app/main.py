from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from contextlib import asynccontextmanager
import logging

# Import all routers
from app.api import auth, sellers, orders, supplies, kiz, audit, debug, qa
from app.api.auth import get_current_active_user, require_admin
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
cors_origins_list = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
if not cors_origins_list:
    cors_origins_list = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Authentication router (public login endpoint + user management)
app.include_router(auth.router, prefix="/api/v1")

# Protected business routers (require active authenticated user)
app.include_router(
    sellers.router, 
    prefix="/api/v1", 
    dependencies=[Depends(get_current_active_user)]
)
app.include_router(
    orders.router, 
    prefix="/api/v1", 
    dependencies=[Depends(get_current_active_user)]
)
app.include_router(
    supplies.router, 
    prefix="/api/v1", 
    dependencies=[Depends(get_current_active_user)]
)
app.include_router(
    kiz.router, 
    prefix="/api/v1", 
    dependencies=[Depends(get_current_active_user)]
)
app.include_router(
    audit.router, 
    prefix="/api/v1", 
    dependencies=[Depends(get_current_active_user)]
)

# Debug and QA routers (only available if DEBUG=True and require Admin role)
if settings.debug:
    logger.info("DEBUG mode enabled: mounting /debug and /qa endpoints with Admin protection.")
    app.include_router(
        debug.router, 
        prefix="/api/v1", 
        dependencies=[Depends(require_admin)]
    )
    app.include_router(
        qa.router, 
        prefix="/api/v1", 
        dependencies=[Depends(require_admin)]
    )
else:
    logger.info("Production mode: /debug and /qa endpoints disabled.")

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
    from app.services.time_service import get_server_time_info
    return {
        "status": "ok",
        "version": settings.app_version,
        "service": settings.app_name,
        "server_time": get_server_time_info(),
    }
