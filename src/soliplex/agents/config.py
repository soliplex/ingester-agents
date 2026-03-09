import enum
import logging
import os
from pathlib import Path
from typing import Annotated
from typing import Literal

from pydantic import BaseModel
from pydantic import Field
from pydantic import SecretStr
from pydantic import model_validator
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict

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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets")
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
    endpoint_url: str = "http://localhost:8000/api/v1"

    # Ingester API authentication (for outgoing requests to the Ingester API)
    ingester_api_key: SecretStr | None = None

    # Authentication settings (matching soliplex_ingester - for this agent's own API server)
    api_key: SecretStr | None = None
    api_key_enabled: bool = False
    auth_trust_proxy_headers: bool = False
    ssl_verify: bool = True

    # HTTP timeout settings (seconds)
    http_timeout_total: int = 120
    http_timeout_connect: int = 10
    http_timeout_sock_read: int = 60

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
    scheduler_modules: list[str] | None = None  # list of scheduler modules example: '["soliplex.agents.example"]'

    # State settings
    state_dir: str = "sync_state"

    # Manifest settings
    manifest_dir: str | None = None  # Directory with manifest .yml files for scheduling

    # Git CLI settings
    scm_use_git_cli: bool = False  # Use git CLI instead of API for file operations
    scm_git_cli_timeout: int = 300  # Timeout for git operations (seconds)
    scm_git_repo_base_dir: str | None = None  # Base directory for cloned repos (default: tempdir)


settings = Settings()


def configure_logging():
    """Configure logging from settings, with safe fallback."""
    try:
        logging.basicConfig(
            level=settings.log_level,
            format=settings.log_format,
            datefmt="%Y-%m-%dT%H:%M:%S",
            style="{",
        )
    except Exception:
        logging.basicConfig(
            level=logging.INFO,
            format="{name}|{asctime}|{levelname}|{message}",
            datefmt="%Y-%m-%dT%H:%M:%S",
            style="{",
        )
        logging.getLogger().warning("invalid settings. environment variables might not be set. ")


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


class FSComponent(BaseModel):
    """Filesystem ingestion component."""

    type: Literal["fs"] = "fs"
    name: str
    path: str
    extensions: list[str] | None = None
    metadata: dict[str, str] | None = None


class SCMComponent(BaseModel):
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


class WebDAVComponent(BaseModel):
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


class WebComponent(BaseModel):
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


Component = Annotated[
    FSComponent | SCMComponent | WebDAVComponent | WebComponent,
    Field(discriminator="type"),
]


# --- Manifest Config ---


class ManifestConfig(BaseModel):
    """Shared configuration applied to all components in a manifest."""

    extensions: list[str] | None = None
    metadata: dict[str, str] | None = None
    start_workflows: bool = False
    workflow_definition_id: str | None = None
    param_set_id: str | None = None
    priority: int = 0

    @model_validator(mode="after")
    def validate_workflow_params(self):
        if self.start_workflows:
            if self.workflow_definition_id is None or self.param_set_id is None:
                raise ValueError("start_workflows requires both workflow_definition_id and param_set_id")
        return self


class Schedule(BaseModel):
    """Cron schedule for automated manifest execution."""

    cron: str


# --- Top-level Manifest ---


class Manifest(BaseModel):
    """Top-level manifest defining a group of ingestion components sharing a single source."""

    id: str
    name: str
    source: str
    schedule: Schedule | None = None
    config: ManifestConfig | None = None
    components: list[Component]

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

    def get_metadata(self, component: FSComponent | SCMComponent | WebDAVComponent | WebComponent) -> dict[str, str]:
        """Resolve metadata for a component (config metadata merged with component metadata on top)."""
        merged = {}
        if self.config and self.config.metadata:
            merged.update(self.config.metadata)
        if component.metadata:
            merged.update(component.metadata)
        return merged
