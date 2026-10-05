import enum
import json
import logging
import logging.handlers
import os
import time
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Annotated
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr
from pydantic import field_validator
from pydantic import model_validator
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict

from soliplex.agents import log_config
from soliplex.agents.common.s3 import split_bucket

logger = logging.getLogger(__name__)


class SCM(enum.StrEnum):
    GITHUB = "github"
    GITEA = "gitea"


class ContentFilter(enum.StrEnum):
    ALL = "all"
    FILES = "files"
    ISSUES = "issues"


class ComponentType(enum.StrEnum):
    FS = "fs"
    SCM = "scm"
    WEBDAV = "webdav"
    WEB = "web"


# Only point pydantic-settings at the secrets dir when it exists, so dev/test
# runs (which lack /run/secrets) don't emit a spurious "directory does not
# exist" UserWarning. In the container the dir is present and secrets load.
_SECRETS_DIR = "/run/secrets"
_secrets_kwargs: dict = {"secrets_dir": _SECRETS_DIR} if Path(_SECRETS_DIR).is_dir() else {}  # pragma: no branch


def _checked_bucket(value: str | None) -> str | None:
    """Normalize and validate a configured download bucket.

    Accepts a bare name or a full ``s3://bucket/prefix`` URI, and rejects an
    unusable one while configuration is being read rather than at the first
    write.

    A blank value becomes ``None``, so object storage is disabled by clearing
    the variable rather than by deleting the line. That is the only way to turn
    it off from a compose ``.env``, where a key is always present once it is
    referenced -- and an empty bucket would otherwise read as "object storage,
    nowhere". Whitespace counts as blank: ``DOWNLOAD_S3_BUCKET= `` with a
    stray trailing space is a disabled store, not a configuration error.
    """
    value = value.strip() if value else value
    if not value:
        return None
    split_bucket(value)
    return value


def _blank_is_none(value: str | None) -> str | None:
    """Treat a blank (or whitespace-only) value as unset, as a compose ``.env`` must."""
    value = value.strip() if value else value
    return value or None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(**_secrets_kwargs)
    # SCM settings
    scm_auth_token: SecretStr | None = None
    scm_auth_username: str | None = None
    scm_auth_password: SecretStr | None = None
    scm_base_url: str | None = None

    # WebDAV settings
    webdav_url: str | None = None
    webdav_username: str | None = None
    webdav_password: SecretStr | None = None

    # File settings
    extensions: list[str] = ["md", "pdf", "doc", "docx"]
    log_level: str = "INFO"
    log_format: str = "{name}|{asctime}|{levelname}|{message}"
    # A logging.config.dictConfig file (YAML or JSON) applied on top of the
    # built-in setup, e.g. to send particular loggers to an HTTP sink. When it
    # cannot be applied, the built-in setup is used and a warning logged --
    # or, with LOG_CONFIG_STRICT, logging setup (and so startup) fails.
    log_config_file: str | None = None
    log_config_strict: bool = False

    _validate_log_config_file = field_validator("log_config_file", mode="after")(_blank_is_none)

    # SMTP email alert settings (handler only added when smtp_host is set)
    smtp_host: str | None = None
    smtp_port: int = 25
    smtp_from: str | None = None
    smtp_to: list[str] | None = None
    smtp_subject: str = "Soliplex Agents Log Alert"
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_use_tls: bool = False
    smtp_log_level: str = "ERROR"
    smtp_cooldown: int = 30  # minimum seconds between emails

    # Authentication settings (for this agent's own API server)
    api_key: SecretStr | None = None
    api_key_enabled: bool = False
    auth_trust_proxy_headers: bool = False
    ssl_verify: bool = True

    # HTTP timeout settings (seconds)
    http_timeout_total: int = 120
    http_timeout_connect: int = 10
    http_timeout_sock_read: int = 60

    # WebDAV concurrency settings
    webdav_max_concurrent_requests: int = 3

    # SCM concurrency and retry settings
    scm_max_concurrent_requests: int = 3
    scm_retry_attempts: int = 3
    scm_retry_backoff_base: float = 1.0
    scm_retry_backoff_max: float = 30.0

    # URL routing settings
    api_prefix: str = ""  # URL prefix for all routes (e.g., "/ingester-agent")
    root_path: str = ""  # Root path for reverse proxy (used for OpenAPI docs)

    # scheduler settings
    scheduler_enabled: bool = False  # turn on scheduler
    # Cron expression for how often the manifest directory is rescanned to
    # hot-reload schedule changes and added/removed manifest files.
    scheduler_reconcile_cron: str = "*/1 * * * *"

    # State settings
    state_dir: str = "sync_state"

    # Local download settings (where agents write fetched documents)
    download_dir: str = "downloads"

    # Manifest settings
    manifest_dir: str | None = None  # Directory with manifest .yml files for scheduling

    # Pre-process spool: where each document is staged while its pre-process
    # steps run, before it is uploaded. Needs room for the largest document.
    # Default (None) is the system temp dir -- often tmpfs (RAM) in containers.
    pre_process_spool_dir: str | None = None

    # haiku-rag load settings (run after each manifest run)
    haiku_load_enabled: bool = False  # Queue a haiku-rag load after each manifest run
    lancedb_dir: str | None = None  # Base dir for per-source .lancedb databases (LANCEDB_DIR)
    haiku_path: str | None = None  # Base dir for haiku-rag config files (HAIKU_PATH)
    haiku_default_config: str = "haiku.rag.default.yaml"  # Default config filename under haiku_path
    # Full command template; placeholders: {haiku_cfg} {db} {source} {lancedb_dir} {haiku_path}
    haiku_load_command: str = "haiku-ingester --config={haiku_cfg} run-batch"
    haiku_load_timeout: int = 1800  # Timeout for a single load subprocess (seconds)
    haiku_load_cwd: str | None = None  # Working dir for the load subprocess (default: inherit)

    # haiku subprocess output (load and maintenance), forwarded to the log in
    # parts: one record per chunk, split at a line break where possible, and
    # at least every flush interval while output is pending.
    haiku_output_chunk_bytes: int = Field(default=64 * 1024, gt=0)
    haiku_output_flush_seconds: float = Field(default=30.0, gt=0)
    haiku_output_max_bytes: int = Field(default=0, ge=0)  # logged per stream; 0 = unlimited
    # Run haiku commands through `python -m soliplex.agents.traced_run`, which
    # attaches the TRACEPARENT the agent exports, so haiku-ingester's own spans
    # join the agent's trace. Only needed while haiku-rag doesn't read
    # TRACEPARENT itself; harmless alongside it.
    haiku_trace_wrapper: bool = False

    # haiku-rag maintenance settings (`manifest migrate` / `manifest vacuum`)
    # Placeholders: {verb} {haiku_cfg} {db} {source} {lancedb_dir} {haiku_path}
    haiku_maintenance_command: str = "haiku-rag --config={haiku_cfg} {verb}"
    # Timeout for a single maintenance subprocess (seconds); higher than the
    # load timeout because a vacuum/compaction can outlast a batch load.
    haiku_maintenance_timeout: int = 3600

    # Logfire token (from /run/secrets/logfire_token or LOGFIRE_TOKEN); enables
    # observability for this process and is passed to the haiku load subprocess.
    logfire_token: SecretStr | None = None
    logfire_service_name: str = "ingester-agents"

    # S3 settings. `s3_endpoint_url` is shared with the urls_file reader; the
    # rest are the writer's credentials. All optional -- unset means fall
    # through to the AWS default credential chain.
    s3_endpoint_url: str | None = None  # Custom S3 endpoint (for MinIO, etc.)
    s3_access_key_id: str | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_region: str | None = None
    s3_allow_http: bool = False  # Required for an http:// endpoint

    # Download store: setting a bucket moves DOWNLOAD_DIR into object storage,
    # where it becomes the key prefix rather than a local directory. Named
    # DOWNLOAD_S3_BUCKET rather than S3_BUCKET so an unrelated variable in the
    # environment cannot silently redirect every download.
    download_s3_bucket: str | None = None

    _validate_bucket = field_validator("download_s3_bucket", mode="after")(_checked_bucket)

    # Git CLI settings
    scm_use_git_cli: bool = False  # Use git CLI instead of API for file operations
    scm_git_cli_timeout: int = 300  # Timeout for git operations (seconds)
    scm_git_repo_base_dir: str | None = None  # Base directory for cloned repos (default: tempdir)


settings = Settings()


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON object."""

    _BUILTIN_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)))

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")
        obj: dict = {
            "timestamp": ts,
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            obj["exception"] = self.formatException(record.exc_info)
        for key, val in record.__dict__.items():
            if key not in self._BUILTIN_ATTRS:
                obj[key] = val
        return json.dumps(obj, default=str)


class _ThrottledSMTPHandler(logging.handlers.SMTPHandler):
    """SMTPHandler that suppresses emails within a cooldown period."""

    def __init__(self, *args, cooldown: int = 30, **kwargs):
        super().__init__(*args, **kwargs)
        self._cooldown = cooldown
        self._last_emit: float = 0

    def emit(self, record):
        now = time.monotonic()
        if now - self._last_emit < self._cooldown:
            return
        self._last_emit = now
        super().emit(record)


def _add_smtp_handler():
    """Attach an SMTPHandler to the root logger if SMTP settings are configured."""
    if not (settings.smtp_host and settings.smtp_from and settings.smtp_to):
        return
    try:
        credentials = None
        if settings.smtp_username and settings.smtp_password:
            credentials = (
                settings.smtp_username,
                settings.smtp_password.get_secret_value(),
            )
        secure = () if settings.smtp_use_tls else None
        handler = _ThrottledSMTPHandler(
            mailhost=(settings.smtp_host, settings.smtp_port),
            fromaddr=settings.smtp_from,
            toaddrs=settings.smtp_to,
            subject=settings.smtp_subject,
            credentials=credentials,
            secure=secure,
            cooldown=settings.smtp_cooldown,
        )
        handler.setLevel(settings.smtp_log_level)
        if settings.log_format == "json":
            handler.setFormatter(JsonFormatter())
        else:
            handler.setFormatter(
                logging.Formatter(
                    fmt=settings.log_format,
                    datefmt="%Y-%m-%dT%H:%M:%S",
                    style="{",
                )
            )
        logging.getLogger().addHandler(handler)
    except Exception:
        logger.warning(
            "Failed to configure SMTP log handler",
            exc_info=True,
        )


def _make_formatter():
    """Return the appropriate formatter based on settings."""
    if settings.log_format == "json":
        return JsonFormatter()
    return logging.Formatter(
        fmt=settings.log_format,
        datefmt="%Y-%m-%dT%H:%M:%S",
        style="{",
    )


def configure_logging():
    """Configure logging from settings, with safe fallback.

    When ``settings.log_format`` equals ``"json"``, a
    `JsonFormatter` is installed; otherwise the value is
    used as a ``str.format``-style pattern.

    ``settings.log_config_file``, when set, is applied on top (see
    :mod:`soliplex.agents.log_config`): it adds handlers and may set the root
    level, but the console and SMTP handlers are installed either way. Safe to
    call repeatedly; each call replaces what the last one installed.
    """
    root = logging.getLogger()
    log_config.stop_listeners()
    root.handlers.clear()
    try:
        root.setLevel(settings.log_level)
        formatter = _make_formatter()
        invalid_settings = False
    except Exception:
        root.setLevel(logging.INFO)
        formatter = logging.Formatter(
            fmt="{name}|{asctime}|{levelname}|{message}",
            datefmt="%Y-%m-%dT%H:%M:%S",
            style="{",
        )
        invalid_settings = True

    # Before the console handler is created: dictConfig closes every handler
    # that already exists.
    config_error = None
    if settings.log_config_file:
        try:
            log_config.apply_file(settings.log_config_file)
        except log_config.LogConfigError as exc:
            config_error = exc

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root.addHandler(handler)
    if invalid_settings:
        root.warning("invalid settings. environment variables might not be set. ")
    if config_error is not None:
        if settings.log_config_strict:
            raise config_error
        logger.warning("%s; using the built-in logging setup", config_error)
    _add_smtp_handler()


# --- Credential Resolution ---


def resolve_credential(value: str) -> str:
    """Resolve a credential value by checking docker secrets first, then environment variables.

    Args:
        value: A docker secret name or environment variable name.

    Returns:
        The resolved credential value.

    Raises:
        ValueError: If the credential cannot be resolved from either source.
    """
    secret_path = Path(f"/run/secrets/{value}")
    if secret_path.is_file():
        return secret_path.read_text().strip()

    env_value = os.environ.get(value)
    if env_value is not None:
        return env_value

    raise ValueError(f"Credential '{value}' not found in /run/secrets/ or environment variables")


# --- Manifest Component Models ---


class _ManifestModel(BaseModel):
    """Base for every model a manifest file is parsed into.

    Unknown keys are rejected rather than silently dropped: a typo such as
    ``extentions`` or ``delete_stail`` would otherwise leave the setting at its
    default without a word, and a key that has been removed (``download_store``)
    would be ignored while the manifest ran somewhere else than intended. A
    rejected manifest is reported like any other invalid one -- once, at ERROR,
    by the scheduler's reconcile; as a validation error by the CLI.

    Free-form fields (``metadata``, a post-process step's ``kwargs``) are dicts,
    so they still accept any keys.
    """

    model_config = ConfigDict(extra="forbid")


class FSComponent(_ManifestModel):
    """Filesystem ingestion component."""

    type: Literal["fs"] = "fs"
    name: str
    path: str
    extensions: list[str] | None = None
    metadata: dict[str, str] | None = None


class SCMComponent(_ManifestModel):
    """Source control management ingestion component."""

    type: Literal["scm"] = "scm"
    name: str
    platform: SCM
    owner: str
    repo: str
    incremental: bool = False
    branch: str = "main"
    content_filter: ContentFilter = ContentFilter.ALL
    base_url: str | None = None
    auth_token: str | None = None
    extensions: list[str] | None = None
    metadata: dict[str, str] | None = None

    @model_validator(mode="after")
    def validate_gitea_base_url(self):
        if self.platform == SCM.GITEA and self.base_url is None:
            if settings.scm_base_url is None:
                logger.warning(
                    f"Component '{self.name}': Gitea platform requires base_url "
                    "(set in component or via scm_base_url env var)"
                )
        return self


def _check_inline_urls(name: str, entries: list[str], is_valid: Callable[[str], bool], expected: str) -> None:
    """Raise if any inline URL entry of component *name* fails *is_valid*.

    The same rule the component's ``urls_file`` lines are held to at run time,
    applied at load so ``manifest validate`` catches a typo before any run.
    """
    invalid = [entry for entry in entries if not is_valid(entry.strip())]
    if invalid:
        raise ValueError(f"Component '{name}': each URL must be {expected}; invalid: {invalid[:5]!r}")


class WebDAVComponent(_ManifestModel):
    """WebDAV ingestion component."""

    type: Literal["webdav"] = "webdav"
    name: str
    url: str
    path: str | None = None
    urls: list[str] | None = None
    urls_file: str | None = None
    username: str | None = None
    password: str | None = None
    extensions: list[str] | None = None
    metadata: dict[str, str] | None = None

    @model_validator(mode="after")
    def validate_source_specified(self):
        sources = [self.path is not None, self.urls is not None, self.urls_file is not None]
        if sum(sources) == 0:
            raise ValueError(f"Component '{self.name}': one of 'path', 'urls', or 'urls_file' is required")
        if sum(sources) > 1:
            raise ValueError(f"Component '{self.name}': only one of 'path', 'urls', or 'urls_file' may be specified")
        return self

    @model_validator(mode="after")
    def validate_urls_are_paths(self):
        # Imported here: urls_file imports this module (for settings).
        from soliplex.agents.common.urls_file import is_webdav_path

        _check_inline_urls(self.name, self.urls or [], is_webdav_path, "an absolute WebDAV path starting with '/'")
        return self


class WebComponent(_ManifestModel):
    """Web page ingestion component (fetches raw HTML)."""

    type: Literal["web"] = "web"
    name: str
    url: str | None = None
    urls: list[str] | None = None
    urls_file: str | None = None
    extensions: list[str] | None = None
    metadata: dict[str, str] | None = None

    @model_validator(mode="after")
    def validate_source_specified(self):
        sources = [self.url is not None, self.urls is not None, self.urls_file is not None]
        if sum(sources) == 0:
            raise ValueError(f"Component '{self.name}': one of 'url', 'urls', or 'urls_file' is required")
        if sum(sources) > 1:
            raise ValueError(f"Component '{self.name}': only one of 'url', 'urls', or 'urls_file' may be specified")
        return self

    @model_validator(mode="after")
    def validate_urls_are_http(self):
        # Imported here: urls_file imports this module (for settings).
        from soliplex.agents.common.urls_file import is_http_url

        entries = [self.url] if self.url is not None else self.urls or []
        _check_inline_urls(self.name, entries, is_http_url, "an http:// or https:// URL with a host")
        return self


Component = Annotated[
    FSComponent | SCMComponent | WebDAVComponent | WebComponent,
    Field(discriminator="type"),
]


# --- Manifest Config ---


class PostProcessStep(_ManifestModel):
    """One post-load callback, invoked as ``method(source, **kwargs)``.

    ``method`` is a dotted import path (``pkg.mod:func`` or ``pkg.mod.func``)
    to a callable importable in the agent's environment; ``kwargs`` are passed
    through as keyword arguments. Steps run in order after the load, and the
    runner fills in ``config``, ``context``, ``ingester``, ``ingester_exit_code``
    and ``run_result`` for a callable that accepts them -- see
    :mod:`soliplex.agents.manifest.post_process`.
    """

    method: str
    kwargs: dict[str, Any] = Field(default_factory=dict)


class PreRunStep(_ManifestModel):
    """One step run once, before any component, invoked as ``method(context, **kwargs)``.

    A step may return SKIP to call the run off (no components, no load, no
    post-process). ``on_error`` decides what a raising or timed-out step means;
    see :mod:`soliplex.agents.manifest.pre_run`.
    """

    method: str
    kwargs: dict[str, Any] = Field(default_factory=dict)
    on_error: Literal["continue", "skip", "fail"] = "fail"
    timeout: float | None = Field(default=300, gt=0)  # seconds; None = no limit


class PreProcessStep(_ManifestModel):
    """One step run on each new or changed document, before it is stored.

    Invoked as ``method(document, **kwargs)``; ``mime_types`` limits it to
    documents of those detected types (``None`` = every document). See
    :mod:`soliplex.agents.manifest.pre_process`.
    """

    method: str
    kwargs: dict[str, Any] = Field(default_factory=dict)
    mime_types: list[str] | None = None
    on_error: Literal["continue", "skip", "fail"] = "continue"


class ManifestConfig(_ManifestModel):
    """Shared configuration applied to all components in a manifest."""

    extensions: list[str] | None = None
    metadata: dict[str, str] | None = None
    delete_stale: bool = True
    haiku_config: str | None = None  # Per-manifest haiku-rag config (abs path, or filename under HAIKU_PATH)
    # Ordered steps run once before any component (see
    # soliplex.agents.manifest.pre_run).
    pre_run: list[PreRunStep] = Field(default_factory=list)
    # Ordered steps run on each new or changed document before it is stored
    # (see soliplex.agents.manifest.pre_process).
    pre_process: list[PreProcessStep] = Field(default_factory=list)
    # Ordered callbacks run after the haiku-rag load completes (see
    # soliplex.agents.manifest.post_process).
    post_process: list[PostProcessStep] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def reject_download_store(cls, data: Any) -> Any:
        """Explain the one removed key, rather than a bare "extra input".

        Where a source's documents go is chosen per installation --
        ``DOWNLOAD_S3_BUCKET`` for object storage, with an S3 haiku config via
        ``HAIKU_DEFAULT_CONFIG`` or this manifest's ``haiku_config``. A
        per-manifest override was applied by temporarily rewriting the shared
        settings, which the haiku load and its callbacks, running later, never
        saw.
        """
        if isinstance(data, dict) and "download_store" in data:
            raise ValueError(
                "download_store is no longer supported: storage is chosen per installation "
                "(DOWNLOAD_S3_BUCKET, with an S3 haiku config via HAIKU_DEFAULT_CONFIG or haiku_config)"
            )
        return data


class Schedule(_ManifestModel):
    """Cron schedule for automated manifest execution."""

    cron: str


# --- Top-level Manifest ---


class Manifest(_ManifestModel):
    """Top-level manifest defining a group of ingestion components sharing a single source."""

    id: str
    name: str
    source: str
    schedule: Schedule | None = None
    config: ManifestConfig | None = None
    components: list[Component]
    manifest_dir: str | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def validate_unique_component_names(self):
        names = [c.name for c in self.components]
        duplicates = [n for n in names if names.count(n) > 1]
        if duplicates:
            raise ValueError(f"Duplicate component names: {set(duplicates)}")
        return self

    def get_extensions(self, component: FSComponent | SCMComponent | WebDAVComponent | WebComponent) -> list[str] | None:
        """Resolve extensions for a component (component > config > None for global fallback)."""
        if component.extensions is not None:
            return component.extensions
        if self.config and self.config.extensions is not None:
            return self.config.extensions
        return None

    def get_download_target(self, download_dir: str | None = None):
        """Where this manifest's documents are written: the installation's store.

        Chosen per installation (``DOWNLOAD_DIR``, or ``DOWNLOAD_S3_BUCKET`` for
        object storage), never per manifest, so the ingest, the haiku load and
        its callbacks all resolve the same place. Returns a
        :class:`~soliplex.agents.store.DownloadTarget`.

        Args:
            download_dir: Override for the resolved directory (mainly tests).
        """
        from soliplex.agents.store import get_document_store

        return get_document_store(self.source, download_dir).target

    def get_metadata(self, component: FSComponent | SCMComponent | WebDAVComponent | WebComponent) -> dict[str, str]:
        """Resolve metadata for a component (config metadata merged with component metadata on top)."""
        merged = {}
        if self.config and self.config.metadata:
            merged.update(self.config.metadata)
        if component.metadata:
            merged.update(component.metadata)
        return merged
