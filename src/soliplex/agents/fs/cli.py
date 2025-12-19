import asyncio
import json
import logging
import os

import typer

from . import app

logger = logging.getLogger(__name__)
INVENTORY_FILE = "inventory.json"


cli = typer.Typer(no_args_is_help=True)


@cli.command("validate-config")
def validate(path: str):
    asyncio.run(app.validate_config(path))


@cli.command("build-config")
def build_config(path: str):
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
    config_path: str,
    source: str,
    detail: bool = False,
):
    asyncio.run(app.status_report(config_path, source, detail=detail))


@cli.command("run-inventory")
def run(
    config_path: str,
    source: str,
    start: int = 0,
    end: int = None,
    resume_batch: int = None,
    workflow_definition_id: str | None = None,
    param_set_id: str | None = None,
    start_workflows: bool = True,
    priority: int = 0,
):
    print(f"loading {config_path} source={source} to")
    if os.path.exists(config_path) and os.path.isdir(config_path):
        print(f"build config file for {config_path}")
        config_path = _build_config(config_path)
    res = asyncio.run(
        app.load_inventory(
            config_path,
            source,
            start,
            end,
            resume_batch,
            workflow_definition_id=workflow_definition_id,
            param_set_id=param_set_id,
            start_workflows=start_workflows,
            priority=priority,
        )
    )
    if res and len(res) > 0 and not isinstance(res, dict):
        logger.error("Found errors:")
        for error in res:
            logger.error(error)
    else:
        logger.info("Load successful.No errors found.")
        for k, v in res.items():
            print(f"{k}: {v}")


if __name__ == "__main__":
    app()
