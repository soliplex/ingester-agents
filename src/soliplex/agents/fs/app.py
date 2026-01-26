import hashlib
import logging
import pathlib
from pathlib import Path

import aiofiles
import aiofiles.os as aos

from soliplex.agents import client
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
        adj_path = path.relative_to(Path(source_dir))
        mime_type = detect_mime_type(str(adj_path))
        rec = {
            "path": str(adj_path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "metadata": {
                "size": path.stat().st_size,
                "content-type": mime_type,
            },
        }
        config.append(rec)
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
    workflow_definition_id: str | None = None,
    start_workflows: bool = False,
    param_set_id: str | None = None,
    priority: int = 0,
):
    """
    Load and process an inventory for ingestion.

    If path is a file, treats it as a config file.
    If path is a directory, builds config from directory contents.

    Args:
        path: Path to either a config file or directory to process
        source: Source identifier for the batch
        start: Starting index for processing (default: 0)
        end: Ending index for processing (default: None, processes all)
        skip_invalid: Skip files that fail validation (default: False)
        workflow_definition_id: Optional workflow to start after ingestion
        start_workflows: Whether to start workflows (default: True)
        param_set_id: Parameter set for workflows
        priority: Workflow priority (default: 0)

    Returns:
        Dictionary with inventory, to_process, batch_id, ingested, errors,
        and workflow_result
    """
    config, data_path = await resolve_config_path(path)
    if skip_invalid:
        filtered = check_config(config)
        config = [x for x in filtered if x["valid"]]

    logger.info(f"found {len(config)} files in {path}")
    to_process = await client.check_status(config, source)
    logger.info(f"found {len(to_process)}  out of {len(config)} to process in {data_path}")
    if end is None:
        end = len(config)
        to_process = to_process[start:end]
    ret = {"inventory": config, "to_process": to_process}
    if len(to_process) == 0:
        logger.info("nothing to process. exiting")
        return ret

    found_batch_id = await client.find_batch_for_source(source)
    if found_batch_id:
        logger.info(f"found batch {found_batch_id} for {source}")
        batch_id = found_batch_id
    else:
        logger.info(f"no batch found for {source}. creating")
        batch_id = await client.create_batch(
            source,
            source,
        )
    ret["batch_id"] = batch_id
    ingested = []
    errors = []
    for row in to_process:
        meta = row["metadata"].copy()
        for k in [
            "path",
            "sha256",
            "size",
            "source",
            "batch_id",
            "source_uri",
        ]:
            if k in meta:
                del meta[k]
        logger.info(f"starting ingest for {row['path']}")
        mime_type = None
        if "metadata" in row and "content-type" in row["metadata"]:
            mime_type = row["metadata"]["content-type"]
        res = await do_ingest(
            data_path,
            row["path"],
            meta,
            source,
            batch_id,
            mime_type,
        )
        if "error" in res:
            logger.error(f"Error ingesting {row['path']}: {res['error']}")
            res["uri"] = row["path"]
            res["source"] = source
            res["batch_id"] = batch_id
            errors.append(res)
        else:
            ingested.append(res)
    wf_res = None
    if len(errors) == 0 and start_workflows:
        wf_res = await client.do_start_workflows(
            batch_id,
            workflow_definition_id,
            param_set_id,
            priority,
        )
    ret["ingested"] = ingested
    ret["errors"] = errors
    ret["workflow_result"] = wf_res
    return ret


async def do_ingest(
    data_path: Path,
    uri: str,
    meta: dict[str, str],
    source: str,
    batch_id: int,
    mime_type: str,
):
    logger.info(f"path={data_path / uri}")
    load_path = uri
    if not uri.startswith("/"):
        load_path = data_path / uri
    async with aiofiles.open(load_path, "rb") as f:
        doc_body = await f.read()

    return await client.do_ingest(
        doc_body,
        uri,
        meta,
        source,
        batch_id,
        mime_type,
    )


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
    to_process = await client.check_status(config, source)
    print(f"Files to process: {len(to_process)}")
    print(f"Total files: {len(config)}")
    if detail and len(to_process) > 0:
        for row in to_process:
            print(row)
