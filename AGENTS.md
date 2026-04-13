# AGENTS.md

Instructions for AI coding agents working with Soliplex Agents.

## Project Overview

Document ingestion agents that load files from multiple sources (filesystem,
WebDAV, GitHub, Gitea, web pages) into Soliplex Ingester for processing and
indexing. Supports declarative YAML manifests for multi-source orchestration.

Stack: Python 3.13+, FastAPI, aiohttp, Typer CLI, Pydantic v2, Tenacity

## Quick Reference

```bash
uv sync                                        # Install dependencies
uv run pytest                                  # Run tests (100% branch coverage)
uv run ruff format . && uv run ruff check .    # Format and lint
uv run --env-file .env si-agent serve --reload # Start REST API server
si-agent fs run-inventory /path/to/docs my-source
si-agent scm run-incremental gitea myowner/myrepo
si-agent manifest run manifests/
```

## Project Structure

```text
src/soliplex/agents/
├── cli.py              # Main Typer CLI entry point
├── client.py           # Ingester API client (HTTP operations)
├── config.py           # Pydantic settings, manifest/component models
├── retry.py            # Shared retry utilities (tenacity-based)
├── common/
│   ├── config.py       # File validation utilities (MIME detection)
│   ├── s3.py           # S3 URL parsing and aioboto3 integration
│   └── urls_file.py    # Multi-source URL list reading
├── fs/                 # Filesystem agent
│   ├── cli.py          # CLI commands
│   └── app.py          # Async file scanning, hashing, ingestion
├── web/                # Web agent (manifest/API only, no CLI)
│   └── app.py          # HTTP fetch and ingest logic
├── scm/                # Source control agent
│   ├── cli.py          # CLI commands
│   ├── app.py          # SCM orchestration
│   ├── base.py         # BaseSCMProvider abstract class
│   ├── git_cli.py      # Git CLI decorator (local clone mode)
│   ├── github/         # GitHub provider implementation
│   ├── gitea/          # Gitea provider implementation
│   └── lib/
│       ├── utils.py    # SHA-256 hashing utilities
│       └── templates/  # Jinja2 issue templates
├── webdav/             # WebDAV agent
│   ├── cli.py          # CLI commands
│   ├── app.py          # Directory scanning, ingestion
│   ├── async_client.py # Async WebDAV client wrapper
│   └── state.py        # ETag-based cache state
├── manifest/           # Manifest runner
│   ├── cli.py          # CLI commands
│   └── runner.py       # YAML loading, validation, dispatch
└── server/             # FastAPI REST API
    ├── __init__.py     # App setup, CORS, cron scheduler
    ├── auth.py         # API key and OAuth2 proxy auth
    ├── locks.py        # Async locks for manifest execution
    └── routes/         # Endpoint handlers (fs, scm, webdav, web, manifest)

tests/
├── unit/              # Unit tests (100% branch coverage)
└── functional/        # Integration tests (skipped by default, uses VCR)
```

## Code Conventions

### Python Style

- Ruff enforced, 126 char line length, target py313
- snake_case for functions/variables, PascalCase for classes
- Type annotations required (Python 3.13+ syntax)
- Single-line imports, grouped: stdlib, third-party, local

### Async Requirements

All I/O operations must use async/await with aiohttp:

```python
async with aiohttp.ClientSession() as session:
    async with session.get(url) as response:
        data = await response.json()
```

### Import Paths

Use `soliplex.agents` (dot notation):

```python
# Correct
from soliplex.agents.client import IngesterClient
from soliplex.agents.config import get_settings

# Wrong
from soliplex_agents.client import IngesterClient
```

### Hashing

SHA-256 is used throughout the entire codebase for all file types:

```python
import hashlib
hashlib.sha256(content, usedforsecurity=False).hexdigest()
```

## Testing

```bash
uv run pytest                           # All unit tests
uv run pytest --cov-report=html         # With HTML coverage report
uv run pytest tests/unit/test_client.py # Specific test file
```

Requirements:

- 100% branch coverage for non-excluded code
- Unit tests in `tests/unit/`
- Functional tests in `tests/functional/` (skipped by default)
- Mock external services (Ingester API, GitHub, Gitea, WebDAV)

Coverage exclusions (pyproject.toml):

- `*/cli.py` - CLI modules
- `*/app.py` - App orchestration
- `*/templates/*` - Jinja2 templates
- `*/conftest.py` - Pytest fixtures
- `*/server/*` - Server modules

## CLI Commands

```text
si-agent
├── fs
│   ├── build-config <path>              # Scan directory
│   ├── validate-config <path>           # Validate file support
│   ├── check-status <path> <source>     # Check ingestion status
│   └── run-inventory <path> <source>    # Ingest documents
├── scm                                  # All use <platform> <owner/repo> format
│   ├── list-issues <platform> <owner/repo>
│   ├── get-repo <platform> <owner/repo>
│   ├── run-inventory <platform> <owner/repo>
│   ├── run-incremental <platform> <owner/repo>
│   ├── get-sync-state <platform> <owner/repo>
│   └── reset-sync <platform> <owner/repo>
├── webdav
│   ├── validate-config <path>
│   ├── export-urls <path> <output-file>
│   ├── check-status <path> <source>
│   ├── run-inventory <path> <source>
│   └── run-from-urls <urls-file> <source>
├── manifest
│   └── run <path>                       # Run manifest file or directory
└── serve [--host] [--port] [--reload]   # Start REST API server
```

Note: The web agent has no CLI. It is accessible via manifest components and
REST API routes only.

## API Endpoints

All routes are under a configurable `api_prefix` (default: none).

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | /api/v1/fs/build-config | Scan filesystem directory |
| POST | /api/v1/fs/validate-config | Validate file support |
| POST | /api/v1/fs/check-status | Check ingestion status |
| POST | /api/v1/fs/run-inventory | Ingest from filesystem |
| GET | /api/v1/scm/{scm}/issues | List SCM issues |
| GET | /api/v1/scm/{scm}/repo | List SCM repo files |
| POST | /api/v1/scm/run-inventory | Ingest from SCM |
| POST | /api/v1/scm/incremental-sync | Incremental SCM sync |
| POST | /api/v1/webdav/validate-config | Validate WebDAV path |
| POST | /api/v1/webdav/check-status | Check ingestion status |
| POST | /api/v1/webdav/run-inventory | Ingest from WebDAV |
| POST | /api/v1/webdav/run-from-file | Ingest from URL list |
| POST | /api/v1/web/run-inventory | Ingest web pages |
| POST | /api/v1/web/run-from-file | Ingest from URL list |
| POST | /api/v1/manifest/run | Execute manifest |
| POST | /api/v1/manifest/run-single | Execute single component |
| POST | /api/v1/manifest/validate | Validate manifest |
| GET | /health | Health check |

## Configuration

### Required

```bash
ENDPOINT_URL=http://localhost:8000/api/v1   # Ingester API
```

### SCM Authentication

```bash
scm_auth_token=<token>                      # GitHub PAT or Gitea token
scm_base_url=https://gitea.example.com/api/v1  # Required for Gitea
```

### WebDAV

```bash
WEBDAV_URL=https://webdav.example.com
WEBDAV_USERNAME=<username>
WEBDAV_PASSWORD=<password>
```

### Server

```bash
SERVER_HOST=127.0.0.1
SERVER_PORT=8001
API_KEY_ENABLED=false
AUTH_TRUST_PROXY_HEADERS=false
```

See `config.py` for full settings reference.

## Key Patterns

### SCM Provider Pattern

Strategy pattern with abstract base class:

```python
from soliplex.agents.scm.base import BaseSCMProvider
from soliplex.agents.scm.github import GitHubProvider
from soliplex.agents.scm.gitea import GiteaProvider
```

Git CLI Decorator: When `scm_use_git_cli=true`, wraps the provider to use
local git clone instead of API calls. Issues are still fetched via API.

### Batch Management

Files are grouped into batches by source name. The system reuses existing
batches for incremental ingestion. Only new and changed files are processed.

### Incremental Sync (SCM)

Commit-based tracking:

1. Get last processed commit SHA from Ingester
2. Fetch commits since that SHA
3. Extract changed file paths
4. Download only modified files
5. Store new commit SHA

### Manifest Runner

Declarative YAML for multi-source orchestration. Supports cron scheduling
when `scheduler_enabled=true`. Components: fs, scm, webdav, web.

## File Organization

When adding features:

- Agent logic goes in `{agent}/app.py`
- CLI commands go in `{agent}/cli.py`
- API endpoints go in `server/routes/{agent}.py`
- Tests go in `tests/unit/test_{module}.py`

## Critical Constraints

- All I/O must use async/await
- SHA-256 for all hashing (do not introduce other algorithms)
- Batch names must be unique per source
- SCM providers must implement `BaseSCMProvider` interface
- WebDAV requires SSL verification by default (`SSL_VERIFY=true`)

## Authentication Priority (SCM)

1. Token auth (`scm_auth_token`) -- preferred
2. Basic auth (`scm_auth_username`/`scm_auth_password`) -- fallback
3. No auth -- public repositories only

## Commit Standards

When asked to commit:

- Use conventional commit format
- Include `Co-Authored-By: Claude <noreply@anthropic.com>` trailer
- Stage specific files, avoid `git add -A`
- Never commit .env files or secrets
