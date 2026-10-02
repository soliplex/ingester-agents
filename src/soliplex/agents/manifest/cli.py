"""CLI commands for manifest execution."""

import asyncio
import json
import logging

import typer

from soliplex.agents.config import settings

from . import haiku_maint
from . import runner

logger = logging.getLogger(__name__)

cli = typer.Typer(no_args_is_help=True)


@cli.command("run")
def run(
    path: str = typer.Argument(help="Path to a manifest YAML file or directory of manifests"),
    do_json: bool = typer.Option(False, "--json", help="Output results as JSON"),
    load: bool = typer.Option(
        None,
        "--load/--no-load",
        help="Run a haiku-rag load after each manifest (default: HAIKU_LOAD_ENABLED)",
    ),
):
    """Run one or more manifests from a YAML file or directory."""
    if load is None:
        load = settings.haiku_load_enabled
    try:
        results = asyncio.run(runner.run_manifests(path, load=load))
    except FileNotFoundError as e:
        print(f"Error: {e}")
        raise SystemExit(1) from None
    except ValueError as e:
        print(f"Validation error: {e}")
        raise SystemExit(1) from None

    if do_json:
        print(json.dumps(results, indent=2, default=str))
    else:
        for manifest_result in results:
            print(f"\nManifest: {manifest_result['manifest_name']} ({manifest_result['manifest_id']})")
            skipped = manifest_result.get("skipped")
            if skipped:
                reason = f": {skipped['message']}" if skipped.get("message") else ""
                print(f"  SKIPPED by {skipped['method']}{reason}")
                continue
            for comp in manifest_result.get("results", []):
                name = comp["component"]
                if "error" in comp:
                    print(f"  {name}: ERROR - {comp['error']}")
                else:
                    result = comp.get("result", {})
                    ingested = len(result.get("ingested", []))
                    errors = len(result.get("errors", []))
                    print(f"  {name}: {ingested} ingested, {errors} errors")
            # Stale removal is reconciled once per manifest (over all
            # components); report the count when delete_stale ran.
            deleted = manifest_result.get("delete_stale_result")
            if deleted is not None:
                print(f"  deleted (stale): {len(deleted)}")
            pre = manifest_result.get("pre_process")
            if pre and pre["checked"]:
                print(
                    f"  pre-process: {pre['checked']} checked, {len(pre['skipped'])} skipped, "
                    f"{len(pre['modified'])} modified, {len(pre['errors'])} errors"
                )
                for item in pre["skipped"]:
                    reason = f": {item['message']}" if item.get("message") else ""
                    print(f"    skipped {item['uri']}{reason}")


_PATH_HELP = "Manifest YAML file, directory of manifests, or 'all' for every manifest in MANIFEST_DIR"


def _report_dry_run(results: list[dict]) -> None:
    """Print the command line for each target, one per line, in run order.

    Only real command lines are printed bare so the output can be pasted
    into a shell; skips and resolution failures are '#'-prefixed comments.
    """
    for result in results:
        if "error" in result:
            print(f"# {result['source']}: ERROR - {result['error']}")
        elif "skipped" in result:
            print(f"# {result['source']}: skipped (duplicate db)")
        else:
            print(result["command"])


def _report_results(results: list[dict]) -> None:
    """Print a one-line outcome per target."""
    for result in results:
        source = result["source"]
        verb = result["verb"]
        if "error" in result:
            print(f"{source}: {verb} ERROR - {result['error']}")
        elif "skipped" in result:
            print(f"{source}: skipped (duplicate db)")
        elif result["timed_out"]:
            print(f"{source}: {verb} TIMED OUT after {result['timeout']}s")
        elif result["returncode"] == 0:
            print(f"{source}: {verb} ok -> {result['db']}{_summary_text(result)}")
        else:
            print(f"{source}: {verb} FAILED (rc={result['returncode']}) -> {result['db']}{_summary_text(result)}")
            for error in (result.get("summary") or {}).get("errors", []):
                print(f"  {error['uri']}: {error['error']}")


def _summary_text(result: dict) -> str:
    """A back-fill's counts, as a suffix for its outcome line."""
    summary = result.get("summary")
    if not summary:
        return ""
    updated = "would update" if summary["check"] else "updated"
    if summary["attachments_updated"]:
        updated += f" ({summary['attachments_updated']} attachments)"
    skipped = summary["skipped_no_provider"] + summary["skipped_provider_opt_out"] + summary["skipped_not_selected"]
    return (
        f" (scanned {summary['scanned']}: {summary['updated']} {updated}, {summary['unchanged']} unchanged, "
        f"{summary['stale']} stale, {summary['orphaned']} orphaned, {skipped} skipped, "
        f"{len(summary['errors'])} errors)"
    )


def _any_failed(results: list[dict], dry_run: bool) -> bool:
    """Whether any target failed; under *dry_run* only resolution counts."""
    for result in results:
        if "error" in result:
            return True
        if not dry_run and "skipped" not in result and (result["timed_out"] or result["returncode"] != 0):
            return True
    return False


def _maintenance(
    verb: str, path: str, do_json: bool, timeout: float | None, dry_run: bool, options: dict | None = None
) -> None:
    """Shared implementation of the `migrate`, `vacuum` and `backfill-metadata` verbs."""
    try:
        results = asyncio.run(haiku_maint.run_maintenance(verb, path, timeout=timeout, dry_run=dry_run, options=options))
    except FileNotFoundError as e:
        print(f"Error: {e}")
        raise SystemExit(1) from None
    except ValueError as e:
        print(f"Validation error: {e}")
        raise SystemExit(1) from None

    if do_json:
        print(json.dumps(results, indent=2, default=str))
    elif dry_run:
        _report_dry_run(results)
    else:
        _report_results(results)

    if _any_failed(results, dry_run):
        raise SystemExit(1)


@cli.command("migrate")
def migrate(
    path: str = typer.Argument(runner.ALL_MANIFESTS, help=_PATH_HELP),
    do_json: bool = typer.Option(False, "--json", help="Output results as JSON"),
    timeout: float = typer.Option(
        None,
        "--timeout",
        help="Seconds before a migration is killed (default: HAIKU_MAINTENANCE_TIMEOUT)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the haiku-rag command lines that would run, without running them",
    ),
):
    """Run pending haiku-rag database migrations for each manifest's source."""
    _maintenance("migrate", path, do_json, timeout, dry_run)


@cli.command("vacuum")
def vacuum(
    path: str = typer.Argument(runner.ALL_MANIFESTS, help=_PATH_HELP),
    do_json: bool = typer.Option(False, "--json", help="Output results as JSON"),
    timeout: float = typer.Option(
        None,
        "--timeout",
        help="Seconds before a vacuum is killed (default: HAIKU_MAINTENANCE_TIMEOUT)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the haiku-rag command lines that would run, without running them",
    ),
):
    """Optimize and compact each manifest source's haiku-rag database."""
    _maintenance("vacuum", path, do_json, timeout, dry_run)


@cli.command(haiku_maint.BACKFILL_VERB)
def backfill_metadata(
    path: str = typer.Argument(runner.ALL_MANIFESTS, help=_PATH_HELP),
    missing: list[str] = typer.Option(
        [],
        "--missing",
        help="Only documents lacking this metadata key, e.g. page_count (repeatable)",
    ),
    content_types: list[str] = typer.Option(
        [],
        "--content-type",
        help="Only documents of this content type, e.g. application/pdf (repeatable)",
    ),
    doc_filter: str = typer.Option(None, "--filter", help="LanceDB WHERE clause scoping which documents run"),
    db_name: str = typer.Option(
        None,
        "--db-name",
        help="Database in the config's lancedb.databases; only needed when it places more than one",
    ),
    batch_size: int = typer.Option(None, "--batch-size", min=1, help="Pagination size (default: 500)"),
    check: bool = typer.Option(False, "--check", help="Report what would change; write nothing"),
    attachments: bool = typer.Option(
        True, "--attachments/--no-attachments", help="Fill PDF attachments too, from their parent document"
    ),
    do_json: bool = typer.Option(False, "--json", help="Output results as JSON"),
    timeout: float = typer.Option(
        None,
        "--timeout",
        help="Seconds before a back-fill is killed (default: HAIKU_MAINTENANCE_TIMEOUT)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the command lines that would run, without running them",
    ),
):
    """Re-run each source's haiku-rag metadata_provider over documents already indexed.

    Fetches each selected document again through its haiku source, calls the
    provider, and merges the result into the document's metadata without
    re-ingesting it. --check reports what would change; --dry-run only prints
    the command. Do not run it while a load for the same source is running.
    """
    _maintenance(
        haiku_maint.BACKFILL_VERB,
        path,
        do_json,
        timeout,
        dry_run,
        {
            "missing": missing,
            "content_types": content_types,
            "doc_filter": doc_filter,
            "db_name": db_name,
            "batch_size": batch_size,
            "check": check,
            "attachments": attachments,
        },
    )


def _resolve(path: str) -> list:
    try:
        return runner.resolve_manifests(path)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        raise SystemExit(1) from None
    except ValueError as e:
        print(f"Validation error: {e}")
        raise SystemExit(1) from None


@cli.command("pre-process-report")
def pre_process_report(
    path: str = typer.Argument(runner.ALL_MANIFESTS, help=_PATH_HELP),
    status: str = typer.Option(None, "--status", help="Only this outcome: continue, modified, skip or error"),
    message: str = typer.Option(None, "--message", help="Only outcomes whose message contains this text"),
    changed_since: str = typer.Option(None, "--changed-since", help="Only documents whose hash changed since (ISO 8601)"),
    do_json: bool = typer.Option(False, "--json", help="Output results as JSON"),
) -> None:
    """List the latest pre-process outcome per document."""
    report = {
        m.id: runner.pre_process_report(m, status=status, message=message, changed_since=changed_since)
        for m in _resolve(path)
    }
    if do_json:
        print(json.dumps(report, indent=2))
        return
    for manifest_id, rows in report.items():
        print(f"\nManifest: {manifest_id} ({len(rows)} document(s))")
        for row in rows:
            by = f" by {row['method']}" if row["method"] else ""
            reason = f": {row['message']}" if row["message"] else ""
            print(
                f"  {row['status']:<8} {row['uri']}{by}{reason} "
                f"[sha {row['input_sha256'][:12]}, changed {row['hash_changed_at']}]"
            )


@cli.command("pre-run-report")
def pre_run_report(
    path: str = typer.Argument(runner.ALL_MANIFESTS, help=_PATH_HELP),
    status: str = typer.Option(None, "--status", help="Only this outcome: continue, skip or error"),
    since: str = typer.Option(None, "--since", help="Only runs started since (ISO 8601)"),
    do_json: bool = typer.Option(False, "--json", help="Output results as JSON"),
) -> None:
    """List recorded pre-run outcomes, newest run first."""
    report = {m.id: runner.pre_run_report(m, status=status, since=since) for m in _resolve(path)}
    if do_json:
        print(json.dumps(report, indent=2))
        return
    for manifest_id, rows in report.items():
        print(f"\nManifest: {manifest_id}")
        for row in rows:
            reason = f": {row['message']}" if row["message"] else ""
            print(f"  {row['started_at']} {row['status']:<8} {row['method']}{reason} ({row['duration_s']:.2f}s)")


@cli.command("reprocess")
def reprocess(
    path: str = typer.Argument(runner.ALL_MANIFESTS, help=_PATH_HELP),
    status: str = typer.Option(
        "skip", "--status", help="Documents with this outcome (continue, modified, skip, error), or 'all'"
    ),
    method: str = typer.Option(None, "--method", help="Only documents this pre-process method ran on"),
    dry_run: bool = typer.Option(False, "--dry-run", help="List the documents without changing anything"),
    do_json: bool = typer.Option(False, "--json", help="Output results as JSON"),
) -> None:
    """Make the next run re-fetch documents and run their pre-process steps again.

    Use after changing a pre-process step: by default the documents it
    skipped are forgotten, so the next run fetches and checks them again.
    For an incremental SCM source this also costs one full listing.
    """
    selected = None if status == "all" else status
    results = [runner.reprocess(m, status=selected, method=method, dry_run=dry_run) for m in _resolve(path)]
    if do_json:
        print(json.dumps(results, indent=2))
        return
    verb = "would re-fetch" if dry_run else "will re-fetch"
    for res in results:
        print(f"{res['manifest_id']}: {verb} {len(res['uris'])} document(s)")
        for uri in res["uris"]:
            print(f"  {uri}")
