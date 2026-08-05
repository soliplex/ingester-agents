"""Built-in manifest post-process callbacks.

A post-process callback is referenced from a manifest's ``config.post_process``
list by dotted path and invoked as ``method(source, **kwargs)`` after the
``haiku-ingester`` load for that source completes (see
``docs/post-process-plan.md``). This module holds the callbacks that ship with
ingester-agents.
"""

import logging

from soliplex.agents.manifest import haiku_maint

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

    Delegates to :func:`soliplex.agents.manifest.haiku_maint.run_verb`, which
    is the same code path the ``si-agent manifest vacuum`` CLI verb uses.
    Running out-of-process -- exactly like the ``haiku-ingester`` load -- keeps
    LanceDB's async runtime out of the agent's event loop (avoiding an
    in-process deadlock) and makes the pass killable, so a stuck compaction
    cannot hang the run or leave the process unable to exit. The subprocess's
    stdout/stderr are streamed to the logger line by line. Retention is taken
    from the haiku config's ``storage.vacuum_retention_seconds``.

    Unlike the CLI verb, a failure is **raised**: the post-process chain stops
    on the first error (see :func:`post_process.run_post_process`).

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
    result = await haiku_maint.run_verb(
        source,
        "vacuum",
        haiku_cfg=str(config) if config else None,
        timeout=timeout,
    )
    if result["timed_out"]:
        raise RuntimeError(f"Vacuum for source '{source}' timed out after {timeout}s")
    if result["returncode"] != 0:
        raise RuntimeError(f"Vacuum for source '{source}' failed (rc={result['returncode']})")
    logger.info("Vacuum completed for source '%s'", source)
