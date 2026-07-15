"""CLI commands for manifest execution."""

import asyncio
import json
import logging

import typer

from soliplex.agents.config import settings

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
