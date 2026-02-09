import asyncio
import json
import logging
import os
from typing import Annotated

import typer

from . import app

logger = logging.getLogger(__name__)
INVENTORY_FILE = "inventory.json"


cli = typer.Typer(no_args_is_help=True)


@cli.command("validate-config")
def validate(
    config_file: Annotated[
        str,
        typer.Argument(help="path to inventory file or directory (will build config if directory)"),
    ],
):
    """
    Validate a configuration.

    If a file is provided, it will be treated as an inventory.json config file.
    If a directory is provided, a config will be built from the directory contents.
    """
    asyncio.run(app.validate_config(config_file))


@cli.command("build-config")
def build_config(path: Annotated[str, typer.Argument(help="path to document directory")]):
    cfg_file = _build_config(path)
    cfg_data = json.load(open(cfg_file))
    print(f"created {cfg_file} with {len(cfg_data)} files")


def _build_config(path: str):
    config = asyncio.run(app.build_config(path))
    cfg_file = os.path.join(path, INVENTORY_FILE)
    json.dump(config, open(cfg_file, "w"), indent=2)
    return cfg_file


@cli.command("check-status")
def check_status(
    config_file: Annotated[
        str,
        typer.Argument(help="path to inventory file or directory (will build config if directory)"),
    ],
    source: Annotated[str, typer.Argument(help="source name")],
    detail: bool = False,
):
    """
    Check the status of files in an inventory.

    If a file is provided, it will be treated as an inventory.json config file.
    If a directory is provided, a config will be built from the directory contents.
    """
    asyncio.run(app.status_report(config_file, source, detail=detail))


@cli.command("run-inventory")
def run(
    config_file: Annotated[
        str,
        typer.Argument(help="path to inventory file or directory (will build config if directory)"),
    ],
    source: Annotated[str, typer.Argument(help="source name")],
    start: Annotated[int, typer.Option(help="start index")] = 0,
    end: Annotated[int, typer.Option(help="end index")] = None,
    start_workflows: Annotated[bool, typer.Option(help="start workflows")] = False,
    workflow_definition_id: Annotated[str, typer.Option(help="workflow definition id")] = None,
    param_set_id: Annotated[str, typer.Option(help="param set id")] = None,
    priority: Annotated[int, typer.Option(help="workflow priority")] = 0,
    do_json: Annotated[bool, typer.Option(help="output json")] = False,
):
    """
    Run an inventory ingestion.

    If a file is provided, it will be treated as an inventory.json config file.
    If a directory is provided, a config will be built from the directory contents.
    """
    if start_workflows:
        if workflow_definition_id is None:
            raise Exception("workflow_definition_id is required when start_workflows is true")  # noqa: TRY002
        if param_set_id is None:
            raise Exception("param_set_id is required when start_workflows is true")  # noqa: TRY002
    print(f"loading {config_file} source={source}")
    if os.path.exists(config_file) and os.path.isdir(config_file):
        print(f"build config file for {config_file}")
        config_file = _build_config(config_file)
    res = asyncio.run(
        app.load_inventory(
            config_file,
            source,
            start,
            end,
            workflow_definition_id=workflow_definition_id,
            param_set_id=param_set_id,
            start_workflows=start_workflows,
            priority=priority,
        )
    )
    if do_json:
        print(json.dumps(res, indent=2))
    else:
        if "errors" in res and len(res["errors"]) > 0:
            print(f"found {len(res['errors'])} errors:")
            for err in res["errors"]:
                print(err)
        else:
            print("no errors found")
            print(f"found {len(res['inventory'])} files")
            print(f"found {len(res['to_process'])} to process")
            if "ingested" in res and len(res["ingested"]) > 0:
                print(f"{len(res['ingested'])} ingested")
            else:
                print("no ingested files")
            if start_workflows:
                print("workflow result")
                print(json.dumps(res["workflow_result"], indent=2))


if __name__ == "__main__":
    app()
