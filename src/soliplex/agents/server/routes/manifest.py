"""Manifest agent API routes."""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Form
from fastapi import HTTPException

from soliplex.agents.manifest import runner as manifest_runner
from soliplex.agents.server.auth import get_current_user
from soliplex.agents.server.locks import get_manifest_lock
from soliplex.agents.server.locks import is_manifest_running

logger = logging.getLogger(__name__)

manifest_router = APIRouter(
    prefix="/api/v1/manifest",
    tags=["manifest"],
    dependencies=[Depends(get_current_user)],
)


@manifest_router.post("/run")
async def run_manifests(
    path: str = Form(..., description="Path to a manifest YAML file or directory of manifests"),
):
    """
    Run one or more manifests from a YAML file or directory.

    Loads and validates manifests, then dispatches each component to its
    appropriate agent (fs, scm, webdav, web).
    """
    from pathlib import Path as FilePath

    p = FilePath(path)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {path}")

    try:
        if p.is_file():
            manifests = [manifest_runner.load_manifest(path)]
        else:
            manifests = manifest_runner.load_manifests_from_dir(path)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error running manifests: {str(e)}") from e

    busy = [m.id for m in manifests if is_manifest_running(m.id)]
    if busy:
        raise HTTPException(
            status_code=409,
            detail=f"Manifest(s) already running: {', '.join(busy)}",
        )

    try:
        results = []
        for manifest in manifests:
            lock = get_manifest_lock(manifest.id)
            async with lock:
                result = await manifest_runner.run_manifest(manifest)
                results.append(result)

        total_components = sum(len(r.get("results", [])) for r in results)
        total_errors = sum(1 for r in results for c in r.get("results", []) if "error" in c)

        return {
            "status": "ok",
            "manifest_count": len(results),
            "total_components": total_components,
            "total_errors": total_errors,
            "manifests": results,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error running manifests: {str(e)}") from e


@manifest_router.post("/run-single")
async def run_single_manifest(
    path: str = Form(..., description="Path to a single manifest YAML file"),
):
    """
    Run a single manifest from a YAML file.

    Loads the manifest, validates it, and dispatches each component.
    """
    try:
        manifest = manifest_runner.load_manifest(path)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error running manifest: {str(e)}") from e

    if is_manifest_running(manifest.id):
        raise HTTPException(
            status_code=409,
            detail=f"Manifest '{manifest.id}' is already running",
        )

    try:
        lock = get_manifest_lock(manifest.id)
        async with lock:
            result = await manifest_runner.run_manifest(manifest)

        component_errors = [c for c in result.get("results", []) if "error" in c]

        return {
            "status": "ok",
            "manifest_id": result["manifest_id"],
            "manifest_name": result["manifest_name"],
            "component_count": len(result.get("results", [])),
            "error_count": len(component_errors),
            "results": result.get("results", []),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error running manifest: {str(e)}") from e


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
