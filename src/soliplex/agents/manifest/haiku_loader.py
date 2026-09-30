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

import logging
import re
import shlex
from pathlib import Path

from soliplex.agents.config import Manifest
from soliplex.agents.config import settings
from soliplex.agents.manifest.context import LoadContext
from soliplex.agents.manifest.haiku_process import run_haiku
from soliplex.agents.sidecar import kinds as sidecar_kinds

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
        raise ValueError("LANCEDB_DIR (settings.lancedb_dir) must be set to resolve a per-source database path")
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


async def _run_post_process(manifest: Manifest, ingester_exit_code: int | None) -> list[dict]:
    """Run the manifest's post-process callbacks after a load.

    Fires regardless of the load outcome (success, failure, or timeout);
    ``ingester_exit_code`` is the load's exit code (``None`` on timeout) and is
    forwarded to the callbacks. The local import avoids a circular import
    (``post_process`` imports ``resolve_haiku_cfg`` from this module).
    """
    from soliplex.agents.manifest import post_process

    return await post_process.run_post_process(manifest, ingester_exit_code=ingester_exit_code)


async def _log_if_no_documents(manifest: Manifest, context: LoadContext) -> None:
    """Log an error when the load is about to run over an empty download folder.

    Checks the same location handed to the subprocess as ``DOWNLOAD_DIR`` /
    ``DOWNLOAD_URI``, counting documents only -- a folder holding nothing but
    sidecars still has nothing to index. The load itself still runs: this
    only makes an empty source visible, it does not change what happens next.
    A listing failure is logged and otherwise ignored, so the check can never
    be what stops a load.
    """
    try:
        keys = await context.store.list()
    except Exception:
        logger.exception(
            "Could not list documents at %s for manifest '%s' before haiku load",
            context.download_uri,
            manifest.id,
        )
        return
    sidecar_suffixes = tuple(kind.suffix for kind in sidecar_kinds().values())
    if not any(not key.endswith(sidecar_suffixes) for key in keys):
        logger.error(
            "Manifest '%s' finished with no documents in %s; haiku load for source '%s' has nothing to index",
            manifest.id,
            context.download_uri,
            manifest.source,
        )


async def run_load(manifest: Manifest, *, queue_wait_s: float | None = None) -> dict:
    """Run a single haiku-rag batch load for *manifest*.

    Spawns the configured load command with ``SOURCE`` set to the
    sanitized download-folder name and ``DOWNLOAD_DIR`` injected so the
    haiku-rag config can locate the ingested documents. The subprocess runs
    in a span of its own and its output is buffered, not streamed -- see
    :mod:`.haiku_process`. Failures and timeouts are logged and reported in the
    result rather than raised.

    Args:
        manifest: The manifest whose source should be loaded.
        queue_wait_s: Seconds the load waited in the haiku queue, recorded on
            the load's span (``None`` when it wasn't queued, as from the CLI).

    Returns:
        Dict with ``source``, ``db``, ``returncode`` (``None`` on timeout),
        ``timed_out``, the captured ``stdout``/``stderr`` and ``post_process``.
    """
    source = manifest.source
    haiku_cfg = resolve_haiku_cfg(manifest)
    db = resolve_db_path(source)
    argv = build_load_argv(haiku_cfg, db, source)

    context = LoadContext.for_source(source)
    env = context.env()
    env["OTEL_SERVICE_NAME"] = env.get("OTEL_SERVICE_NAME", "ingester-agent") + f".haiku-ingester.{source}"
    # Flush promptly, so a timed-out child's last output is not lost in its buffer.
    env["PYTHONUNBUFFERED"] = "1"
    if settings.logfire_token is not None:
        env["LOGFIRE_TOKEN"] = settings.logfire_token.get_secret_value()

    await _log_if_no_documents(manifest, context)
    run = await run_haiku(
        argv,
        operation="load",
        source=source,
        env=env,
        cwd=settings.haiku_load_cwd,
        timeout=settings.haiku_load_timeout,
        attributes={
            "haiku.db": db,
            "haiku.config": haiku_cfg,
            "manifest.id": manifest.id,
            "haiku.queue_wait_s": queue_wait_s,
        },
    )
    return {
        "source": source,
        "db": db,
        "returncode": run.returncode,
        "stdout": run.stdout,
        "stderr": run.stderr,
        "timed_out": run.timed_out,
        "post_process": await _run_post_process(manifest, run.returncode),
    }
