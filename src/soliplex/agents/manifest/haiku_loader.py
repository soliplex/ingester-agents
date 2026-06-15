"""Run haiku-rag batch loads after a manifest's ingestion completes.

Each manifest maps to one ``source`` (and thus one downloaded document
folder). After ingestion, ``haiku-ingester run-batch`` loads those
documents into a per-source LanceDB database. The command is configurable
via ``settings.haiku_load_command``; the haiku-rag config file resolves
from the manifest override or the installation default.

The haiku-rag config interpolates ``${VAR}`` references at its own
startup, so the load subprocess inherits the parent environment plus an
explicit ``SOURCE`` (the sanitized download-folder name) and
``DOWNLOAD_DIR`` so the config can locate the ingested documents.
"""

import asyncio
import logging
import os
import re
import shlex
from pathlib import Path

from soliplex.agents.config import Manifest
from soliplex.agents.config import settings
from soliplex.agents.local_store import sanitize_source

logger = logging.getLogger(__name__)

_WHITESPACE = re.compile(r"\s+")


def slugify_source(source: str) -> str:
    """Convert a source identifier into a hyphenated slug.

    Runs of whitespace become a single hyphen; leading and trailing
    hyphens are trimmed. Used for the per-source ``.lancedb`` filename so
    sources containing spaces map to a clean file name.

    Args:
        source: Source identifier (e.g. ``"composite source"``).

    Returns:
        A hyphenated slug (e.g. ``"composite-source"``).
    """
    slug = _WHITESPACE.sub("-", source.strip()).strip("-")
    return slug or "source"


def resolve_haiku_cfg(manifest: Manifest) -> str:
    """Resolve the haiku-rag config path for *manifest*.

    Uses the manifest's ``config.haiku_config`` override when set, else
    ``settings.haiku_default_config``. Absolute values are used as-is;
    relative values are joined under ``settings.haiku_path``.

    Args:
        manifest: The manifest about to be loaded.

    Returns:
        Absolute or installation-relative config path as a string.

    Raises:
        ValueError: If a relative value is given but ``haiku_path`` is unset.
    """
    value = settings.haiku_default_config
    if manifest.config and manifest.config.haiku_config:
        value = manifest.config.haiku_config
    path = Path(value)
    if path.is_absolute():
        return str(path)
    if not settings.haiku_path:
        raise ValueError(f"HAIKU_PATH (settings.haiku_path) must be set to resolve relative haiku config '{value}'")
    return str(Path(settings.haiku_path) / value)


def resolve_db_path(source: str) -> str:
    """Return the ``.lancedb`` path for *source* under ``lancedb_dir``.

    Args:
        source: Source identifier (slugified for the filename).

    Returns:
        Absolute database path as a string.

    Raises:
        ValueError: If ``settings.lancedb_dir`` is unset.
    """
    if not settings.lancedb_dir:
        raise ValueError("LANCEDB_DIR (settings.lancedb_dir) must be set to run haiku loads")
    return str(Path(settings.lancedb_dir) / f"{slugify_source(source)}.lancedb")


def build_load_argv(haiku_cfg: str, db: str, source: str) -> list[str]:
    """Build the load command argv from the configurable template.

    The template is split into tokens *before* substitution so that a
    value containing spaces cannot inject extra arguments.

    Args:
        haiku_cfg: Resolved haiku-rag config path.
        db: Resolved ``.lancedb`` database path.
        source: Source identifier (slugified for the ``{source}`` token).

    Returns:
        Argument vector suitable for ``create_subprocess_exec``.
    """
    substitutions = {
        "haiku_cfg": haiku_cfg,
        "db": db,
        "source": slugify_source(source),
        "lancedb_dir": settings.lancedb_dir or "",
        "haiku_path": settings.haiku_path or "",
    }
    return [token.format(**substitutions) for token in shlex.split(settings.haiku_load_command)]


async def run_load(manifest: Manifest) -> dict:
    """Run a single haiku-rag batch load for *manifest*.

    Spawns the configured load command with ``SOURCE`` set to the
    sanitized download-folder name and ``DOWNLOAD_DIR`` injected so the
    haiku-rag config can locate the ingested documents. Failures and
    timeouts are logged and reported in the result rather than raised.

    Args:
        manifest: The manifest whose source should be loaded.

    Returns:
        Dict with ``source``, ``db``, ``returncode``, ``timed_out`` and
        (unless timed out) captured ``stdout``/``stderr``.
    """
    source = manifest.source
    haiku_cfg = resolve_haiku_cfg(manifest)
    db = resolve_db_path(source)
    argv = build_load_argv(haiku_cfg, db, source)

    env = os.environ.copy()
    env["SOURCE"] = sanitize_source(source)
    env["DOWNLOAD_DIR"] = settings.download_dir

    logger.info("Starting haiku load for source '%s' -> %s", source, db)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=settings.haiku_load_cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=settings.haiku_load_timeout,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        logger.error(  # noqa: TRY400 — timeout traceback adds no signal
            "haiku load for source '%s' timed out after %ds",
            source,
            settings.haiku_load_timeout,
        )
        return {"source": source, "db": db, "returncode": None, "timed_out": True}

    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    if proc.returncode == 0:
        logger.info("haiku load for source '%s' completed", source)
    else:
        logger.error(
            "haiku load for source '%s' failed (rc=%s): %s",
            source,
            proc.returncode,
            err.strip(),
        )
    return {
        "source": source,
        "db": db,
        "returncode": proc.returncode,
        "stdout": out,
        "stderr": err,
        "timed_out": False,
    }
