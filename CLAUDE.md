# Soliplex Ingester-Agents

Document ingestion agents for loading files from filesystem, WebDAV, SCM
platforms (GitHub, Gitea), and web pages into Soliplex Ingester. Supports
declarative YAML manifests for multi-source ingestion.

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

# SCM agent (github/gitea) -- uses owner/repo notation
si-agent scm run-inventory <platform> <owner>/<repo>
si-agent scm run-incremental <platform> <owner>/<repo>
si-agent scm list-issues <platform> <owner>/<repo>
si-agent scm get-repo <platform> <owner>/<repo>
si-agent scm get-sync-state <platform> <owner>/<repo>
si-agent scm reset-sync <platform> <owner>/<repo>

# WebDAV agent
si-agent webdav run-inventory <path> <source>
si-agent webdav run-from-urls <urls-file> <source>
si-agent webdav check-status <path> <source>
si-agent webdav export-urls <path> <output-file>
si-agent webdav validate-config <path>

# Web agent (no CLI -- available via manifest and REST API only)

# Manifest runner
si-agent manifest run <path>            # Run manifest file or directory
si-agent manifest run <path> --json     # Output results as JSON

# REST API server
si-agent serve
si-agent serve --host 0.0.0.0 --port 8080
si-agent serve --reload
```

## Critical Constraints

- **Test coverage:** 100% branch coverage required for non-excluded code
- **Hashing:** SHA-256 used throughout (filesystem, WebDAV, web, SCM files, SCM issues)
- **Async patterns:** All I/O uses aiohttp/aiofiles with async/await
- **Provider pattern:** SCM uses strategy pattern (base.py + implementations)

## Project Structure

```text
src/soliplex/agents/
├── cli.py              # Main Typer entry point
├── client.py           # Ingester API client (batch, status, ingest, sync state)
├── config.py           # Pydantic Settings, manifest/component models
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
├── webdav/             # WebDAV agent
│   ├── cli.py          # CLI commands
│   ├── app.py          # Directory scanning, ingestion
│   ├── async_client.py # Async WebDAV client wrapper
│   └── state.py        # ETag-based cache state
├── manifest/           # Manifest runner
│   ├── runner.py       # YAML loading, validation, agent dispatch
│   └── cli.py          # CLI commands
├── scm/                # SCM agent
│   ├── cli.py          # CLI commands
│   ├── app.py          # Orchestration logic
│   ├── base.py         # BaseSCMProvider abstract class
│   ├── git_cli.py      # Git CLI decorator (local clone mode)
│   ├── github/         # GitHub implementation
│   ├── gitea/          # Gitea implementation
│   └── lib/
│       ├── utils.py    # SHA-256 hashing, base64 decoding
│       └── templates/  # Jinja2 issue rendering
└── server/             # FastAPI REST API
    ├── __init__.py     # App setup, CORS, cron scheduler
    ├── auth.py         # API key and OAuth2 proxy auth
    ├── locks.py        # Async locks for manifest execution
    └── routes/         # Endpoint handlers (fs, scm, webdav, web, manifest)
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
scm_base_url=https://gitea.example.com/api/v1  # Required for Gitea

# WebDAV authentication
WEBDAV_URL=https://webdav.example.com
WEBDAV_USERNAME=user
WEBDAV_PASSWORD=pass

# File filtering
EXTENSIONS=md,pdf,doc,docx

# Git CLI mode (alternative to API for file operations)
scm_use_git_cli=true
scm_git_cli_timeout=300

# Server settings
SERVER_HOST=127.0.0.1
SERVER_PORT=8001
API_KEY_ENABLED=false

# Manifest scheduling
MANIFEST_DIR=/path/to/manifests
```

## Key Patterns

- **Batch management:** Documents grouped by source name; system reuses existing batches
- **Status checking:** Files hashed (SHA-256) and compared; only new/changed files ingested
- **Incremental sync (SCM):** Tracks last commit SHA to only fetch changes
- **Manifests:** Declarative YAML for multi-source orchestration with delete_stale support

## Testing

```bash
uv run pytest                           # Unit tests (required)
uv run pytest tests/unit/test_client.py # Specific test file
uv run pytest --cov-report=html         # Coverage report
```

Coverage exclusions: `*/cli.py`, `*/app.py`, `*/templates/*`, `*/conftest.py`, `*/server/*`

## Documentation

- [README.md](README.md) - User guide, installation, examples, full configuration reference
- [example-manifests/](example-manifests/) - Sample manifest YAML files
