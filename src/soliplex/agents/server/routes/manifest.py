"""Manifest agent API routes.

Manifests run only on :mod:`soliplex.agents.server.manifest_queue`, which is
fed by the cron scheduler and by ``POST /run``. Nothing here executes a
manifest inline: a second execution path would need a lock to serialize
against the scheduler, and the single queue is what removes that lock. So
``POST /run`` enqueues and returns 202; the run happens on the worker,
coalesced with any scheduled run of the same manifest.
"""

import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Form
from fastapi import HTTPException

from soliplex.agents.config import settings
from soliplex.agents.manifest import runner as manifest_runner
from soliplex.agents.server import manifest_queue
from soliplex.agents.server.auth import get_current_user
from soliplex.agents.server.manifest_queue import EnqueueResult

logger = logging.getLogger(__name__)

manifest_router = APIRouter(
    prefix="/api/v1/manifest",
    tags=["manifest"],
    dependencies=[Depends(get_current_user)],
)


@manifest_router.post("/validate")
async def validate_manifest(
    path: str = Form(..., description="Path to a manifest YAML file or directory"),
):
    """
    Validate one or more manifest YAML files without executing them.

    Checks that manifests are valid YAML, conform to the schema, and
    have unique IDs (when validating a directory).
    """
    from pathlib import Path as FilePath

    p = FilePath(path)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {path}")

    try:
        if p.is_file():
            manifest = manifest_runner.load_manifest(path)
            return {
                "status": "ok",
                "manifest_count": 1,
                "manifests": [
                    {
                        "id": manifest.id,
                        "name": manifest.name,
                        "source": manifest.source,
                        "component_count": len(manifest.components),
                        "has_schedule": manifest.schedule is not None,
                    }
                ],
            }
        else:
            manifests = manifest_runner.load_manifests_from_dir(path)
            return {
                "status": "ok",
                "manifest_count": len(manifests),
                "manifests": [
                    {
                        "id": m.id,
                        "name": m.name,
                        "source": m.source,
                        "component_count": len(m.components),
                        "has_schedule": m.schedule is not None,
                    }
                    for m in manifests
                ],
            }
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error validating manifest: {str(e)}") from e


@manifest_router.post("/run", status_code=202)
async def run_manifest(
    manifest_id: str = Form(..., description="Id of a manifest in MANIFEST_DIR"),
):
    """
    Queue a manifest from ``MANIFEST_DIR`` to run.

    Takes a manifest id rather than a path, so only operator-curated
    manifests can run. Returns 202 once the run is queued (or coalesced with
    one already queued or running); the run itself happens later on the
    manifest run queue.
    """
    if not settings.manifest_dir:
        raise HTTPException(status_code=503, detail="MANIFEST_DIR is not configured")
    if not Path(settings.manifest_dir).is_dir():
        raise HTTPException(
            status_code=503,
            detail=f"MANIFEST_DIR is not a directory: {settings.manifest_dir}",
        )

    scan = manifest_runner.scan_manifests(settings.manifest_dir)
    if manifest_id in scan.duplicates:
        raise HTTPException(
            status_code=409,
            detail=f"Manifest id '{manifest_id}' is declared by more than one file",
        )
    path = next((p for m, p in scan.pairs if m.id == manifest_id), None)
    if path is None:
        detail = f"No manifest with id '{manifest_id}' in MANIFEST_DIR"
        if scan.invalid:
            detail += f" ({len(scan.invalid)} manifest file(s) failed to load)"
        raise HTTPException(status_code=404, detail=detail)

    result = await manifest_queue.enqueue_manifest(manifest_id, path)
    if result is EnqueueResult.NOT_STARTED:
        raise HTTPException(status_code=503, detail="Manifest run queue is not running")
    status = "queued" if result is EnqueueResult.QUEUED else "already_queued"
    return {"status": status, "manifest_id": manifest_id}


@manifest_router.get("/queue")
async def manifest_queue_status():
    """List the ids of manifests currently queued or running."""
    return {"pending": sorted(manifest_queue.pending_manifests())}
