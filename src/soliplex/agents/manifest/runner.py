"""Manifest runner — load YAML manifests and dispatch components to agents."""

import datetime
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
    """Run all components in a manifest, between its pre-run and pre-process hooks.

    Every ``pre_run`` and ``pre_process`` method is imported first, so a typo
    fails the run before any step -- a "starting" notification, say -- has
    run. The ``pre_run`` steps then decide whether the run happens at all;
    when one returns SKIP, nothing else runs and the result carries
    ``skipped``. Otherwise the components run with this manifest's
    pre-process steps active, so every document they write passes through
    them (see :mod:`soliplex.agents.manifest.pre_process`).

    After every component has executed, if ``delete_stale`` is enabled
    in the manifest config **and** no component produced an error, a
    consolidated prune removes documents whose URI no longer appears in
    any component. Because every component in a manifest shares one
    ``source`` (and thus one download folder and state DB), pruning must
    happen once over the union of all component URIs, never per component.

    Args:
        manifest: Validated Manifest instance.

    Returns:
        Dict with manifest id/name, per-component results list, optional
        delete_stale result, ``summary``, ``pre_run`` and ``pre_process``
        outcomes, and ``skipped`` (``None`` unless a pre-run step skipped it).

    Raises:
        ImportError, AttributeError: when a hook's method cannot be imported.
        PreRunFailed: when a pre-run step fails under ``on_error: fail``.
    """
    from soliplex.agents.manifest import pre_process
    from soliplex.agents.manifest import pre_run
    from soliplex.agents.manifest.context import LoadContext

    pre_run_steps = pre_run.resolve_steps(manifest)
    pre_process_steps = pre_process.resolve_steps(manifest)
    started_at = datetime.datetime.now(datetime.UTC).isoformat()

    target = manifest.get_download_target()
    logger.info(
        "Starting manifest '%s' (%s) with %d components -> %s",
        manifest.id,
        manifest.name,
        len(manifest.components),
        target.base_uri,
    )
    with download_target(target):
        load = LoadContext.for_source(manifest.source)
        pre_run_outcome = await pre_run.run_pre_run(manifest, pre_run_steps, load=load, started_at=started_at)
        if pre_run_outcome["skipped"] is not None:
            return {
                "manifest_id": manifest.id,
                "manifest_name": manifest.name,
                "started_at": started_at,
                "skipped": pre_run_outcome["skipped"],
                "pre_run": pre_run_outcome["steps"],
                "pre_process": None,
                "results": [],
                "delete_stale_result": None,
                "summary": {"components": len(manifest.components), "skipped": True},
            }
        run = pre_process.PreProcessRun(
            manifest_id=manifest.id,
            source=manifest.source,
            steps=pre_process_steps,
            context=load,
        )
        with pre_process.activate(run):
            result = await _run_components(manifest, target, run)
    result["started_at"] = started_at
    result["skipped"] = None
    result["pre_run"] = pre_run_outcome["steps"]
    result["pre_process"] = run.report()
    return result


@contextmanager
def download_target(target):
    """Make *target* the store every agent in this manifest resolves.

    The agents read `settings.download_dir` / `settings.download_s3_bucket`
    rather than taking a target, so a per-manifest override is applied by
    overriding those for the duration. This relies on manifest execution being
    serialized, which the single worker in `server.manifest_queue` enforces --
    the same assumption `override_settings` already makes for `extensions`.

    Threading the resolved target through `write_document` and the four agents
    instead would remove that assumption; it is a wider change than the
    override is worth until something actually runs manifests concurrently.
    """
    from soliplex.agents.store import reset_store_cache

    reset_store_cache()
    try:
        with override_settings(
            download_dir=target.dir,
            download_s3_bucket=target.bucket,
        ):
            yield
    finally:
        # Stores are cached per resolved target; drop them so the next manifest
        # does not inherit this one's.
        reset_store_cache()


def _component_type(component) -> str:
    """The component's manifest ``type`` (``fs``, ``webdav``, ...), or its class name."""
    ctype = getattr(component, "type", None)
    return ctype if isinstance(ctype, str) else type(component).__name__


def _count(result: dict[str, Any], key: str) -> int:
    """Length of the list an agent reported under *key* (0 when absent)."""
    value = result.get(key)
    return len(value) if isinstance(value, list) else 0


async def _run_components(manifest: Manifest, target, run) -> dict:
    """Execute a manifest's components and reconcile, under a resolved target.

    The returned ``summary`` is the one set of outcome counts for the run: the
    final log line reads it, and so can anything reporting on the run. A
    component *fails* when its handler raises; it finishes *with file errors*
    when it returns per-file ``errors``. Either makes the run a failure.
    *run* is the active :class:`~soliplex.agents.manifest.pre_process.PreProcessRun`,
    whose counts join the summary.
    """
    results: list[dict[str, Any]] = []
    summary = {
        "components": len(manifest.components),
        "component_errors": 0,
        "components_with_file_errors": 0,
        "file_errors": 0,
        "ingested": 0,
        "not_found": 0,
        "deleted": 0,
        "pre_process_checked": 0,
        "pre_process_skipped": 0,
        "pre_process_modified": 0,
        "pre_process_errors": 0,
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
                results.append({"component": component.name, "result": result})
                # Per-file transient errors (timeout/5xx) block the reconcile to
                # stay safe, mirroring a raised component exception.
                file_errors = _count(result, "errors")
                component_span.set_attributes(
                    {
                        "component.ingested": _count(result, "ingested"),
                        "component.errors": file_errors,
                        "component.not_found": _count(result, "not_found"),
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
    summary["pre_process_checked"] = run.checked
    summary["pre_process_skipped"] = len(run.skipped)
    summary["pre_process_modified"] = len(run.modified)
    summary["pre_process_errors"] = len(run.errors)
    # ERROR, not INFO, whenever anything failed: a level filter must find a run
    # whose files failed even though every component returned.
    failed = summary["component_errors"] or summary["file_errors"]
    logger.log(
        logging.ERROR if failed else logging.INFO,
        "Manifest '%s' finished: %d components, %d component errors, %d file errors, %d not found (404), %d deleted, "
        "%d pre-process skipped, %d modified",
        manifest.id,
        summary["components"],
        summary["component_errors"],
        summary["file_errors"],
        summary["not_found"],
        summary["deleted"],
        summary["pre_process_skipped"],
        summary["pre_process_modified"],
    )

    return {
        "manifest_id": manifest.id,
        "manifest_name": manifest.name,
        "results": results,
        "delete_stale_result": delete_stale_result,
        "summary": summary,
    }


def installation_target(source: str):
    """The target *source* resolves to with no manifest override applied.

    This is what a manifest is migrating *away from*: the override in the
    manifest names the destination, so the installation default is the origin.
    """
    from soliplex.agents.store import DownloadTarget
    from soliplex.agents.store import storage_options

    return DownloadTarget(
        dir=settings.download_dir,
        source=source,
        bucket=settings.download_s3_bucket,
        storage_options=storage_options() if settings.download_s3_bucket else {},
    )


async def migrate_store(manifest: Manifest, dry_run: bool = False) -> dict:
    """Copy a source's documents from its installation target to its override.

    Without this, flipping a manifest's ``download_store`` still works -- the
    target-qualified state file means everything re-fetches from upstream (see
    :func:`~soliplex.agents.local_state.get_state_path`). This exists to avoid
    that re-fetch, which is slow and re-hits rate limits on SCM and WebDAV
    sources.

    Copies rather than moves, in both halves: the documents *and* the state
    file are left in place at the origin, which is the whole rollback story --
    setting ``target: fs`` back then costs nothing.

    Args:
        manifest: The manifest whose override names the destination.
        dry_run: Report what would be copied without writing anything.

    Returns:
        Dict with ``source``, ``from``, ``to``, ``keys``, ``copied``,
        ``state_copied``, and ``dry_run``.
    """
    import shutil

    from soliplex.agents.local_state import close_state_connections
    from soliplex.agents.local_state import get_state_path
    from soliplex.agents.store import LocalDocumentStore
    from soliplex.agents.store import S3DocumentStore

    def _store(target):
        return LocalDocumentStore(target) if target.is_local else S3DocumentStore(target)

    origin = installation_target(manifest.source)
    destination = manifest.get_download_target()
    result = {
        "source": manifest.source,
        "from": origin.base_uri,
        "to": destination.base_uri,
        "keys": 0,
        "copied": 0,
        "state_copied": False,
        "dry_run": dry_run,
    }
    if origin.base_uri == destination.base_uri:
        logger.info("Source '%s' already targets %s; nothing to migrate", manifest.source, destination.base_uri)
        return result

    src, dst = _store(origin), _store(destination)
    keys = await src.list()
    result["keys"] = len(keys)
    logger.info(
        "Migrating source '%s': %d object(s) %s -> %s%s",
        manifest.source,
        len(keys),
        origin.base_uri,
        destination.base_uri,
        " (dry run)" if dry_run else "",
    )
    if dry_run:
        return result

    for key in keys:
        await dst.write(key, await src.read(key))
        result["copied"] += 1

    # The state file too, so the next run sees the documents as already present
    # instead of re-fetching them.
    old_state = get_state_path(manifest.source, origin)
    new_state = get_state_path(manifest.source, destination)
    if old_state.is_file() and old_state != new_state:
        # Close first: under WAL the newest commits sit in the -wal sidecar
        # until a clean close checkpoints them, so copying the .db alone
        # while a connection is open would silently drop recent rows.
        close_state_connections(old_state)
        new_state.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(old_state, new_state)
        result["state_copied"] = True
    return result


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
    and the remaining manifests still run. A manifest a pre-run step skipped is
    returned with ``skipped`` set and is not loaded.

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
            if result.get("skipped"):
                # A pre-run step called the run off: nothing to load.
                results.append(result)
                continue
            if load:
                from soliplex.agents.manifest import haiku_loader

                try:
                    result["haiku_load"] = await haiku_loader.run_load(manifest, run_result=dict(result))
                except Exception as e:
                    logger.exception("haiku load failed for manifest '%s' (%s)", manifest.id, manifest.name)
                    result["haiku_load_error"] = str(e)
                    telemetry.fail(span, f"haiku load failed: {type(e).__name__}", e)
            results.append(result)
    return results


def pre_process_report(
    manifest: Manifest,
    *,
    status: str | None = None,
    message: str | None = None,
    changed_since: str | None = None,
) -> list[dict]:
    """The latest pre-process outcome per document of *manifest*'s source.

    Read from the state DB of the target the manifest resolves to; filters are
    those of :func:`~soliplex.agents.local_state.list_pre_process`.
    """
    with download_target(manifest.get_download_target()):
        return local_state.list_pre_process(manifest.source, status=status, message=message, changed_since=changed_since)


def pre_run_report(manifest: Manifest, *, status: str | None = None, since: str | None = None) -> list[dict]:
    """Recorded pre-run outcomes for *manifest*'s source, newest run first."""
    with download_target(manifest.get_download_target()):
        return local_state.list_pre_run(manifest.source, status=status, since=since)


def reprocess(
    manifest: Manifest,
    *,
    status: str | None = None,
    method: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Make the next run of *manifest* re-fetch and re-pre-process matching documents.

    See :func:`~soliplex.agents.local_state.reprocess`.

    Returns:
        ``{"manifest_id", "source", "uris", "dry_run"}``.
    """
    with download_target(manifest.get_download_target()):
        uris = local_state.reprocess(manifest.source, status=status, method=method, dry_run=dry_run)
    return {"manifest_id": manifest.id, "source": manifest.source, "uris": uris, "dry_run": dry_run}
