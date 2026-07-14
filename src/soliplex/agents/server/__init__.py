"""
FastAPI server for Soliplex Agents.

Provides REST API endpoints for filesystem, SCM, and WebDAV ingestion agents.
"""

import asyncio
import logging
from datetime import UTC
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from soliplex.agents.config import configure_logging
from soliplex.agents.config import settings
from soliplex.agents.manifest.schedule_registry import ScheduleRegistry

from .haiku_queue import enqueue_load
from .haiku_queue import start_worker
from .haiku_queue import stop_worker
from .locks import get_global_manifest_semaphore
from .locks import get_manifest_lock
from .locks import is_any_manifest_running
from .locks import is_manifest_running
from .routes.fs import fs_router
from .routes.manifest import manifest_router
from .routes.scm import scm_router
from .routes.web import web_router
from .routes.webdav import webdav_router

logger = logging.getLogger(__name__)

# Registry of manifest schedules, reconciled against the manifest directory
# on each tick so schedule edits and added/removed files hot-reload without
# a restart.
_schedule_registry = ScheduleRegistry()


async def run_scheduled_manifest(manifest_id: str, path: str) -> None:
    """Execute one manifest, honoring the global one-at-a-time lock.

    Skips (rather than queues) when another manifest is already running so
    frequent schedules don't pile up. Failures are logged, not raised, since
    this is invoked as a fire-and-forget task.

    Args:
        manifest_id: The manifest's id (used for locking and logging).
        path: Path to the manifest YAML file, reloaded fresh on each run.
    """
    from soliplex.agents.manifest import runner as manifest_runner

    if is_manifest_running(manifest_id):
        logger.warning(
            "Skipping manifest '%s': previous run still in progress",
            manifest_id,
        )
        return
    if is_any_manifest_running():
        logger.info(
            "Skipping manifest '%s': another manifest is running",
            manifest_id,
        )
        return

    try:
        async with get_global_manifest_semaphore():
            lock = get_manifest_lock(manifest_id)
            async with lock:
                loaded = manifest_runner.load_manifest(path)
                result = await manifest_runner.run_manifest(loaded)
                logger.info(
                    "Manifest '%s' completed: %d components",
                    manifest_id,
                    len(result.get("results", [])),
                )
        if settings.haiku_load_enabled:
            await enqueue_load(loaded)
    except Exception:
        logger.exception("Error running manifest '%s'", manifest_id)


async def reconcile_manifest_schedules() -> None:
    """Rescan the manifest directory and fire due/newly-added manifests.

    Runs on a fixed interval (see ``scheduler_reconcile_cron``) so that
    added, removed, and re-scheduled manifest files take effect without a
    restart. Scheduled manifests fire when due; manifests without a schedule
    run once when first seen.
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
        # e.g. a transient duplicate id mid-edit -- keep the last good state.
        logger.exception("Error loading manifests; skipping this reconcile")
        return

    result = _schedule_registry.reconcile(pairs, datetime.now(UTC))

    for entry in result.added:
        if entry.cron_expr is not None:
            logger.info(
                "Scheduled manifest '%s' cron='%s'",
                entry.manifest_id,
                entry.cron_expr,
            )
        else:
            logger.info(
                "Registered manifest '%s' (no schedule; one-time run)",
                entry.manifest_id,
            )
    for entry in result.rescheduled:
        logger.info(
            "Rescheduled manifest '%s' cron='%s'",
            entry.manifest_id,
            entry.cron_expr,
        )
    for mid in result.removed:
        logger.info("Unregistered manifest '%s' (file removed)", mid)

    for entry in result.to_run:
        asyncio.create_task(
            run_scheduled_manifest(entry.manifest_id, entry.path),
            name=f"manifest_run_{entry.manifest_id}",
        )


def configure_logfire(app: FastAPI) -> None:
    """Configure Pydantic Logfire for the server process.

    Only active when a token is available (read from
    ``/run/secrets/logfire_token`` or the ``LOGFIRE_TOKEN`` env var). When
    enabled it instruments the FastAPI app and routes stdlib logging to
    Logfire. Any failure is logged and swallowed so observability never
    blocks the server.
    """
    if settings.logfire_token is None:
        logger.info("No Logfire token configured; skipping Logfire setup")
        return
    try:
        import logfire

        logfire.configure(
            token=settings.logfire_token.get_secret_value(),
            service_name=settings.logfire_service_name,
            send_to_logfire=True,
            console=False,
        )
        logfire.instrument_fastapi(app, capture_headers=True)
        logging.getLogger().addHandler(logfire.LogfireLoggingHandler())
        logger.info(
            "Logfire configured (service=%s)",
            settings.logfire_service_name,
        )
    except Exception:
        logger.exception("Failed to configure Logfire; continuing without it")


async def lifespan(app: FastAPI):
    """Manage app lifecycle."""

    configure_logging()
    configure_logfire(app)
    logger.info("Starting soliplex-agents server")
    if settings.api_prefix:
        logger.info(f"API prefix: {settings.api_prefix}")
    if settings.root_path:
        logger.info(f"Root path: {settings.root_path}")

    if settings.haiku_load_enabled:
        start_worker()

    if settings.scheduler_enabled:
        # Run one reconcile immediately so schedules register and
        # unscheduled manifests run at startup; the reconciler cron picks
        # up changes on every subsequent tick.
        await reconcile_manifest_schedules()

    yield
    if settings.haiku_load_enabled:
        await stop_worker()
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

    @_crons.cron(settings.scheduler_reconcile_cron, name="manifest_reconciler")
    async def _manifest_reconciler_job():
        await reconcile_manifest_schedules()


# Include the parent router in the app
app.include_router(api_router)
