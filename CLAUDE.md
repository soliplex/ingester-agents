# Soliplex Ingester-Agents

Document ingestion CLI for loading files from filesystem, WebDAV, and SCM platforms into Soliplex Ingester.

## Quick Reference

- **Package:** soliplex.agents
- **CLI:** si-agent
- **Python:** 3.13+
- **Entry:** src/soliplex/agents/cli.py

## Commands

```bash
# Development
uv sync                     # Install dependencies
uv run pytest               # Run tests (100% branch coverage required)
uv run ruff check --fix     # Lint and auto-fix
uv run ruff format          # Format code

# Filesystem agent
si-agent fs run-inventory <path> <source>
si-agent fs check-status <path> <source>
si-agent fs build-config <path>
si-agent fs validate-config <path>

# SCM agent (github/gitea)
si-agent scm run-inventory <platform> <repo> <owner>
si-agent scm run-incremental <platform> <repo> <owner>
si-agent scm list-issues <platform> <repo> <owner>
si-agent scm get-repo <platform> <repo> <owner>
si-agent scm get-sync-state <platform> <repo> <owner>
si-agent scm reset-sync <platform> <repo> <owner>

# WebDAV agent
si-agent webdav run-inventory <path> <source>
si-agent webdav check-status <path> <source>
si-agent webdav build-config <path>
si-agent webdav validate-config <path>

# REST API server
si-agent serve
si-agent serve --host 0.0.0.0 --port 8080
si-agent serve --reload
```

## Critical Constraints

- **Test coverage:** 100% branch coverage required for non-CLI code
- **Hashing algorithms:**
  - Filesystem/WebDAV: SHA256
  - SCM files: SHA3-256 (see src/soliplex/agents/scm/lib/utils.py)
  - SCM issues: SHA256
- **Async patterns:** All I/O uses aiohttp/aiofiles
- **Provider pattern:** SCM uses strategy pattern (base.py + implementations)

## Project Structure

```
src/soliplex/agents/
├── cli.py              # Main Typer entry point
├── client.py           # Ingester API client (batch, status, ingest, sync state)
├── config.py           # Pydantic Settings (environment variables)
├── common/config.py    # Shared validation utilities
├── fs/                 # Filesystem agent
│   ├── cli.py          # CLI commands
│   └── app.py          # Business logic
├── webdav/             # WebDAV agent
│   ├── cli.py          # CLI commands
│   └── app.py          # Business logic
├── scm/                # SCM agent
│   ├── cli.py          # CLI commands
│   ├── app.py          # Orchestration logic
│   ├── base.py         # BaseSCMProvider abstract class
│   ├── github/         # GitHub implementation
│   ├── gitea/          # Gitea implementation
│   └── lib/
│       ├── utils.py    # SHA3-256 hashing, base64 decoding
│       └── templates/  # Jinja2 issue rendering
└── server/             # FastAPI REST API
    ├── __init__.py     # App setup, CORS, scheduler
    ├── auth.py         # API key and OAuth2 proxy auth
    └── routes/         # Endpoint handlers (fs, scm, webdav)
```

## Configuration

Key environment variables:

```bash
# Required
ENDPOINT_URL=http://localhost:8000/api/v1

# Ingester authentication
INGESTER_API_KEY=your-key

# SCM authentication
scm_auth_token=your-token
scm_owner=default-owner
scm_base_url=https://gitea.example.com/api/v1  # Required for Gitea

# WebDAV authentication
WEBDAV_URL=https://webdav.example.com
WEBDAV_USERNAME=user
WEBDAV_PASSWORD=pass

# File filtering
EXTENSIONS=md,pdf,doc,docx

# Server settings
SERVER_HOST=127.0.0.1
SERVER_PORT=8001
API_KEY=server-api-key
API_KEY_ENABLED=false
AUTH_TRUST_PROXY_HEADERS=false
```

## Key Patterns

### Batch Management
Documents are grouped into batches by source name. The system reuses existing batches for incremental ingestion.

### Status Checking
Files are hashed and compared against the Ingester database:
- **new:** File does not exist
- **mismatch:** File changed (hash differs)
- **match:** File unchanged (skipped)

### Incremental Sync (SCM)
The run-incremental command tracks the last processed commit SHA to only fetch changed files on subsequent runs.

## Testing

```bash
# Unit tests (required)
uv run pytest tests/unit/

# Specific test file
uv run pytest tests/unit/test_client.py

# Coverage report
uv run pytest --cov-report=html
```

Coverage exclusions (pyproject.toml):
- CLI modules (cli.py)
- App orchestration (app.py)
- Templates
- Test fixtures (conftest.py)

## Documentation

- [README.md](README.md) - User guide, examples, API reference
- [tmp_docs/](tmp_docs/) - Development notes and implementation guides
