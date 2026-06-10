import hashlib
import logging
import pathlib
from pathlib import Path

import aiofiles
import aiofiles.os as aos

from soliplex.agents import local_state
from soliplex.agents import local_store
from soliplex.agents.common.config import check_config
from soliplex.agents.common.config import detect_mime_type
from soliplex.agents.common.config import read_config
from soliplex.agents.config import settings

logger = logging.getLogger(__name__)


async def validate_config(path: str):
    """
    Validate a configuration and print out validation results.

    If path is a file, treats it as a config file.
    If path is a directory, builds config from directory contents.

    Args:
        path: Path to either a config file or directory to validate

    Returns:
        None
    """
    config, _ = await resolve_config_path(path)
    validate = check_config(config)
    invalid = [row for row in validate if "valid" in row and not row["valid"]]
    print(f"Validation for {path}")
    print(f"Total files: {len(config)}")
    if invalid:
        print(f"Found {len(invalid)} Invalid files:")
        for row in invalid:
            print(row["path"], row["reason"], row["metadata"]["content-type"])


async def resolve_config_path(path: str) -> tuple[list[dict], Path]:
    """
    Resolve a path to a configuration and data directory.

    If the path is a file, treat it as an inventory.json config file.
    If the path is a directory, build a config from the directory contents.

    Args:
        path: Path to either a config file or directory to scan

    Returns:
        Tuple of (config list, data_path) where data_path is the parent
        directory containing the files referenced in the config

    Raises:
        FileNotFoundError: If the path doesn't exist
        ValidationError: If the config file format is invalid
    """
    path_obj = Path(path)

    if not await aos.path.exists(path_obj):
        raise FileNotFoundError(f"Path does not exist: {path}")

    is_file = await aos.path.isfile(path_obj)

    if is_file:
        # Path is a config file - read it directly
        logger.info(f"Using {path} as config file")
        config = await read_config(path)
        data_path = path_obj.parent
    else:
        # Path is a directory - build config from contents
        logger.info(f"Building config from directory {path}")
        config = await build_config(path)
        data_path = path_obj

    return config, data_path


async def build_config(source_dir) -> list[dict]:
    paths = await recursive_listdir(Path(source_dir))
    allowed_extensions = settings.extensions
    config = []
    for path in paths:
        ext = path.suffix.lstrip(".")
        if ext not in allowed_extensions and not path.is_dir():
            logger.info(f"skipping {path}")
            continue
        try:
            adj_path = path.relative_to(Path(source_dir))
            mime_type = detect_mime_type(str(adj_path))
            rec = {
                "path": str(adj_path),
                "sha256": hashlib.sha256(path.read_bytes(), usedforsecurity=False).hexdigest(),
                "metadata": {
                    "size": path.stat().st_size,
                    "content-type": mime_type,
                },
            }
            config.append(rec)
        except OSError as e:
            logger.warning("Skipping %s: %s", path, e)
            continue
    return config


async def recursive_listdir(file_dir: pathlib.Path):
    file_paths = []
    ls = await aos.listdir(file_dir)
    for entry in ls:
        ed = file_dir / entry
        isdir = await aos.path.isdir(ed)
        if isdir:
            ext = await recursive_listdir(ed)
            file_paths.extend(ext)
        else:
            file_paths.append(ed)
    return file_paths


async def load_inventory(
    path: str,
    source: str,
    start: int = 0,
    end: int = None,
    skip_invalid: bool = False,
    extra_metadata: dict[str, str] | None = None,
    delete_stale: bool = False,
):
    """
    Load an inventory and write changed files to the download directory.

    If path is a file, treats it as a config file.
    If path is a directory, builds config from directory contents.

    Args:
        path: Path to either a config file or directory to process
        source: Source identifier (becomes the per-source download folder)
        start: Starting index for processing (default: 0)
        end: Ending index for processing (default: None, processes all)
        skip_invalid: Skip files that fail validation (default: False)
        extra_metadata: Extra metadata attached to every document
        delete_stale: Remove documents not in inventory (default: False)

    Returns:
        Dictionary with inventory, to_process, ingested, errors, and
        delete_stale_result
    """
    config, data_path = await resolve_config_path(path)
    if skip_invalid:
        filtered = check_config(config)
        config = [x for x in filtered if x["valid"]]

    logger.info(f"found {len(config)} files in {path}")
    to_process = local_state.compute_to_process(config, source)
    if end is None:
        end = len(config)
    to_process = to_process[start:end]
    logger.info(f"found {len(to_process)} out of {len(config)} to process in {data_path}")

    ingested = []
    errors = []
    ret = {"inventory": config, "to_process": to_process, "ingested": ingested, "errors": errors}
    for row in to_process:
        uri = row["path"]
        try:
            meta = dict(row.get("metadata") or {})
            for k in ("path", "sha256", "size", "source", "batch_id", "source_uri", "content-type"):
                meta.pop(k, None)
            if extra_metadata:
                meta.update(extra_metadata)
            logger.info(f"writing {uri}")
            mime_type = (row.get("metadata") or {}).get("content-type") or detect_mime_type(uri)
            await _write_local(data_path, uri, meta, source, mime_type, row.get("sha256"))
            ingested.append(uri)
        except Exception as e:
            logger.exception("Failed to write %s", uri)
            errors.append({"uri": uri, "error": str(e)})

    delete_stale_result = None
    if delete_stale and len(errors) == 0:
        delete_stale_result = local_state.prune_documents(source, {r["path"] for r in config})
    ret["delete_stale_result"] = delete_stale_result
    return ret


async def _write_local(
    data_path: Path,
    uri: str,
    meta: dict[str, str],
    source: str,
    mime_type: str,
    sha256: str | None,
):
    """Read a source file and write it (plus its sidecar) to the download dir."""
    load_path = uri if uri.startswith("/") else data_path / uri
    async with aiofiles.open(load_path, "rb") as f:
        doc_body = await f.read()
    local_store.write_document(source, uri, doc_body, mime_type, meta)
    local_state.upsert_file(source, uri, sha256, size=len(doc_body), mime_type=mime_type)


async def status_report(config_path: str, source: str, detail: bool = False):
    """
    Generate a status report for an inventory.

    If config_path is a file, treats it as a config file.
    If config_path is a directory, builds config from directory contents.

    Args:
        config_path: Path to either a config file or directory
        source: Source identifier to check against
        detail: Whether to print detailed file list (default: False)
    """
    print(f"checking status for {config_path} source={source} ")
    config, _ = await resolve_config_path(config_path)
    to_process = local_state.compute_to_process(config, source)
    print(f"Files to process: {len(to_process)}")
    print(f"Total files: {len(config)}")
    if detail and len(to_process) > 0:
        for row in to_process:
            print(row)
