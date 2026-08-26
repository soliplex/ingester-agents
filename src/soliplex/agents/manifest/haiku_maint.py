"""Run haiku-rag database maintenance verbs (``migrate`` / ``vacuum``).

Each manifest maps to one ``source`` and therefore to one per-source LanceDB
database. These verbs operate on that database rather than on the downloaded
documents, so nothing is ingested and **no post-process callbacks run** --
that is the one difference from :mod:`soliplex.agents.manifest.haiku_loader`,
whose conventions this module otherwise follows exactly: the command comes
from a configurable template (``settings.haiku_maintenance_command``), the
config file resolves from the manifest override or the installation default,
and the subprocess inherits the parent environment plus an explicit
``SOURCE`` / ``DOWNLOAD_DIR`` so the haiku-rag config's ``${VAR}``
interpolation resolves the same way it does for a load.

Running out-of-process keeps LanceDB's async runtime out of the agent's event
loop (avoiding an in-process deadlock) and makes a stuck compaction killable.
"""

import asyncio
import logging
import shlex
import signal

from soliplex.agents.config import Manifest
from soliplex.agents.config import settings
from soliplex.agents.manifest.context import LoadContext
from soliplex.agents.manifest.haiku_loader import _pump_stream
from soliplex.agents.manifest.haiku_loader import resolve_db_path
from soliplex.agents.manifest.haiku_loader import resolve_haiku_cfg
from soliplex.agents.manifest.haiku_loader import slugify_source

logger = logging.getLogger(__name__)

# The maintenance verbs exposed as `si-agent manifest <verb>`.
MAINTENANCE_VERBS = ("migrate", "vacuum")


def build_maintenance_argv(verb: str, haiku_cfg: str | None, db: str, source: str) -> list[str]:
    """Build the maintenance command argv from the configurable template.

    The template is split into tokens *before* substitution so that a value
    containing spaces cannot inject extra arguments.

    Args:
        verb: Maintenance verb (``"migrate"`` or ``"vacuum"``).
        haiku_cfg: Resolved haiku-rag config path, or ``None`` to drop the
            config argument entirely and let haiku-rag fall back to its own
            config discovery.
        db: Resolved ``.lancedb`` database path.
        source: Source identifier (slugified for the ``{source}`` token).

    Returns:
        Argument vector suitable for ``create_subprocess_exec``.
    """
    substitutions = {
        "verb": verb,
        "haiku_cfg": haiku_cfg or "",
        "db": db,
        "source": slugify_source(source),
        "lancedb_dir": settings.lancedb_dir or "",
        "haiku_path": settings.haiku_path or "",
    }
    argv = []
    for token in shlex.split(settings.haiku_maintenance_command):
        if haiku_cfg is None and "{haiku_cfg}" in token:
            continue
        argv.append(token.format(**substitutions))
    return argv


def _maintenance_env(source: str, verb: str) -> dict[str, str]:
    """Build the subprocess environment for a maintenance verb.

    Same context as the load (:class:`LoadContext`), differing only in the
    OpenTelemetry service name.
    """
    env = LoadContext.for_source(source).env()
    env["OTEL_SERVICE_NAME"] = env.get("OTEL_SERVICE_NAME", "ingester-agent") + f".haiku-rag.{verb}.{source}"
    # Force the (Python) child to flush stdout so we can stream it live.
    env["PYTHONUNBUFFERED"] = "1"
    if settings.logfire_token is not None:
        env["LOGFIRE_TOKEN"] = settings.logfire_token.get_secret_value()
    return env


async def run_verb(
    source: str,
    verb: str,
    *,
    haiku_cfg: str | None,
    timeout: float | None = None,
    dry_run: bool = False,
) -> dict:
    """Run one haiku-rag maintenance verb against one source's database.

    The subprocess's stdout and stderr are streamed to the logger line by
    line as the operation progresses. Failures and timeouts are logged and
    reported in the result rather than raised, so a caller iterating over
    manifests can keep going.

    Args:
        source: Source identifier; slugified to locate the database.
        verb: Maintenance verb (``"migrate"`` or ``"vacuum"``).
        haiku_cfg: Resolved haiku-rag config path, or ``None`` to let
            haiku-rag discover its own config.
        timeout: Seconds before the subprocess is killed; defaults to
            ``settings.haiku_maintenance_timeout``.
        dry_run: Resolve everything and return the command without spawning.

    Returns:
        Dict with ``source``, ``verb``, ``db``, ``argv``, ``command`` and the
        resolved ``timeout``, plus either ``dry_run`` (when *dry_run*) or
        ``returncode`` / ``timed_out`` / ``stdout`` / ``stderr``. On timeout
        ``returncode`` is ``None`` and no output is captured.

    Raises:
        ValueError: If ``settings.lancedb_dir`` is unset.
    """
    if timeout is None:
        timeout = settings.haiku_maintenance_timeout
    db = resolve_db_path(source)
    argv = build_maintenance_argv(verb, haiku_cfg, db, source)
    result = {
        "source": source,
        "verb": verb,
        "db": db,
        "argv": argv,
        "command": shlex.join(argv),
        "timeout": timeout,
    }
    if dry_run:
        return result | {"dry_run": True}

    logger.info("Starting haiku %s for source '%s' -> %s", verb, source, db)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=settings.haiku_load_cwd,
        env=_maintenance_env(source, verb),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(timeout):
            out, err = await asyncio.gather(
                _pump_stream(proc.stdout, logger.info, source),
                _pump_stream(proc.stderr, logger.info, source),
            )
            await proc.wait()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        logger.error(  # noqa: TRY400 — timeout traceback adds no signal
            "haiku %s for source '%s' timed out after %ss",
            verb,
            source,
            timeout,
        )
        return result | {"returncode": None, "timed_out": True}

    if proc.returncode == 0:
        logger.info("haiku %s for source '%s' completed", verb, source)
    elif proc.returncode < 0:
        try:
            signame = signal.Signals(-proc.returncode).name
        except ValueError:  # pragma: no cover - signal set is platform-specific
            signame = f"signal {-proc.returncode}"
        logger.error(
            "haiku %s for source '%s' was killed by %s (rc=%s); a SIGKILL "
            "usually means the container exceeded its memory limit",
            verb,
            source,
            signame,
            proc.returncode,
        )
    else:
        logger.error(
            "haiku %s for source '%s' failed (rc=%s)",
            verb,
            source,
            proc.returncode,
        )
    return result | {
        "returncode": proc.returncode,
        "timed_out": False,
        "stdout": out,
        "stderr": err,
    }


def plan_targets(verb: str, manifests: list[Manifest]) -> list[dict]:
    """Plan one entry per manifest, in order, deduplicating by (config, db).

    Several manifests can declare the same ``source`` -- and therefore the
    same database -- so the same verb would otherwise run against it more
    than once. The first manifest wins; duplicates and resolution failures
    become finished ``report`` entries so nothing disappears from the output
    and the reported order still matches the manifest order.

    Args:
        verb: Maintenance verb, recorded on each entry.
        manifests: Manifests to plan, in the order they should run.

    Returns:
        One dict per manifest: either ``{"manifest", "haiku_cfg"}`` for an
        operation to run, or ``{"report"}`` for a skip or failure.
    """
    entries: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for manifest in manifests:
        base = {"manifest_id": manifest.id, "source": manifest.source, "verb": verb}
        try:
            haiku_cfg = resolve_haiku_cfg(manifest)
            db = resolve_db_path(manifest.source)
        except ValueError as e:
            logger.error("Cannot %s manifest '%s': %s", verb, manifest.id, e)  # noqa: TRY400
            entries.append({"report": base | {"error": str(e)}})
            continue
        key = (haiku_cfg, db)
        if key in seen:
            logger.info(
                "Skipping %s for manifest '%s': database %s already handled",
                verb,
                manifest.id,
                db,
            )
            entries.append({"report": base | {"db": db, "skipped": "duplicate-db"}})
            continue
        seen.add(key)
        entries.append({"manifest": manifest, "haiku_cfg": haiku_cfg})
    return entries


async def run_maintenance(
    verb: str,
    path: str = "all",
    *,
    timeout: float | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Run *verb* against every database named by the manifests at *path*.

    Operations run **strictly sequentially** -- the same capacity constraint
    that applies to haiku-rag loads means only one ``haiku-rag`` process may
    run at a time. A failure for one manifest is isolated to that manifest:
    it is logged and recorded, and the remaining manifests still run.

    Args:
        verb: Maintenance verb (``"migrate"`` or ``"vacuum"``).
        path: ``"all"`` (every manifest in ``settings.manifest_dir``), a
            single manifest file, or a directory of manifests.
        timeout: Per-operation timeout in seconds; defaults to
            ``settings.haiku_maintenance_timeout``.
        dry_run: Resolve every target and return the commands that would
            run, without spawning anything.

    Returns:
        One result dict per manifest, in manifest order: a :func:`run_verb`
        result, or an entry carrying ``error`` (resolution failed) or
        ``skipped`` (a duplicate database).

    Raises:
        FileNotFoundError: If *path* does not exist, or ``"all"`` was given
            and ``settings.manifest_dir`` is unset or not a directory.
        ValueError: If duplicate manifest IDs are found (directory mode).
    """
    from soliplex.agents.manifest import runner

    manifests = runner.resolve_manifests(path)
    results: list[dict] = []
    for entry in plan_targets(verb, manifests):
        report = entry.get("report")
        if report is not None:
            results.append(report)
            continue
        manifest = entry["manifest"]
        base = {"manifest_id": manifest.id, "source": manifest.source, "verb": verb}
        try:
            # Resolved under the manifest's own download target, exactly as
            # `run_manifest` does. Without this a manifest that overrides
            # `download_store` gets a DOWNLOAD_URI from the installation
            # default instead -- a `file://` URI handed to a config whose
            # source stanza is `type: s3`.
            with runner.download_target(manifest.get_download_target()):
                result = await run_verb(
                    manifest.source,
                    verb,
                    haiku_cfg=entry["haiku_cfg"],
                    timeout=timeout,
                    dry_run=dry_run,
                )
        except Exception as e:
            # e.g. the haiku-rag executable is missing; keep going so the
            # remaining databases are still processed.
            logger.exception("haiku %s failed for manifest '%s'", verb, manifest.id)
            results.append(base | {"error": str(e)})
            continue
        results.append(base | result)
    return results
