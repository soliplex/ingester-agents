"""
FastAPI server for Soliplex Agents.

Provides REST API endpoints for filesystem, SCM, and WebDAV ingestion agents.
"""

import importlib
import logging

from fastapi import APIRouter
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from soliplex.agents.config import settings

from .routes.fs import fs_router
from .routes.scm import scm_router
from .routes.webdav import webdav_router

logger = logging.getLogger(__name__)


async def lifespan(app: FastAPI):
    """Manage app lifecycle."""
    logging.basicConfig(level=settings.log_level)
    logger.info("Starting soliplex-agents server")
    if settings.api_prefix:
        logger.info(f"API prefix: {settings.api_prefix}")
    if settings.root_path:
        logger.info(f"Root path: {settings.root_path}")
    yield
    logger.info("soliplex-agents server stopped")


app = FastAPI(
    title="Soliplex Agents API",
    description="REST API for Soliplex document ingestion agents",
    version="0.1.0",
    lifespan=lifespan,
    root_path=settings.root_path or "",
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create parent router with configurable prefix for all API routes
api_router = APIRouter(prefix=settings.api_prefix or "")

# Include sub-routers
api_router.include_router(fs_router)
api_router.include_router(scm_router)
api_router.include_router(webdav_router)


# Health check endpoint (no auth required, under the prefix)
@api_router.get("/health", tags=["health"])
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


# start schedules if needed
if settings.scheduler_enabled:
    from fastapi_crons import Crons
    from fastapi_crons import SQLiteStateBackend
    from fastapi_crons import get_cron_router

    # Custom database path
    state_backend = SQLiteStateBackend(db_path=":memory:")
    crons = Crons(app, state_backend=state_backend)
    app.include_router(get_cron_router())

    @crons.cron("*/1 * * * *", name="run_scheduled_jobs")
    async def run_jobs():
        logger.info("start jobs")
        if settings.scheduler_modules:
            for module_name in settings.scheduler_modules:
                try:
                    module = importlib.import_module(module_name)
                    await module.run_schedule_minute()
                except Exception as e:
                    logger.exception(f"Error running module {module_name}", exc_info=e)
        logger.info("end jobs")


# Include the parent router in the app
app.include_router(api_router)
