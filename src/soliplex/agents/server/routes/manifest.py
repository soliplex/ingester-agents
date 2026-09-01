"""Manifest agent API routes.

Read-only: manifests are executed solely by the cron scheduler, which feeds
:mod:`soliplex.agents.server.manifest_queue`. There is deliberately no HTTP
run endpoint -- a second execution entry point would need a lock to
serialize against the scheduler, and that lock is what this design removes.
Anything that needs an on-demand run should enqueue onto that same queue
rather than executing inline.
"""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Form
from fastapi import HTTPException

from soliplex.agents.manifest import runner as manifest_runner
from soliplex.agents.server.auth import get_current_user

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
