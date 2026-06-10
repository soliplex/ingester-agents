"""Web (HTTP) agent API routes."""

import json
import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import UploadFile

from soliplex.agents.server.auth import get_current_user
from soliplex.agents.web import app as web_app

logger = logging.getLogger(__name__)

web_router = APIRouter(
    prefix="/api/v1/web",
    tags=["web"],
    dependencies=[Depends(get_current_user)],
)


@web_router.post("/run-inventory")
async def run_inventory(
    urls: str = Form(..., description="JSON array of URLs to fetch and write"),
    source: str = Form(..., description="Source name"),
    metadata: str | None = Form(None, description="JSON string of extra metadata to attach to all documents"),
):
    """
    Fetch and write web pages from a list of URLs.

    URLs are fetched via HTTP GET, hashed, checked for changes,
    and written to the download directory.
    """
    try:
        url_list = json.loads(urls)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=f"Invalid JSON in urls: {str(e)}") from e

    if not isinstance(url_list, list):
        raise HTTPException(status_code=422, detail="urls must be a JSON array of strings")

    try:
        extra_metadata = json.loads(metadata) if metadata else None

        result = await web_app.load_inventory(
            url_list,
            source,
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
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error running web inventory: {str(e)}") from e


@web_router.post("/run-from-file")
async def run_from_file(
    file: UploadFile = File(..., description="Text file containing URLs (one per line)"),
    source: str = Form(..., description="Source name"),
    metadata: str | None = Form(None, description="JSON string of extra metadata to attach to all documents"),
):
    """
    Fetch and write web pages from an uploaded URL list file.

    Accepts a file upload containing URLs (one per line), fetches each via HTTP GET,
    and writes them to the download directory.
    """
    try:
        content = await file.read()
        text = content.decode("utf-8")
        url_list = [line.strip() for line in text.splitlines() if line.strip()]

        extra_metadata = json.loads(metadata) if metadata else None

        result = await web_app.load_inventory(
            url_list,
            source,
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
        raise HTTPException(status_code=500, detail=f"Error running web inventory from file: {str(e)}") from e
