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
            print(f"{source}: {verb} ok -> {result['db']}")
        else:
            print(f"{source}: {verb} FAILED (rc={result['returncode']}) -> {result['db']}")


def _any_failed(results: list[dict], dry_run: bool) -> bool:
    """Whether any target failed; under *dry_run* only resolution counts."""
    for result in results:
        if "error" in result:
            return True
        if not dry_run and "skipped" not in result and (result["timed_out"] or result["returncode"] != 0):
            return True
    return False


def _maintenance(verb: str, path: str, do_json: bool, timeout: float | None, dry_run: bool) -> None:
    """Shared implementation of the `migrate` and `vacuum` verbs."""
    try:
        results = asyncio.run(haiku_maint.run_maintenance(verb, path, timeout=timeout, dry_run=dry_run))
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


@cli.command("migrate-store")
def migrate_store(
    path: str = typer.Argument(runner.ALL_MANIFESTS, help="Manifest file, directory, or 'all'"),
    do_json: bool = typer.Option(False, "--json", help="Output results as JSON"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be copied, writing nothing"),
) -> None:
    """Copy a source's documents to the target its manifest overrides to.

    Flipping a manifest's ``download_store`` already works without this: the
    state file is qualified by target, so everything re-fetches from upstream.
    This copies the objects sideways instead, which avoids re-downloading and
    re-hitting SCM / WebDAV rate limits.

    Copies rather than moves -- the documents and the old state file stay put,
    so rolling back is a config edit.
    """
    manifests = runner.resolve_manifests(path)
    results = [asyncio.run(runner.migrate_store(m, dry_run=dry_run)) for m in manifests]
    if do_json:
        print(json.dumps(results, indent=2))
    else:
        for res in results:
            if res["from"] == res["to"]:
                print(f"{res['source']}: already at {res['to']}")
                continue
            verb = "would copy" if res["dry_run"] else "copied"
            state = "" if res["dry_run"] else f", state {'copied' if res['state_copied'] else 'absent'}"
            print(f"{res['source']}: {verb} {res['keys']} object(s) {res['from']} -> {res['to']}{state}")
    if any(r["keys"] and not r["dry_run"] and r["copied"] != r["keys"] for r in results):
        raise typer.Exit(1)


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
