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

``backfill-metadata`` is the one verb that is not haiku-rag's own: it runs
:mod:`soliplex.agents.haiku_backfill`, which re-runs each source's
``metadata_provider`` over documents already indexed. It goes through the same
planning, deduplication, environment and subprocess handling, only its command
line differs (:func:`build_backfill_argv`), and its result carries the
``summary`` the subprocess prints.

Running out-of-process keeps LanceDB's async runtime out of the agent's event
loop (avoiding an in-process deadlock) and makes a stuck compaction killable.
"""

import json
import logging
import shlex
import sys
from collections.abc import Iterable
from collections.abc import Mapping
from typing import Any

from soliplex.agents import alerts
from soliplex.agents import haiku_backfill
from soliplex.agents.config import Manifest
from soliplex.agents.config import settings
from soliplex.agents.manifest.context import LoadContext
from soliplex.agents.manifest.haiku_loader import resolve_db_path
from soliplex.agents.manifest.haiku_loader import resolve_haiku_cfg
from soliplex.agents.manifest.haiku_loader import slugify_source
from soliplex.agents.manifest.haiku_process import run_haiku

logger = logging.getLogger(__name__)

# The maintenance verbs exposed as `si-agent manifest <verb>`.
BACKFILL_VERB = "backfill-metadata"
MAINTENANCE_VERBS = ("migrate", "vacuum", BACKFILL_VERB)
# The verbs whose failures raise an operator alert, and the stage each reports.
ALERT_STAGES = {"migrate": alerts.Stage.MIGRATE, "vacuum": alerts.Stage.VACUUM}


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


def build_backfill_argv(
    haiku_cfg: str | None,
    *,
    missing: Iterable[str] = (),
    content_types: Iterable[str] = (),
    doc_filter: str | None = None,
    db_name: str | None = None,
    batch_size: int | None = None,
    check: bool = False,
    attachments: bool = True,
) -> list[str]:
    """Build the :mod:`soliplex.agents.haiku_backfill` command argv.

    Runs with this interpreter, so the subprocess sees the same haiku-rag and
    the same registered metadata providers. Each value is one ``--opt=value``
    token, so nothing in it can become an extra argument.

    Args:
        haiku_cfg: Resolved haiku-rag config path, or ``None`` to let the
            subprocess fall back to haiku-rag's config discovery.
        missing: Only documents lacking one of these metadata keys.
        content_types: Only documents of one of these content types.
        doc_filter: A LanceDB ``WHERE`` clause, AND-ed with *missing*'s.
        db_name: The database to open, when the config places several.
        batch_size: Pagination size for the document listing.
        check: Report what would change; write nothing.
        attachments: Fill PDF attachments too (from their parent).
    """
    argv = [sys.executable, "-m", haiku_backfill.__name__]
    if haiku_cfg is not None:
        argv.append(f"--config={haiku_cfg}")
    if db_name is not None:
        argv.append(f"--db-name={db_name}")
    argv += [f"--missing={key}" for key in missing]
    argv += [f"--content-type={content_type}" for content_type in content_types]
    if doc_filter is not None:
        argv.append(f"--filter={doc_filter}")
    if batch_size is not None:
        argv.append(f"--batch-size={batch_size}")
    if check:
        argv.append("--check")
    if not attachments:
        argv.append("--no-attachments")
    return argv


def parse_backfill_summary(stdout: str) -> dict | None:
    """The counts a back-fill printed last, or ``None`` when it printed none."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(haiku_backfill.SUMMARY_PREFIX):
            try:
                return json.loads(line[len(haiku_backfill.SUMMARY_PREFIX) :])
            except json.JSONDecodeError:
                return None
    return None


def _maintenance_env(source: str, verb: str) -> dict[str, str]:
    """Build the subprocess environment for a maintenance verb.

    Same context as the load (:class:`LoadContext`), differing only in the
    OpenTelemetry service name.
    """
    env = LoadContext.for_source(source).env()
    env["OTEL_SERVICE_NAME"] = env.get("OTEL_SERVICE_NAME", "ingester-agent") + f".haiku-rag.{verb}.{source}"
    # Flush promptly, so a timed-out child's last output is not lost in its buffer.
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
    options: Mapping[str, Any] | None = None,
) -> dict:
    """Run one haiku-rag maintenance verb against one source's database.

    The subprocess runs in a span of its own and its output is buffered, not
    streamed -- see :mod:`.haiku_process`. Failures and timeouts are logged and
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
        options: Keyword arguments for :func:`build_backfill_argv`; only
            ``backfill-metadata`` takes any.

    Returns:
        Dict with ``source``, ``verb``, ``db``, ``argv``, ``command`` and the
        resolved ``timeout``, plus either ``dry_run`` (when *dry_run*) or
        ``returncode`` / ``timed_out`` / ``stdout`` / ``stderr``. On timeout
        ``returncode`` is ``None`` and the output is whatever arrived first.
        A ``backfill-metadata`` run also has ``summary``: the counts it
        printed, or ``None`` when it printed none.

    Raises:
        ValueError: If ``settings.lancedb_dir`` is unset.
    """
    if timeout is None:
        timeout = settings.haiku_maintenance_timeout
    db = resolve_db_path(source)
    if verb == BACKFILL_VERB:
        argv = build_backfill_argv(haiku_cfg, **(options or {}))
    elif options:
        raise ValueError(f"haiku {verb} takes no options")
    else:
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

    run = await run_haiku(
        argv,
        operation=verb,
        source=source,
        env=_maintenance_env(source, verb),
        cwd=settings.haiku_load_cwd,
        timeout=timeout,
        attributes={"haiku.db": db, "haiku.config": haiku_cfg or ""},
    )
    result |= {
        "returncode": run.returncode,
        "timed_out": run.timed_out,
        "stdout": run.stdout,
        "stderr": run.stderr,
    }
    if verb == BACKFILL_VERB:
        result["summary"] = parse_backfill_summary(run.stdout)
    return result


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
    options: Mapping[str, Any] | None = None,
) -> list[dict]:
    """Run *verb* against every database named by the manifests at *path*.

    Operations run **strictly sequentially** -- the same capacity constraint
    that applies to haiku-rag loads means only one ``haiku-rag`` process may
    run at a time. A failure for one manifest is isolated to that manifest:
    it is logged and recorded, and the remaining manifests still run. Once
    every target has run, ``migrate`` and ``vacuum`` raise an operator alert
    for each manifest that failed (see :func:`alert_failures`); a dry run
    raises none.

    Args:
        verb: Maintenance verb (``"migrate"`` or ``"vacuum"``).
        path: ``"all"`` (every manifest in ``settings.manifest_dir``), a
            single manifest file, or a directory of manifests.
        timeout: Per-operation timeout in seconds; defaults to
            ``settings.haiku_maintenance_timeout``.
        dry_run: Resolve every target and return the commands that would
            run, without spawning anything.
        options: Passed to :func:`run_verb` for every target.

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
            result = await run_verb(
                manifest.source,
                verb,
                haiku_cfg=entry["haiku_cfg"],
                timeout=timeout,
                dry_run=dry_run,
                options=options,
            )
        except Exception as e:
            # e.g. the haiku-rag executable is missing; keep going so the
            # remaining databases are still processed.
            logger.exception("haiku %s failed for manifest '%s'", verb, manifest.id)
            results.append(base | {"error": str(e)})
            continue
        results.append(base | result)
    if not dry_run:
        alert_failures(verb, manifests, results)
    return results


def maintenance_failure(verb: str, result: dict) -> str | None:
    """Why *verb* failed for one :func:`run_maintenance` result; ``None`` if it didn't."""
    from soliplex.agents.manifest import runner

    if "error" in result:
        return f"haiku {verb}: {result['error']}"
    if "skipped" in result:
        return None
    if result["timed_out"]:
        return f"haiku {verb}: timed out after {result['timeout']}s"
    if result["returncode"] != 0:
        return f"haiku {verb}: {runner.describe_returncode(result['returncode'])}"
    return None


def alert_failures(verb: str, manifests: list[Manifest], results: list[dict]) -> None:
    """Raise an operator alert for each manifest *verb* failed on.

    Only for the verbs in :data:`ALERT_STAGES`. *results* holds one entry per
    manifest, in the same order (see :func:`plan_targets`).
    """
    stage = ALERT_STAGES.get(verb)
    if stage is None:
        return
    for manifest, result in zip(manifests, results, strict=True):
        reason = maintenance_failure(verb, result)
        if reason is not None:
            alerts.manifest_failed(
                manifest_id=manifest.id,
                path=manifest.manifest_path,
                stage=stage,
                reasons=[reason],
                source=manifest.source,
            )
