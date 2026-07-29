"""Built-in manifest post-process callbacks.

A post-process callback is referenced from a manifest's ``config.post_process``
list by dotted path and invoked as ``method(source, **kwargs)`` after the
``haiku-ingester`` load for that source completes (see
``docs/post-process-plan.md``). This module holds the callbacks that ship with
ingester-agents.
"""

import asyncio
import logging
import os

from soliplex.agents.config import settings
from soliplex.agents.local_store import sanitize_source
from soliplex.agents.manifest.haiku_loader import _pump_stream
from soliplex.agents.manifest.haiku_loader import resolve_db_path

logger = logging.getLogger(__name__)

# Default upper bound (seconds) on a vacuum subprocess before it is killed.
DEFAULT_VACUUM_TIMEOUT = 1800


async def vacuum(
    source: str,
    *,
    config: str | None = None,
    timeout: float = DEFAULT_VACUUM_TIMEOUT,
) -> None:
    """Vacuum the per-source LanceDB by running ``haiku-rag vacuum`` as a
    subprocess.

    Running out-of-process -- exactly like the ``haiku-ingester`` load -- keeps
    LanceDB's async runtime out of the agent's event loop (avoiding an
    in-process deadlock) and makes the pass killable, so a stuck compaction
    cannot hang the run or leave the process unable to exit. The subprocess's
    stdout/stderr are streamed to the logger line by line. Retention is taken
    from the haiku config's ``storage.vacuum_retention_seconds``.

    Args:
        source: The manifest source; slugified to locate the database.
        config: Optional haiku.rag config path (auto-injected by the
            post-process runner). Passed as ``haiku-rag --config``; when omitted
            the subprocess falls back to haiku's own config discovery. It must
            match the DB embedder.
        timeout: Seconds before the vacuum subprocess is killed.

    Raises:
        RuntimeError: if the vacuum times out or exits non-zero.
    """
    db_path = resolve_db_path(source)
    argv = ["haiku-rag"]
    if config:
        argv += ["--config", str(config)]
    argv += ["vacuum", "--db", db_path]

    # Mirror the load subprocess env so a config that interpolates ${SOURCE} /
    # ${DOWNLOAD_DIR} resolves, and force unbuffered output for live streaming.
    env = os.environ.copy()
    env["SOURCE"] = sanitize_source(source)
    env["DOWNLOAD_DIR"] = settings.download_dir
    env["PYTHONUNBUFFERED"] = "1"

    logger.info("Vacuuming LanceDB for source '%s' -> %s", source, db_path)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(timeout):
            await asyncio.gather(
                _pump_stream(proc.stdout, logger.info, source),
                _pump_stream(proc.stderr, logger.info, source),
            )
            await proc.wait()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"Vacuum for source '{source}' timed out after {timeout}s") from None

    if proc.returncode != 0:
        raise RuntimeError(f"Vacuum for source '{source}' failed (rc={proc.returncode})")
    logger.info("Vacuum completed for source '%s'", source)
