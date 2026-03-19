"""
FastAPI server for Soliplex Agents.

Provides REST API endpoints for filesystem, SCM, and WebDAV ingestion agents.
"""

import asyncio
import importlib
import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from soliplex.agents.config import configure_logging
from soliplex.agents.config import settings

from .routes.fs import fs_router
from .routes.manifest import manifest_router
from .routes.scm import scm_router
from .routes.web import web_router
from .routes.webdav import webdav_router

logger = logging.getLogger(__name__)


async def _run_manifest_at_startup(manifest_path: str) -> None:
    """Run a single manifest and log the outcome."""
    from soliplex.agents.manifest import runner as manifest_runner

    try:
        m = manifest_runner.load_manifest(manifest_path)
        result = await manifest_runner.run_manifest(m)
        count = len(result.get("results", []))
        logger.info(
            "Startup manifest '%s' completed: %d components",
            m.id,
            count,
        )
    except Exception:
        logger.exception("Error running startup manifest %s", manifest_path)


def setup_manifest_schedules(crons) -> None:
    """Register cron jobs for scheduled manifests and fire-and-forget
    tasks for unscheduled ones.

    Args:
        crons: A ``fastapi_crons.Crons`` instance used to register
            cron handlers.
    """
    from soliplex.agents.manifest import runner as manifest_runner

    if not settings.manifest_dir:
        return

    manifest_path = Path(settings.manifest_dir)
    if not manifest_path.is_dir():
        logger.warning(
            "manifest_dir is not a directory: %s",
            settings.manifest_dir,
        )
        return

    try:
        pairs = manifest_runner.load_manifests_with_paths(settings.manifest_dir)
    except ValueError:
        logger.exception("Error loading manifests from directory")
        return

    for m_obj, yml_path in pairs:
        if m_obj.schedule:

            def make_handler(mpath):
                async def handler():
                    loaded = manifest_runner.load_manifest(mpath)
                    result = await manifest_runner.run_manifest(loaded)
                    logger.info(
                        "Manifest %s completed: %d components",
                        mpath,
                        len(result.get("results", [])),
                    )

                return handler

            crons.cron(
                m_obj.schedule.cron,
                name=f"manifest_{m_obj.id}",
            )(make_handler(yml_path))
            logger.info(
                "Scheduled manifest '%s' cron='%s'",
                m_obj.id,
                m_obj.schedule.cron,
            )
        else:
            asyncio.create_task(
                _run_manifest_at_startup(yml_path),
                name=f"startup_manifest_{m_obj.id}",
            )
            logger.info(
                "Queued startup run for manifest '%s'",
                m_obj.id,
            )


async def lifespan(app: FastAPI):
    """Manage app lifecycle."""

    configure_logging()
    logger.info("Starting soliplex-agents server")
    if settings.api_prefix:
        logger.info(f"API prefix: {settings.api_prefix}")
    if settings.root_path:
        logger.info(f"Root path: {settings.root_path}")

    if settings.scheduler_enabled:
        setup_manifest_schedules(_crons)

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
api_router.include_router(manifest_router)
api_router.include_router(scm_router)
api_router.include_router(web_router)
api_router.include_router(webdav_router)


# Health check endpoint (no auth required, under the prefix)
@api_router.get("/health", tags=["health"])
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


# Initialise the cron scheduler (module-level so lifespan can use it)
_crons = None
if settings.scheduler_enabled:
    from fastapi_crons import Crons
    from fastapi_crons import SQLiteStateBackend
    from fastapi_crons import get_cron_router

    state_backend = SQLiteStateBackend(db_path=":memory:")
    _crons = Crons(app, state_backend=state_backend)
    app.include_router(get_cron_router())

    @_crons.cron("*/1 * * * *", name="run_scheduled_jobs")
    async def run_jobs():
        logger.info("start jobs")
        if settings.scheduler_modules:
            for module_name in settings.scheduler_modules:
                try:
                    module = importlib.import_module(module_name)
                    await module.run_schedule_minute()
                except Exception as e:
                    logger.exception(
                        f"Error running module {module_name}",
                        exc_info=e,
                    )
        logger.info("end jobs")


# Include the parent router in the app
app.include_router(api_router)
