"""Manifest runner — load YAML manifests and dispatch components to agents."""

import logging
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from soliplex.agents import local_state
from soliplex.agents import telemetry
from soliplex.agents.config import FSComponent
from soliplex.agents.config import Manifest
from soliplex.agents.config import SCMComponent
from soliplex.agents.config import WebComponent
from soliplex.agents.config import WebDAVComponent
from soliplex.agents.config import resolve_credential
from soliplex.agents.config import settings

logger = logging.getLogger(__name__)

# Sentinel path meaning "every manifest in settings.manifest_dir".
ALL_MANIFESTS = "all"


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
    manifest = Manifest(**raw)
    manifest.manifest_dir = str(file_path.parent.resolve())
    return manifest


@dataclass(frozen=True)
class ScanResult:
    """What a manifest directory holds, including what is wrong with it.

    ``invalid`` maps each file that failed to load to why; ``duplicates``
    lists ids declared by more than one file. Reporting them rather than
    logging or raising lets a caller that scans repeatedly (the scheduler's
    reconcile tick) decide how often to say so.
    """

    pairs: list[tuple[Manifest, str]]
    invalid: dict[str, str]
    duplicates: list[str]


def scan_manifests(dir_path: str) -> ScanResult:
    """Load every ``.yml`` / ``.yaml`` manifest in *dir_path*, never raising.

    Args:
        dir_path: Path to directory containing .yml/.yaml files.

    Returns:
        A :class:`ScanResult`.
    """
    directory = Path(dir_path)
    pairs: list[tuple[Manifest, str]] = []
    invalid: dict[str, str] = {}
    for yml_file in sorted(directory.glob("*.yml")) + sorted(directory.glob("*.yaml")):
        try:
            pairs.append((load_manifest(str(yml_file)), str(yml_file)))
        except Exception as e:
            invalid[str(yml_file)] = str(e)
    ids = [m.id for m, _ in pairs]
    duplicates = sorted(i for i in set(ids) if ids.count(i) > 1)
    return ScanResult(pairs=pairs, invalid=invalid, duplicates=duplicates)


def load_manifests_with_paths(
    dir_path: str,
) -> list[tuple[Manifest, str]]:
    """Load all YAML manifests from a directory, returning file paths.

    Skips files that fail to parse with a warning log.
    Validates that all manifest IDs are unique.

    Args:
        dir_path: Path to directory containing .yml/.yaml files.

    Returns:
        List of (Manifest, file_path) tuples.

    Raises:
        ValueError: If duplicate manifest IDs are found.
    """
    scan = scan_manifests(dir_path)
    for path, error in scan.invalid.items():
        logger.warning("Skipping invalid manifest %s: %s", path, error)
    if scan.duplicates:
        raise ValueError(f"Duplicate manifest IDs found: {scan.duplicates}")
    return scan.pairs


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
    return [m for m, _ in load_manifests_with_paths(dir_path)]


def resolve_manifests(path: str) -> list[Manifest]:
    """Resolve *path* into a list of manifests.

    ``path`` is either the sentinel ``"all"`` (every ``.yml``/``.yaml`` in
    ``settings.manifest_dir``), a single manifest file, or a directory of
    manifests. ``"all"`` is therefore a reserved word: a file or directory
    literally named ``all`` cannot be addressed by name.

    Args:
        path: ``"all"``, a manifest file path, or a directory path.

    Returns:
        Validated Manifest instances (invalid files in directory mode are
        skipped with a warning by :func:`load_manifests_with_paths`).

    Raises:
        FileNotFoundError: If *path* does not exist, or ``"all"`` was given
            and ``settings.manifest_dir`` is unset or not a directory.
        ValueError: If duplicate manifest IDs are found (directory mode).
    """
    if path == ALL_MANIFESTS:
        if not settings.manifest_dir:
            raise FileNotFoundError(
                f"MANIFEST_DIR (settings.manifest_dir) must be set to use '{ALL_MANIFESTS}'",
            )
        if not Path(settings.manifest_dir).is_dir():
            raise FileNotFoundError(f"MANIFEST_DIR is not a directory: {settings.manifest_dir}")
        return load_manifests_from_dir(settings.manifest_dir)
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Path not found: {path}")
    if p.is_file():
        return [load_manifest(path)]
    return load_manifests_from_dir(path)


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


async def _run_fs_component(component: FSComponent, manifest: Manifest, metadata: dict) -> dict:
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
        )


async def _run_scm_component(component: SCMComponent, manifest: Manifest, metadata: dict) -> dict:
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
            )
        else:
            return await scm_app.load_inventory(
                component.platform,
                component.repo,
                owner=component.owner,
                content_filter=component.content_filter,
                extra_metadata=metadata or None,
                source=manifest.source,
            )


async def _run_webdav_component(component: WebDAVComponent, manifest: Manifest, metadata: dict) -> dict:
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
                base_dir=manifest.manifest_dir,
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
            )


async def _run_web_component(component: WebComponent, manifest: Manifest, metadata: dict) -> dict:
    """Dispatch a WebComponent to the web agent."""
    from soliplex.agents.web import app as web_app

    resolved = await web_app.resolve_urls(
        url=component.url,
        urls=component.urls,
        urls_file=component.urls_file,
        base_dir=manifest.manifest_dir,
    )
    return await web_app.load_inventory(
        resolved,
        manifest.source,
        extra_metadata=metadata or None,
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
    dicts suitable for stale-document pruning.

    Args:
        result: The dict returned by a component handler.

    Returns:
        List of ``{"uri": str, "sha256": str}`` dicts.
    """
    items: list[dict[str, str]] = []
    for entry in result.get("inventory", []):
        uri = entry.get("uri") or entry.get("path")
        sha256 = entry.get("sha256") or ""
        if uri:
            items.append({"uri": uri, "sha256": sha256})
    return items


async def run_manifest(manifest: Manifest) -> dict:
    """Run all components in a manifest.

    After every component has executed, if ``delete_stale`` is enabled
    in the manifest config **and** no component produced an error, a
    consolidated prune removes documents whose URI no longer appears in
    any component. Because every component in a manifest shares one
    ``source`` (and thus one download folder and state DB), pruning must
    happen once over the union of all component URIs, never per component.

    Args:
        manifest: Validated Manifest instance.

    Returns:
        Dict with manifest id/name, per-component results list,
        and optional delete_stale result.
    """
    target = manifest.get_download_target()
    logger.info(
        "Starting manifest '%s' (%s) with %d components -> %s",
        manifest.id,
        manifest.name,
        len(manifest.components),
        target.base_uri,
    )
    return await _run_components(manifest)


def _component_type(component) -> str:
    """The component's manifest ``type`` (``fs``, ``webdav``, ...), or its class name."""
    ctype = getattr(component, "type", None)
    return ctype if isinstance(ctype, str) else type(component).__name__


def _count(result: dict[str, Any], key: str) -> int:
    """Length of the list an agent reported under *key* (0 when absent)."""
    value = result.get(key)
    return len(value) if isinstance(value, list) else 0


async def _run_components(manifest: Manifest) -> dict:
    """Execute a manifest's components and reconcile.

    The returned ``summary`` is the one set of outcome counts for the run: the
    final log line reads it, and so can anything reporting on the run. A
    component *fails* when its handler raises; it finishes *with file errors*
    when it returns per-file ``errors``. Either makes the run a failure.
    """
    results: list[dict[str, Any]] = []
    summary = {
        "components": len(manifest.components),
        "component_errors": 0,
        "components_with_file_errors": 0,
        "file_errors": 0,
        "ingested": 0,
        "not_found": 0,
        "rejected": 0,
        "deleted": 0,
        "delete_stale_skipped": False,
    }
    all_uri_hashes: list[dict[str, str]] = []
    all_not_found: set[str] = set()
    has_errors = False
    incremental_scm_components: list[SCMComponent] = []

    for component in manifest.components:
        ctype = _component_type(component)
        attributes = {
            telemetry.COMPONENT_NAME: component.name,
            telemetry.COMPONENT_TYPE: ctype,
            telemetry.MANIFEST_ID: manifest.id,
        }
        with telemetry.span("component", f"component {component.name} ({ctype})", attributes) as component_span:
            logger.info("Running component '%s' (type=%s)", component.name, type(component).__name__)
            metadata = manifest.get_metadata(component)
            handler = _DISPATCH.get(type(component))
            if handler is None:
                logger.error("Unknown component type: %s", type(component))
                results.append({"component": component.name, "error": f"Unknown component type: {type(component)}"})
                summary["component_errors"] += 1
                has_errors = True
                telemetry.fail(component_span, "unknown component type")
                continue
            try:
                result = await handler(component, manifest, metadata)
                # Skip URI collection for incremental SCM — handled below
                if isinstance(component, SCMComponent) and component.incremental:
                    incremental_scm_components.append(component)
                else:
                    all_uri_hashes.extend(collect_inventory_uris(result))
                # 404s are removals, not errors: exclude them from the reconcile
                # "should exist" set so their local copies are deleted.
                all_not_found.update(result.get("not_found", []))
                summary["ingested"] += _count(result, "ingested")
                summary["rejected"] += _count(result, "rejected")
                results.append({"component": component.name, "result": result})
                # Per-file transient errors (timeout/5xx) block the reconcile to
                # stay safe, mirroring a raised component exception.
                file_errors = _count(result, "errors")
                component_span.set_attributes(
                    {
                        "component.ingested": _count(result, "ingested"),
                        "component.errors": file_errors,
                        "component.not_found": _count(result, "not_found"),
                        "component.rejected": _count(result, "rejected"),
                    }
                )
                if file_errors:
                    has_errors = True
                    summary["components_with_file_errors"] += 1
                    summary["file_errors"] += file_errors
                    logger.warning("Component '%s' finished with %d file errors", component.name, file_errors)
                    telemetry.fail(component_span, f"{file_errors} file errors")
                else:
                    logger.info("Component '%s' completed successfully", component.name)
            except Exception as e:
                logger.exception("Error running component %s", component.name)
                results.append({"component": component.name, "error": str(e)})
                summary["component_errors"] += 1
                has_errors = True
                # Caught so the next component still runs: fail the span by hand.
                telemetry.fail(component_span, f"component failed: {type(e).__name__}", e)

    # --- full URI listing for incremental SCM components -----------------------
    if manifest.config and manifest.config.delete_stale and not has_errors and incremental_scm_components:
        for inc_component in incremental_scm_components:
            with telemetry.span(
                "list scm uris",
                f"list scm uris {inc_component.name}",
                {telemetry.COMPONENT_NAME: inc_component.name, telemetry.MANIFEST_ID: manifest.id},
            ):
                full_uris = await _list_scm_all_uris(inc_component, manifest)
            all_uri_hashes.extend(full_uris)

    # --- delete stale documents ------------------------------------------------
    delete_stale_result = None
    if manifest.config and manifest.config.delete_stale:
        if has_errors:
            summary["delete_stale_skipped"] = True
            logger.warning(
                "Skipping delete_stale for source %s: one or more components had errors",
                manifest.source,
            )
        else:
            current_uris = {item["uri"] for item in all_uri_hashes} - all_not_found
            with telemetry.span(
                "delete stale",
                f"delete stale {manifest.source}",
                {telemetry.MANIFEST_ID: manifest.id, telemetry.MANIFEST_SOURCE: manifest.source},
            ) as stale_span:
                delete_stale_result = await local_state.reconcile_documents(manifest.source, current_uris)
                stale_span.set_attribute("manifest.deleted", len(delete_stale_result or []))

    summary["deleted"] = len(delete_stale_result or [])
    summary["not_found"] = len(all_not_found)
    # ERROR, not INFO, whenever anything failed: a level filter must find a run
    # whose files failed even though every component returned.
    failed = summary["component_errors"] or summary["file_errors"]
    logger.log(
        logging.ERROR if failed else logging.INFO,
        "Manifest '%s' finished: %d components, %d component errors, %d file errors, %d not found (404), %d deleted",
        manifest.id,
        summary["components"],
        summary["component_errors"],
        summary["file_errors"],
        summary["not_found"],
        summary["deleted"],
    )

    return {
        "manifest_id": manifest.id,
        "manifest_name": manifest.name,
        "results": results,
        "delete_stale_result": delete_stale_result,
        "summary": summary,
    }


async def run_manifests(path: str, load: bool = False) -> list[dict]:
    """Load and run manifests from a file or directory.

    When *load* is true, a haiku-rag batch load runs (awaited) after each
    manifest. The sequential loop guarantees only one load runs at a time.

    Args:
        path: ``"all"`` (every manifest in ``settings.manifest_dir``), a
            single YAML file, or a directory of YAML files.
        load: Run a haiku-rag load after each manifest.

    A failure while running or loading one manifest is isolated to that
    manifest: it is logged and recorded (an ``error`` on the result, or a
    ``haiku_load_error`` when the load/post-process step is the one that failed)
    and the remaining manifests still run.

    Returns:
        List of per-manifest result dicts.

    Raises:
        FileNotFoundError: If the path does not exist.
        ValueError: If duplicate manifest IDs are found (directory mode).
    """
    manifests = resolve_manifests(path)
    results = []
    for manifest in manifests:
        # The same span the server's queue opens, so a CLI run traces the same
        # way whenever Logfire is configured (and costs nothing when it isn't).
        with telemetry.manifest_span(manifest.id) as span:
            telemetry.describe_manifest(span, manifest)
            try:
                result = await run_manifest(manifest)
            except Exception as e:
                logger.exception("Manifest '%s' (%s) failed", manifest.id, manifest.name)
                results.append({"manifest_id": manifest.id, "manifest_name": manifest.name, "error": str(e)})
                telemetry.fail(span, f"manifest run failed: {type(e).__name__}", e)
                continue
            telemetry.record_summary(span, result["summary"])
            if load:
                from soliplex.agents.manifest import haiku_loader

                try:
                    result["haiku_load"] = await haiku_loader.run_load(manifest)
                except Exception as e:
                    logger.exception("haiku load failed for manifest '%s' (%s)", manifest.id, manifest.name)
                    result["haiku_load_error"] = str(e)
                    telemetry.fail(span, f"haiku load failed: {type(e).__name__}", e)
            results.append(result)
    return results
