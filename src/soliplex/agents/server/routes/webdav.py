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

    Scans the specified WebDAV directory recursively and validates discovered files.
    """
    try:
        pwd = webdav_password.get_secret_value() if webdav_password else None
        config, listing_errors = await webdav_app.build_config(config_path, webdav_url, webdav_username, pwd)
        webdav_app.raise_if_incomplete(config_path, listing_errors)
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
        from soliplex.agents import local_state

        pwd = webdav_password.get_secret_value() if webdav_password else None
        config, listing_errors = await webdav_app.build_config(config_path, webdav_url, webdav_username, pwd, source=source)
        webdav_app.raise_if_incomplete(config_path, listing_errors)
        to_process = local_state.compute_to_process(config, source)

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
