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
    config_file: str = Form(..., description="Path to inventory file"),
):
    """
    Validate an inventory JSON file.

    Checks file support and identifies invalid files.
    """
    if not os.path.exists(config_file):
        raise HTTPException(status_code=404, detail=f"Config file not found: {config_file}")

    config = await fs_app.read_config(config_file)
    validated = fs_app.check_config(config)
    invalid = [row for row in validated if "valid" in row and not row["valid"]]

    return {
        "status": "ok",
        "total_files": len(config),
        "invalid_count": len(invalid),
        "invalid_files": invalid,
    }


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

    config = await fs_app.build_config(path)

    # Optionally save to file
    cfg_file = os.path.join(path, "inventory.json")
    with open(cfg_file, "w") as f:
        json.dump(config, f, indent=2)

    return {
        "status": "ok",
        "files_count": len(config),
        "inventory_file": cfg_file,
        "inventory": config,
    }


@fs_router.post("/check-status")
async def check_status(
    config_file: str = Form(..., description="Path to inventory file"),
    source: str = Form(..., description="Source name"),
    detail: bool = Form(False, description="Include detailed file list"),
):
    """
    Check which files need to be ingested.

    Compares file hashes against the Ingester database to identify
    new or modified files.
    """
    if not os.path.exists(config_file):
        raise HTTPException(status_code=404, detail=f"Config file not found: {config_file}")

    from soliplex.agents import client

    config = await fs_app.read_config(config_file)
    to_process = await client.check_status(config, source)

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
    config_file: str = Form(..., description="Path to inventory file or directory"),
    source: str = Form(..., description="Source name"),
    start: int = Form(0, description="Start index"),
    end: int | None = Form(None, description="End index"),
    start_workflows: bool = Form(True, description="Start workflows after ingestion"),
    workflow_definition_id: str | None = Form(None, description="Workflow definition ID"),
    param_set_id: str | None = Form(None, description="Parameter set ID"),
    priority: int = Form(0, description="Workflow priority"),
):
    """
    Run document ingestion from an inventory file.

    If a directory is provided, an inventory file will be built automatically.
    """
    if not os.path.exists(config_file):
        raise HTTPException(status_code=404, detail=f"Path not found: {config_file}")

    # If directory provided, build config first
    if os.path.isdir(config_file):
        config = await fs_app.build_config(config_file)
        cfg_file = os.path.join(config_file, "inventory.json")
        with open(cfg_file, "w") as f:
            json.dump(config, f, indent=2)
        config_file = cfg_file

    result = await fs_app.load_inventory(
        config_file,
        source,
        start,
        end,
        workflow_definition_id=workflow_definition_id,
        param_set_id=param_set_id,
        start_workflows=start_workflows,
        priority=priority,
    )

    return {
        "status": "ok",
        "inventory_count": len(result.get("inventory", [])),
        "to_process_count": len(result.get("to_process", [])),
        "ingested_count": len(result.get("ingested", [])),
        "error_count": len(result.get("errors", [])),
        "batch_id": result.get("batch_id"),
        "errors": result.get("errors", []),
        "workflow_result": result.get("workflow_result"),
    }
