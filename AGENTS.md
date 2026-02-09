# AGENTS.md

Instructions for AI coding agents working with Soliplex Agents.

## Project Overview

Document ingestion agents that load files from multiple sources (filesystem, WebDAV, GitHub, Gitea) into Soliplex Ingester for processing and indexing.

**Stack:** Python 3.13+, FastAPI, aiohttp, Typer CLI, Pydantic v2

## Quick Reference

```bash
# Install dependencies
uv sync

# Run tests (100% branch coverage required)
uv run pytest

# Format and lint
uv run ruff format . && uv run ruff check .

# Start REST API server
uv run --env-file .env si-agent serve --reload

# Filesystem ingestion
si-agent fs run-inventory /path/to/docs my-source

# SCM incremental sync
si-agent scm run-incremental gitea myowner/myrepo
```

## Project Structure

```text
src/soliplex/agents/
├── cli.py              # Main Typer CLI entry point
├── client.py           # Ingester API client (HTTP operations)
├── config.py           # Pydantic settings
├── common/
│   └── config.py       # File validation utilities
├── fs/                 # Filesystem agent
│   ├── cli.py          # CLI commands
│   └── app.py          # Business logic
├── scm/                # Source control agent
│   ├── cli.py          # CLI commands
│   ├── app.py          # SCM orchestration
│   ├── base.py         # BaseSCMProvider abstract class
│   ├── github/         # GitHub provider implementation
│   ├── gitea/          # Gitea provider implementation
│   └── lib/
│       ├── utils.py    # Hashing utilities
│       └── templates/  # Jinja2 templates
├── webdav/             # WebDAV agent
│   ├── cli.py
│   └── app.py
└── server/             # FastAPI REST API
    ├── __init__.py     # App setup, CORS, scheduler
    ├── auth.py         # Authentication
    └── routes/         # API endpoints
```

## Code Conventions

### Python Style

- PEP8 with 126 char line length (ruff configured)
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

# Incorrect
from soliplex_agents.client import IngesterClient
```

### Hashing Algorithms

Different contexts use different algorithms:

```python
# Filesystem/WebDAV files: SHA256
import hashlib
hashlib.sha256(content, usedforsecurity=False).hexdigest()

# SCM files: SHA3-256
hashlib.sha3_256(content).hexdigest()

# SCM issues: SHA256
hashlib.sha256(content.encode()).hexdigest()
```

## Testing

```bash
# Run all unit tests
uv run pytest

# Run with coverage report
uv run pytest --cov-report=html

# Run specific test
uv run pytest tests/unit/test_client.py
```

**Requirements:**
- 100% branch coverage for non-excluded code
- Unit tests in `tests/unit/`
- Functional tests in `tests/functional/` (skipped by default)
- Mock external services (Ingester API, GitHub, Gitea)

**Coverage Exclusions:**
- `*/cli.py` - CLI modules
- `*/app.py` - App orchestration
- `*/templates/*` - Jinja2 templates
- `*/server/*` - Server modules

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

### Server Authentication

```bash
API_KEY=<key>
API_KEY_ENABLED=false
AUTH_TRUST_PROXY_HEADERS=false
```

See `config.py` for full settings reference.

## CLI Commands

```text
si-agent
├── fs
│   ├── build-config <path>              # Scan directory
│   ├── validate-config <path>           # Validate files
│   ├── check-status <path> <source>     # Check ingestion status
│   └── run-inventory <path> <source>    # Ingest documents
├── scm
│   ├── list-issues <platform> <repo> <owner>
│   ├── get-repo <platform> <repo> <owner>
│   ├── run-inventory <platform> <repo> <owner>
│   ├── run-incremental <platform> <repo> <owner>
│   ├── get-sync-state <platform> <repo> <owner>
│   └── reset-sync <platform> <repo> <owner>
├── webdav
│   ├── build-config <path>
│   ├── validate-config <path>
│   ├── check-status <path> <source>
│   └── run-inventory <path> <source>
└── serve [--host] [--port] [--reload]
```

## API Endpoints

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | /api/v1/fs/build-config | Scan filesystem |
| POST | /api/v1/fs/run-inventory | Ingest from filesystem |
| GET | /api/scm/{platform}/{repo}/issues | List issues |
| GET | /api/scm/{platform}/{repo}/files | List files |
| POST | /api/scm/{platform}/{repo}/ingest | Ingest from SCM |
| POST | /api/v1/webdav/build-config | Scan WebDAV |
| POST | /api/v1/webdav/run-inventory | Ingest from WebDAV |
| GET | /health | Health check |

## Key Patterns

### SCM Provider Pattern

Strategy pattern with abstract base class:

```python
from soliplex.agents.scm.base import BaseSCMProvider
from soliplex.agents.scm.github import GitHubProvider
from soliplex.agents.scm.gitea import GiteaProvider

# Factory pattern with optional Git CLI decorator
def get_provider(platform: str) -> BaseSCMProvider:
    if platform == "github":
        provider = GitHubProvider()
    elif platform == "gitea":
        provider = GiteaProvider()

    # Git CLI mode wraps provider with decorator
    if settings.scm_use_git_cli:
        from soliplex.agents.scm.git_cli import GitCliDecorator
        provider = GitCliDecorator(provider)

    return provider
```

**Git CLI Decorator:** When `scm_use_git_cli=true`, the decorator intercepts file operations to use local git clone instead of API calls. API-only operations (issues, repo management) are delegated to the wrapped provider.

### Batch Management

Files are grouped into batches by source:
- System reuses existing batch if source matches
- Creates new batch only if none exists
- Enables incremental ingestion (only new/changed files)

### Incremental Sync (SCM)

Commit-based tracking for efficient syncing:
1. Get last processed commit SHA from Ingester
2. Fetch commits since that SHA
3. Extract changed file paths
4. Download only modified files
5. Store new commit SHA

## File Organization

When adding features:
- Agent logic goes in `{agent}/app.py`
- CLI commands go in `{agent}/cli.py`
- API endpoints go in `server/routes/{agent}.py`
- Tests go in `tests/unit/test_{module}.py`

## Critical Constraints

- Do not mix hashing algorithms (SHA256 vs SHA3-256)
- Always use async/await for I/O operations
- Batch names must be unique per source
- WebDAV requires SSL verification by default
- SCM providers must implement `BaseSCMProvider` interface

## Authentication Priority

1. Token auth (`scm_auth_token`) - preferred
2. Basic auth (`scm_auth_username`/`scm_auth_password`) - fallback
3. No auth - public repositories only

## Commit Standards

When asked to commit:
- Use conventional commit format
- Include `Co-Authored-By: Claude <noreply@anthropic.com>` trailer
- Stage specific files, avoid `git add -A`
- Never commit .env files or secrets
