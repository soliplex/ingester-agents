"""
FastAPI server for Soliplex Agents.

Ingestion runs only through manifests: the cron scheduler and
``POST /api/v1/manifest/run`` both feed the single manifest run queue
(:mod:`.manifest_queue`). The filesystem, SCM, and WebDAV routes are
read-only inspection helpers.
"""

import logging
from datetime import UTC
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from soliplex.agents import telemetry
from soliplex.agents.config import configure_logging
from soliplex.agents.config import settings
from soliplex.agents.manifest.schedule_registry import ScheduleRegistry

from . import haiku_queue
from . import manifest_queue
from .routes.fs import fs_router
from .routes.manifest import manifest_router
from .routes.scm import scm_router
from .routes.webdav import webdav_router

logger = logging.getLogger(__name__)

# Registry of manifest schedules, reconciled against the manifest directory
# on each tick so schedule edits and added/removed files hot-reload without
# a restart.
_schedule_registry = ScheduleRegistry()


class _ReconcileLog:
    """What the reconcile tick last reported, so each problem is logged once.

    The tick runs every minute. Without this a broken manifest logged the same
    warning sixty times an hour -- and still never said which manifest had
    stopped running. A problem is reported again only when it changes: a
    different error, or the file edited while still invalid.
    """

    def __init__(self) -> None:
        self.dir_problem: str | None = None
        self.duplicates: list[str] = []
        # path -> (error, mtime_ns) as last reported
        self.invalid: dict[str, tuple[str, int]] = {}


_reconcile_log = _ReconcileLog()


def _mtime_ns(path: str) -> int:
    try:
        return Path(path).stat().st_mtime_ns
    except OSError:
        return 0


def _report_invalid(invalid: dict[str, str]) -> None:
    """Log each invalid manifest file once per change, and each recovery.

    Runs before the registry reconciles, so a file that just broke can still
    be named by the manifest id it was registered under.
    """
    reported = _reconcile_log.invalid
    for path, error in invalid.items():
        fingerprint = (error, _mtime_ns(path))
        if reported.get(path) == fingerprint:
            continue
        reported[path] = fingerprint
        entry = _schedule_registry.entry_for_path(path)
        logger.error(
            "Manifest file %s (id %s) is invalid; it will not run until fixed: %s",
            path,
            entry.manifest_id if entry else "unknown",
            error,
        )
    for path in [p for p in reported if p not in invalid]:
        del reported[path]
        if Path(path).exists():
            logger.info("Manifest file %s is valid again", path)


async def reconcile_manifest_schedules() -> None:
    """Rescan the manifest directory and fire due/newly-added manifests.

    Runs on a fixed interval (see ``scheduler_reconcile_cron``) so that
    added, removed, and re-scheduled manifest files take effect without a
    restart. Scheduled manifests fire when due; manifests without a schedule
    run once when first seen.

    Due manifests are handed to :mod:`.manifest_queue` rather than executed
    here, so a manifest that comes due while another is running waits its
    turn instead of being dropped. This pass therefore never blocks on a
    manifest run and stays safe to drive from a cron tick.

    Problems with the directory -- it is missing, a file is invalid, two files
    share an id -- are logged once per change rather than on every tick (see
    :class:`_ReconcileLog`).
    """
    from soliplex.agents.manifest import runner as manifest_runner

    if not settings.manifest_dir:
        return

    manifest_path = Path(settings.manifest_dir)
    if not manifest_path.is_dir():
        if _reconcile_log.dir_problem != settings.manifest_dir:
            logger.warning(
                "manifest_dir is not a directory: %s",
                settings.manifest_dir,
            )
            _reconcile_log.dir_problem = settings.manifest_dir
        return
    _reconcile_log.dir_problem = None

    scan = manifest_runner.scan_manifests(settings.manifest_dir)
    _report_invalid(scan.invalid)
    if scan.duplicates:
        # e.g. a transient duplicate id mid-edit -- keep the last good state.
        if scan.duplicates != _reconcile_log.duplicates:
            logger.error(
                "Duplicate manifest IDs found: %s; skipping reconcile until resolved",
                scan.duplicates,
            )
            _reconcile_log.duplicates = scan.duplicates
        return
    if _reconcile_log.duplicates:
        logger.info("Duplicate manifest IDs resolved")
        _reconcile_log.duplicates = []

    result = _schedule_registry.reconcile(scan.pairs, datetime.now(UTC))

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
    for entry in result.removed:
        if entry.path in scan.invalid:
            # The file is still there: it stopped loading. Saying "removed"
            # would hide that the manifest silently stopped running.
            logger.error("Unregistered manifest '%s': %s is invalid", entry.manifest_id, entry.path)
        else:
            logger.info("Unregistered manifest '%s' (file removed)", entry.manifest_id)

    for entry in result.to_run:
        await manifest_queue.enqueue_manifest(entry.manifest_id, entry.path)


# Requests that get no span. The health check is polled every 30s by the
# container HEALTHCHECK (and by any orchestrator probe), so tracing it would
# bury real requests. Anchored on the end of the path because the route sits
# under `api_prefix` and, behind a proxy, `root_path`. These are regexes
# searched against the request URL; no commas, which OpenTelemetry splits on.
_UNTRACED_URLS = [r"/health/?(\?.*)?$"]


def configure_logfire(app: FastAPI) -> None:
    """Configure Pydantic Logfire for the server process and trace its requests.

    Only active when a token is available (read from
    ``/run/secrets/logfire_token`` or the ``LOGFIRE_TOKEN`` env var); the setup
    shared with the CLI lives in :func:`soliplex.agents.telemetry.configure`.
    Any failure is logged and swallowed so observability never blocks the
    server.

    Called at import, right after the app is created, **not** from the
    lifespan. ``instrument_fastapi`` only wraps ``build_middleware_stack``, and
    Starlette builds the stack on the app's first call -- the lifespan's
    startup -- so instrumenting from inside the lifespan produced no request
    spans at all.
    """
    if not telemetry.configure():
        logger.info("No Logfire token configured; skipping Logfire setup")
        return
    try:
        import logfire

        logfire.instrument_fastapi(app, capture_headers=True, excluded_urls=_UNTRACED_URLS)
    except Exception:
        logger.exception("Failed to instrument FastAPI for Logfire; continuing without request spans")


async def lifespan(app: FastAPI):
    """Manage app lifecycle."""

    configure_logging()
    # configure_logging() replaced the root handlers; put Logfire's back.
    telemetry.configure()
    logger.info("Starting soliplex-agents server")
    if settings.api_prefix:
        logger.info(f"API prefix: {settings.api_prefix}")
    if settings.root_path:
        logger.info(f"Root path: {settings.root_path}")

    if settings.haiku_load_enabled:
        haiku_queue.start_worker()

    # The run queue is started whether or not the scheduler is, so
    # `POST /api/v1/manifest/run` can queue manifests either way. It must be
    # draining before the first reconcile, or that pass would have nowhere
    # to enqueue its due manifests.
    manifest_queue.start_worker()

    if settings.scheduler_enabled:
        # Run one reconcile immediately so schedules register and
        # unscheduled manifests run at startup; the reconciler cron picks
        # up changes on every subsequent tick.
        await reconcile_manifest_schedules()

    yield
    # Stop the manifest worker first: it feeds the haiku queue, so draining
    # it in the other order could enqueue a load onto a stopped worker.
    await manifest_queue.stop_worker()
    if settings.haiku_load_enabled:
        await haiku_queue.stop_worker()
    logger.info("soliplex-agents server stopped")


app = FastAPI(
    title="Soliplex Agents API",
    description="REST API for Soliplex manifest-driven document ingestion",
    version="0.1.0",
    lifespan=lifespan,
    root_path=settings.root_path or "",
)
configure_logfire(app)

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
