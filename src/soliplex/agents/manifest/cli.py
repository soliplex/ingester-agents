"""CLI commands for manifest execution."""

import asyncio
import json
import logging

import typer

from soliplex.agents import client

from . import runner

logger = logging.getLogger(__name__)

cli = typer.Typer(no_args_is_help=True)


@cli.command("run")
def run(
    path: str = typer.Argument(help="Path to a manifest YAML file or directory of manifests"),
    do_json: bool = typer.Option(False, "--json", help="Output results as JSON"),
):
    """Run one or more manifests from a YAML file or directory."""
    try:
        results = asyncio.run(runner.run_manifests(path))
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


@cli.command("preflight")
def preflight(
    path: str = typer.Argument(help="Path to a manifest YAML file"),
):
    """Check that workflow and parameter set referenced in a manifest exist."""
    try:
        manifest = runner.load_manifest(path)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        raise SystemExit(1) from None
    except (ValueError, TypeError) as e:
        print(f"Validation error: {e}")
        raise SystemExit(1) from None

    cfg = manifest.config
    if cfg is None or not cfg.start_workflows:
        print("start_workflows is not enabled — no preflight checks required.")
        return

    workflow_id = cfg.workflow_definition_id
    param_set_id = cfg.param_set_id
    has_error = False

    try:
        workflow = asyncio.run(client.find_workflow(workflow_id))
        if workflow is None:
            print(f"MISSING  workflow_definition_id: {workflow_id!r}")
            has_error = True
        else:
            print(f"OK       workflow_definition_id: {workflow_id!r}")
    except Exception as e:
        print(f"ERROR    workflow_definition_id: {workflow_id!r} — {e}")
        has_error = True

    try:
        param_set = asyncio.run(client.find_param_set(param_set_id))
        if param_set is None:
            print(f"MISSING  param_set_id: {param_set_id!r}")
            has_error = True
        else:
            print(f"OK       param_set_id: {param_set_id!r}")
    except Exception as e:
        print(f"ERROR    param_set_id: {param_set_id!r} — {e}")
        has_error = True

    if has_error:
        raise SystemExit(1)
