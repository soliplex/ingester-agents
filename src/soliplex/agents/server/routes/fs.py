"""Filesystem agent API routes."""

import json
import logging
import os

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Form
from fastapi import HTTPException

from soliplex.agents.fs import app as fs_app
from soliplex.agents.server.auth import get_current_user

logger = logging.getLogger(__name__)

fs_router = APIRouter(
    prefix="/api/v1/fs",
    tags=["filesystem"],
    dependencies=[Depends(get_current_user)],
)


@fs_router.post("/validate-config")
async def validate_config(
    config_file: str = Form(..., description="Path to the document directory"),
):
    """
    Validate an inventory configuration.

    The inventory is built by scanning the directory's contents.

    Checks file support and identifies invalid files.
    """
    if not os.path.exists(config_file):
        raise HTTPException(status_code=404, detail=f"Path not found: {config_file}")

    try:
        config, _ = await fs_app.resolve_config_path(config_file)
        validated = fs_app.check_config(config)
        invalid = [row for row in validated if "valid" in row and not row["valid"]]

        return {
            "status": "ok",
            "total_files": len(config),
            "invalid_count": len(invalid),
            "invalid_files": invalid,
        }
    except Exception as e:
        logger.exception("Error validating config %s", config_file)
        raise HTTPException(status_code=500, detail=str(e)) from e


@fs_router.post("/build-config")
async def build_config(
    path: str = Form(..., description="Path to document directory"),
):
    """
    Scan a directory and create an inventory configuration.

    Returns file metadata including paths, hashes, and MIME types.
    """
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Directory not found: {path}")

    if not os.path.isdir(path):
        raise HTTPException(status_code=400, detail=f"Path is not a directory: {path}")

    try:
        config = await fs_app.build_config(path)

        return {
            "status": "ok",
            "files_count": len(config),
            "inventory": config,
        }
    except Exception as e:
        logger.exception("Error building config for %s", path)
        raise HTTPException(status_code=500, detail=str(e)) from e


@fs_router.post("/check-status")
async def check_status(
    config_file: str = Form(..., description="Path to the document directory"),
    source: str = Form(..., description="Source name"),
    detail: bool = Form(False, description="Include detailed file list"),
):
    """
    Check which files need to be ingested.

    The inventory is built by scanning the directory's contents.

    Compares file hashes against the Ingester database to identify
    new or modified files.
    """
    if not os.path.exists(config_file):
        raise HTTPException(status_code=404, detail=f"Path not found: {config_file}")

    try:
        from soliplex.agents import local_state

        config, _ = await fs_app.resolve_config_path(config_file)
        to_process = local_state.compute_to_process(config, source)
    except Exception as e:
        logger.exception("Error checking status for %s", config_file)
        raise HTTPException(status_code=500, detail=str(e)) from e

    result = {
        "status": "ok",
        "total_files": len(config),
        "files_to_process": len(to_process),
    }

    if detail:
        result["files"] = to_process

    return result


@fs_router.post("/run-inventory")
async def run_inventory(
    config_file: str = Form(..., description="Path to the document directory"),
    source: str = Form(..., description="Source name"),
    start: int = Form(0, description="Start index"),
    end: int | None = Form(None, description="End index"),
    metadata: str | None = Form(None, description="JSON string of extra metadata to attach to all documents"),
):
    """
    Run document ingestion from a directory.

    The inventory is built by scanning the directory's contents; path
    resolution happens inside load_inventory via resolve_config_path.
    """
    if not os.path.exists(config_file):
        raise HTTPException(status_code=404, detail=f"Path not found: {config_file}")

    try:
        extra_metadata = json.loads(metadata) if metadata else None

        # Path resolution now handled by load_inventory internally
        result = await fs_app.load_inventory(
            config_file,
            source,
            start,
            end,
            extra_metadata=extra_metadata,
        )

        return {
            "status": "ok",
            "inventory_count": len(result.get("inventory", [])),
            "to_process_count": len(result.get("to_process", [])),
            "ingested_count": len(result.get("ingested", [])),
            "error_count": len(result.get("errors", [])),
            "errors": result.get("errors", []),
        }
    except Exception as e:
        logger.exception("Error running inventory for %s", config_file)
        raise HTTPException(status_code=500, detail=str(e)) from e
