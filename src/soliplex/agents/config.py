import enum

from pydantic_settings import BaseSettings


class SCM(str, enum.Enum):
    GITHUB = "github"
    GITEA = "gitea"


class Settings(BaseSettings):
    gitea_url: str | None = None
    gitea_token: str | None = None
    gitea_owner: str | None = "admin"
    gh_token: str | None = None
    gh_owner: str | None = None
    extensions: list[str] = ["md", "pdf", "doc", "docx"]
    log_level: str = "INFO"
    endpoint_url: str = "http://localhost:8000/api/v1"

    # Ingester API authentication (for outgoing requests to the Ingester API)
    ingester_api_key: str | None = None

    # Authentication settings (matching soliplex_ingester - for this agent's own API server)
    api_key: str | None = None
    api_key_enabled: bool = False
    auth_trust_proxy_headers: bool = False

    # Server settings
    server_host: str = "127.0.0.1"
    server_port: int = 8001

    # URL routing settings
    api_prefix: str = ""  # URL prefix for all routes (e.g., "/ingester-agent")
    root_path: str = ""  # Root path for reverse proxy (used for OpenAPI docs)


settings = Settings()
