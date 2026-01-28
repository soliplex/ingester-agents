import enum

from pydantic import SecretStr
from pydantic_settings import BaseSettings


class SCM(str, enum.Enum):
    GITHUB = "github"
    GITEA = "gitea"


class Settings(BaseSettings):
    # SCM settings
    scm_auth_token: SecretStr | None = None
    scm_auth_username: str | None = None
    scm_auth_password: SecretStr | None = None
    scm_base_url: str | None = None
    scm_owner: str | None = None

    # File settings
    extensions: list[str] = ["md", "pdf", "doc", "docx"]
    log_level: str = "INFO"
    endpoint_url: str = "http://localhost:8000/api/v1"

    # Ingester API authentication (for outgoing requests to the Ingester API)
    ingester_api_key: SecretStr | None = None

    # Authentication settings (matching soliplex_ingester - for this agent's own API server)
    api_key: SecretStr | None = None
    api_key_enabled: bool = False
    auth_trust_proxy_headers: bool = False
    ssl_verify: bool = True

    # Server settings
    server_host: str = "127.0.0.1"
    server_port: int = 8001

    # URL routing settings
    api_prefix: str = ""  # URL prefix for all routes (e.g., "/ingester-agent")
    root_path: str = ""  # Root path for reverse proxy (used for OpenAPI docs)

    # scheduler settings
    scheduler_enabled: bool = False  # turn on scheduler
    scheduler_modules: list[str] | None = None  # list of scheduler modules example: '["soliplex.agents.example"]'


settings = Settings()
