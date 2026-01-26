"""WebDAV agent API routes."""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Form
from fastapi import HTTPException
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

    If a local file is provided, it will be treated as an inventory.json config file.
    If a WebDAV path is provided, a config will be built from the WebDAV directory contents.

    Checks file support and identifies invalid files.
    """
    try:
        config, _ = await webdav_app.resolve_config_path(config_path, webdav_url, webdav_username, webdav_password)
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


@webdav_router.post("/build-config")
async def build_config(
    webdav_path: str = Form(..., description="WebDAV directory path (e.g., /documents)"),
    webdav_url: str = Form(None, description="WebDAV server URL (optional, uses env var if not provided)"),
    webdav_username: str = Form(None, description="WebDAV username (optional, uses env var if not provided)"),
    webdav_password: SecretStr = Form(None, description="WebDAV password (optional, uses env var if not provided)"),
):
    """
    Scan a WebDAV directory and create an inventory configuration.

    Returns file metadata including paths, hashes, and MIME types.
    """
    try:
        config = await webdav_app.build_config(webdav_path, webdav_url, webdav_username, webdav_password)

        return {
            "status": "ok",
            "files_count": len(config),
            "inventory": config,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error building config: {str(e)}") from e


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
    endpoint_url: str = Form(None, description="Ingester API endpoint URL (optional, uses env var if not provided)"),
):
    """
    Check which files need to be ingested.

    If a local file is provided, it will be treated as an inventory.json config file.
    If a WebDAV path is provided, a config will be built from the WebDAV directory contents.

    Compares file hashes against the Ingester database to identify
    new or modified files.
    """
    try:
        from soliplex.agents import client
        from soliplex.agents.config import settings as app_settings

        # Temporarily override endpoint_url if provided
        original_endpoint = app_settings.endpoint_url
        if endpoint_url:
            app_settings.endpoint_url = endpoint_url

        try:
            config, _ = await webdav_app.resolve_config_path(config_path, webdav_url, webdav_username, webdav_password)
            to_process = await client.check_status(config, source)

            result = {
                "status": "ok",
                "total_files": len(config),
                "files_to_process": len(to_process),
            }

            if detail:
                result["files"] = to_process

            return result
        finally:
            app_settings.endpoint_url = original_endpoint
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
    endpoint_url: str = Form(None, description="Ingester API endpoint URL (optional, uses env var if not provided)"),
):
    """
    Run document ingestion from an inventory.

    If a local file is provided, it will be treated as an inventory.json config file.
    If a WebDAV path is provided, a config will be built from the WebDAV directory contents.
    Path resolution is handled internally by load_inventory via resolve_config_path.
    """
    try:
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
            webdav_password=webdav_password,
            endpoint_url=endpoint_url,
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
