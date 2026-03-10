"""Manifest runner — load YAML manifests and dispatch components to agents."""

import logging
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from soliplex.agents import client
from soliplex.agents.config import FSComponent
from soliplex.agents.config import Manifest
from soliplex.agents.config import SCMComponent
from soliplex.agents.config import WebComponent
from soliplex.agents.config import WebDAVComponent
from soliplex.agents.config import resolve_credential
from soliplex.agents.config import settings

logger = logging.getLogger(__name__)


def load_manifest(path: str) -> Manifest:
    """Read a YAML file and validate it as a Manifest.

    Args:
        path: Path to the YAML manifest file.

    Returns:
        Validated Manifest instance.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the YAML is invalid or fails Pydantic validation.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"Manifest file not found: {path}")
    try:
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in {path}: {e}") from e
    if not isinstance(raw, dict):
        raise TypeError(f"Expected a YAML mapping in {path}, got {type(raw).__name__}")
    return Manifest(**raw)


def load_manifests_from_dir(dir_path: str) -> list[Manifest]:
    """Load all YAML manifests from a directory.

    Skips files that fail to parse with a warning log.
    Validates that all manifest IDs are unique.

    Args:
        dir_path: Path to directory containing .yml/.yaml files.

    Returns:
        List of validated Manifest instances.

    Raises:
        ValueError: If duplicate manifest IDs are found.
    """
    directory = Path(dir_path)
    manifests = []
    for yml_file in sorted(directory.glob("*.yml")) + sorted(directory.glob("*.yaml")):
        try:
            manifests.append(load_manifest(str(yml_file)))
        except Exception:
            logger.warning(f"Skipping invalid manifest {yml_file}", exc_info=True)
    # Validate unique IDs
    ids = [m.id for m in manifests]
    duplicates = [i for i in set(ids) if ids.count(i) > 1]
    if duplicates:
        raise ValueError(f"Duplicate manifest IDs found: {sorted(duplicates)}")
    return manifests


@contextmanager
def override_settings(**kwargs):
    """Temporarily override settings attributes, restoring on exit.

    Args:
        **kwargs: Setting name/value pairs to override.
    """
    originals = {}
    for key, value in kwargs.items():
        originals[key] = getattr(settings, key)
        object.__setattr__(settings, key, value)
    try:
        yield
    finally:
        for key, value in originals.items():
            object.__setattr__(settings, key, value)


def _resolve_workflow_params(manifest: Manifest) -> dict:
    """Extract workflow params from manifest config."""
    if manifest.config is None:
        return {
            "start_workflows": False,
            "workflow_definition_id": None,
            "param_set_id": None,
            "priority": 0,
        }
    return {
        "start_workflows": manifest.config.start_workflows,
        "workflow_definition_id": manifest.config.workflow_definition_id,
        "param_set_id": manifest.config.param_set_id,
        "priority": manifest.config.priority,
    }


async def _run_fs_component(component: FSComponent, manifest: Manifest, wf_params: dict, metadata: dict) -> dict:
    """Dispatch an FSComponent to the filesystem agent."""
    from soliplex.agents.fs import app as fs_app

    extensions = manifest.get_extensions(component)
    overrides = {}
    if extensions is not None:
        overrides["extensions"] = extensions
    with override_settings(**overrides):
        return await fs_app.load_inventory(
            component.path,
            manifest.source,
            extra_metadata=metadata or None,
            **wf_params,
        )


async def _run_scm_component(component: SCMComponent, manifest: Manifest, wf_params: dict, metadata: dict) -> dict:
    """Dispatch an SCMComponent to the SCM agent."""
    from soliplex.agents.scm import app as scm_app

    extensions = manifest.get_extensions(component)
    overrides = {}
    if extensions is not None:
        overrides["extensions"] = extensions
    if component.auth_token:
        from pydantic import SecretStr

        overrides["scm_auth_token"] = SecretStr(resolve_credential(component.auth_token))
    if component.base_url:
        overrides["scm_base_url"] = component.base_url

    with override_settings(**overrides):
        if component.incremental:
            return await scm_app.incremental_sync(
                component.platform,
                component.repo,
                owner=component.owner,
                branch=component.branch,
                content_filter=component.content_filter,
                extra_metadata=metadata or None,
                source=manifest.source,
                **wf_params,
            )
        else:
            return await scm_app.load_inventory(
                component.platform,
                component.repo,
                owner=component.owner,
                content_filter=component.content_filter,
                extra_metadata=metadata or None,
                source=manifest.source,
                **wf_params,
            )


async def _run_webdav_component(component: WebDAVComponent, manifest: Manifest, wf_params: dict, metadata: dict) -> dict:
    """Dispatch a WebDAVComponent to the WebDAV agent."""
    from soliplex.agents.webdav import app as webdav_app

    extensions = manifest.get_extensions(component)
    overrides = {}
    if extensions is not None:
        overrides["extensions"] = extensions

    # Resolve credentials
    username = None
    password = None
    if component.username:
        username = resolve_credential(component.username)
    if component.password:
        password = resolve_credential(component.password)

    with override_settings(**overrides):
        if component.urls_file:
            return await webdav_app.load_inventory_from_urls(
                component.urls_file,
                manifest.source,
                webdav_url=component.url,
                webdav_username=username,
                webdav_password=password,
                extra_metadata=metadata or None,
                **wf_params,
            )
        elif component.urls:
            # Write URLs to a temp file and use load_inventory_from_urls
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
                tmp.write("\n".join(component.urls))
                tmp_path = tmp.name
            try:
                return await webdav_app.load_inventory_from_urls(
                    tmp_path,
                    manifest.source,
                    webdav_url=component.url,
                    webdav_username=username,
                    webdav_password=password,
                    extra_metadata=metadata or None,
                    **wf_params,
                )
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        else:
            return await webdav_app.load_inventory(
                component.path,
                manifest.source,
                webdav_url=component.url,
                webdav_username=username,
                webdav_password=password,
                extra_metadata=metadata or None,
                **wf_params,
            )


async def _run_web_component(component: WebComponent, manifest: Manifest, wf_params: dict, metadata: dict) -> dict:
    """Dispatch a WebComponent to the web agent."""
    from soliplex.agents.web import app as web_app

    resolved = await web_app.resolve_urls(
        url=component.url,
        urls=component.urls,
        urls_file=component.urls_file,
    )
    return await web_app.load_inventory(
        resolved,
        manifest.source,
        extra_metadata=metadata or None,
        **wf_params,
    )


_DISPATCH = {
    FSComponent: _run_fs_component,
    SCMComponent: _run_scm_component,
    WebDAVComponent: _run_webdav_component,
    WebComponent: _run_web_component,
}


async def _list_scm_all_uris(
    component: SCMComponent,
    manifest: Manifest,
) -> list[dict[str, str]]:
    """Fetch the full URI set for an SCM component.

    Used when ``delete_stale`` is enabled and the component uses
    incremental sync, which only returns changed files.
    """
    from soliplex.agents.scm import app as scm_app

    extensions = manifest.get_extensions(component)
    overrides: dict[str, object] = {}
    if extensions is not None:
        overrides["extensions"] = extensions
    if component.auth_token:
        from pydantic import SecretStr

        overrides["scm_auth_token"] = SecretStr(resolve_credential(component.auth_token))
    if component.base_url:
        overrides["scm_base_url"] = component.base_url

    with override_settings(**overrides):
        return await scm_app.list_all_uris(
            component.platform,
            component.repo,
            owner=component.owner,
            branch=component.branch,
            content_filter=component.content_filter,
        )


def collect_inventory_uris(result: dict[str, Any]) -> list[dict[str, str]]:
    """Extract URI/hash pairs from a component result's inventory.

    Each agent returns an ``inventory`` list whose items use either
    ``uri`` (SCM) or ``path`` (fs, webdav, web) as the identifier key.
    This helper normalises both into ``{"uri": ..., "sha256": ...}``
    dicts suitable for ``client.check_status()``.

    Args:
        result: The dict returned by a component handler.

    Returns:
        List of ``{"uri": str, "sha256": str}`` dicts.
    """
    items: list[dict[str, str]] = []
    for entry in result.get("inventory", []):
        uri = entry.get("uri") or entry.get("path")
        sha256 = entry.get("sha256", "")
        if uri:
            items.append({"uri": uri, "sha256": sha256})
    return items


async def run_manifest(manifest: Manifest) -> dict:
    """Run all components in a manifest.

    After every component has executed, if ``delete_stale`` is enabled
    in the manifest config **and** no component produced an error, a
    consolidated ``check_status`` call with ``delete_stale=True`` is
    made so the Ingester can remove documents whose URI no longer
    appears in any component.

    Args:
        manifest: Validated Manifest instance.

    Returns:
        Dict with manifest id/name, per-component results list,
        and optional delete_stale result.
    """
    wf_params = _resolve_workflow_params(manifest)
    results: list[dict[str, Any]] = []
    all_uri_hashes: list[dict[str, str]] = []
    has_errors = False
    incremental_scm_components: list[SCMComponent] = []

    for component in manifest.components:
        metadata = manifest.get_metadata(component)
        handler = _DISPATCH.get(type(component))
        if handler is None:
            logger.error(f"Unknown component type: {type(component)}")
            results.append({"component": component.name, "error": f"Unknown component type: {type(component)}"})
            has_errors = True
            continue
        try:
            result = await handler(component, manifest, wf_params, metadata)
            # Skip URI collection for incremental SCM — handled below
            if isinstance(component, SCMComponent) and component.incremental:
                incremental_scm_components.append(component)
            else:
                all_uri_hashes.extend(collect_inventory_uris(result))
            results.append({"component": component.name, "result": result})
        except Exception as e:
            logger.exception(f"Error running component {component.name}")
            results.append({"component": component.name, "error": str(e)})
            has_errors = True

    # --- full URI listing for incremental SCM components -----------------------
    if manifest.config and manifest.config.delete_stale and not has_errors and incremental_scm_components:
        for inc_component in incremental_scm_components:
            full_uris = await _list_scm_all_uris(inc_component, manifest)
            all_uri_hashes.extend(full_uris)

    # --- delete stale documents ------------------------------------------------
    delete_stale_result = None
    if manifest.config and manifest.config.delete_stale:
        if has_errors:
            logger.warning(
                "Skipping delete_stale for source %s: one or more components had errors",
                manifest.source,
            )
        else:
            delete_stale_result = await client.check_status(
                all_uri_hashes,
                manifest.source,
                delete_stale=True,
            )

    return {
        "manifest_id": manifest.id,
        "manifest_name": manifest.name,
        "results": results,
        "delete_stale_result": delete_stale_result,
    }


async def run_manifests(path: str) -> list[dict]:
    """Load and run manifests from a file or directory.

    Args:
        path: Path to a single YAML file or directory of YAML files.

    Returns:
        List of per-manifest result dicts.

    Raises:
        FileNotFoundError: If the path does not exist.
        ValueError: If duplicate manifest IDs are found (directory mode).
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Path not found: {path}")
    if p.is_file():
        manifests = [load_manifest(path)]
    else:
        manifests = load_manifests_from_dir(path)
    results = []
    for manifest in manifests:
        result = await run_manifest(manifest)
        results.append(result)
    return results
