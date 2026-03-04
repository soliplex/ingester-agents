"""WebDAV agent API routes."""

import json
import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import UploadFile
from pydantic import SecretStr

from soliplex.agents.server.auth import get_current_user
from soliplex.agents.webdav import app as webdav_app

logger = logging.getLogger(__name__)

webdav_router = APIRouter(
    prefix="/api/v1/webdav",
    tags=["webdav"],
    dependencies=[Depends(get_current_user)],
)


@webdav_router.post("/validate-config")
async def validate_config(
    config_path: str = Form(
        ...,
        description="Path to inventory file or WebDAV directory (e.g., /documents)",
    ),
    webdav_url: str = Form(None, description="WebDAV server URL (optional, uses env var if not provided)"),
    webdav_username: str = Form(None, description="WebDAV username (optional, uses env var if not provided)"),
    webdav_password: SecretStr = Form(None, description="WebDAV password (optional, uses env var if not provided)"),
):
    """
    Validate an inventory configuration.

    Scans the specified WebDAV directory recursively and validates discovered files.
    """
    try:
        pwd = webdav_password.get_secret_value() if webdav_password else None
        config = await webdav_app.build_config(config_path, webdav_url, webdav_username, pwd)
        validated = webdav_app.check_config(config)
        invalid = [row for row in validated if "valid" in row and not row["valid"]]

        return {
            "status": "ok",
            "total_files": len(config),
            "invalid_count": len(invalid),
            "invalid_files": invalid,
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error validating config: {str(e)}") from e


@webdav_router.post("/check-status")
async def check_status(
    config_path: str = Form(
        ...,
        description="Path to inventory file or WebDAV directory (e.g., /documents)",
    ),
    source: str = Form(..., description="Source name"),
    detail: bool = Form(False, description="Include detailed file list"),
    webdav_url: str = Form(None, description="WebDAV server URL (optional, uses env var if not provided)"),
    webdav_username: str = Form(None, description="WebDAV username (optional, uses env var if not provided)"),
    webdav_password: SecretStr = Form(None, description="WebDAV password (optional, uses env var if not provided)"),
):
    """
    Check which files need to be ingested.

    Scans the specified WebDAV directory recursively and compares file hashes
    against the Ingester database to identify new or modified files.
    """
    try:
        from soliplex.agents import client

        pwd = webdav_password.get_secret_value() if webdav_password else None
        config = await webdav_app.build_config(config_path, webdav_url, webdav_username, pwd)
        to_process = await client.check_status(config, source)

        result = {
            "status": "ok",
            "total_files": len(config),
            "files_to_process": len(to_process),
        }

        if detail:
            result["files"] = to_process

        return result  # noqa: TRY300

    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error checking status: {str(e)}") from e


@webdav_router.post("/run-inventory")
async def run_inventory(
    config_path: str = Form(
        ...,
        description="Path to inventory file or WebDAV directory (e.g., /documents)",
    ),
    source: str = Form(..., description="Source name"),
    start: int = Form(0, description="Start index"),
    end: int | None = Form(None, description="End index"),
    start_workflows: bool = Form(True, description="Start workflows after ingestion"),
    workflow_definition_id: str | None = Form(None, description="Workflow definition ID"),
    param_set_id: str | None = Form(None, description="Parameter set ID"),
    priority: int = Form(0, description="Workflow priority"),
    webdav_url: str = Form(None, description="WebDAV server URL (optional, uses env var if not provided)"),
    webdav_username: str = Form(None, description="WebDAV username (optional, uses env var if not provided)"),
    webdav_password: SecretStr = Form(None, description="WebDAV password (optional, uses env var if not provided)"),
    metadata: str | None = Form(None, description="JSON string of extra metadata to attach to all documents"),
):
    """
    Run document ingestion from an inventory.

    Scans the specified WebDAV directory recursively and ingests discovered files.
    """
    try:
        pwd = webdav_password.get_secret_value() if webdav_password else None
        extra_metadata = json.loads(metadata) if metadata else None
        result = await webdav_app.load_inventory(
            config_path,
            source,
            start,
            end,
            workflow_definition_id=workflow_definition_id,
            param_set_id=param_set_id,
            start_workflows=start_workflows,
            priority=priority,
            webdav_url=webdav_url,
            webdav_username=webdav_username,
            webdav_password=pwd,
            extra_metadata=extra_metadata,
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
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error running inventory: {str(e)}") from e


@webdav_router.post("/run-from-file")
async def run_from_file(
    file: UploadFile = File(..., description="Text file containing WebDAV URLs (one per line)"),
    source: str = Form(..., description="Source name"),
    start: int = Form(0, description="Start index"),
    end: int | None = Form(None, description="End index"),
    start_workflows: bool = Form(True, description="Start workflows after ingestion"),
    workflow_definition_id: str | None = Form(None, description="Workflow definition ID"),
    param_set_id: str | None = Form(None, description="Parameter set ID"),
    priority: int = Form(0, description="Workflow priority"),
    webdav_url: str = Form(None, description="WebDAV server URL (optional, uses env var if not provided)"),
    webdav_username: str = Form(None, description="WebDAV username (optional, uses env var if not provided)"),
    webdav_password: SecretStr = Form(None, description="WebDAV password (optional, uses env var if not provided)"),
    skip_hash_check: bool = Form(False, description="Skip hash check and ingest all URLs"),
    metadata: str | None = Form(None, description="JSON string of extra metadata to attach to all documents"),
):
    """
    Run document ingestion from an uploaded URL list file.

    Accepts a file upload containing WebDAV URLs (one per line) and ingests those specific files.
    """
    try:
        pwd = webdav_password.get_secret_value() if webdav_password else None
        extra_metadata = json.loads(metadata) if metadata else None

        content = await file.read()
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            result = await webdav_app.load_inventory_from_urls(
                tmp_path,
                source,
                start,
                end,
                workflow_definition_id=workflow_definition_id,
                param_set_id=param_set_id,
                start_workflows=start_workflows,
                priority=priority,
                webdav_url=webdav_url,
                webdav_username=webdav_username,
                webdav_password=pwd,
                skip_hash_check=skip_hash_check,
                extra_metadata=extra_metadata,
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

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
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error running from file: {str(e)}") from e
