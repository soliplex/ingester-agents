# Soliplex Ingester-Agents

Document ingestion CLI for collecting files from filesystem, WebDAV, SCM platforms, and web pages into a local download directory. Supports declarative YAML manifests for multi-source ingestion and an optional haiku-rag load step that indexes each source into LanceDB.

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

# SCM agent (github/gitea) - uses owner/repo notation
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

# Manifest runner
si-agent manifest run <path>            # Run manifest file or directory
si-agent manifest run <path> --json     # Output results as JSON
si-agent manifest run <path> --load     # Also run a haiku-rag load per manifest

# REST API server
si-agent serve
si-agent serve --host 0.0.0.0 --port 8080
si-agent serve --reload
```

## Critical Constraints

- **Test coverage:** 100% branch coverage required for non-CLI code
- **Hashing algorithms:**
  - Filesystem/WebDAV/Web: SHA256
  - SCM files: SHA3-256 (see src/soliplex/agents/scm/lib/utils.py)
  - SCM issues: SHA256
- **Async patterns:** All I/O uses aiohttp/aiofiles
- **Provider pattern:** SCM uses strategy pattern (base.py + implementations)

## Project Structure

```text
src/soliplex/agents/
├── cli.py              # Main Typer entry point
├── config.py           # Pydantic Settings, manifest/component models
├── local_state.py      # Local sync state (hashes, commit SHAs, pruning)
├── local_store.py      # Writes documents + .meta.json to DOWNLOAD_DIR
├── common/config.py    # Shared validation utilities
├── fs/                 # Filesystem agent
│   ├── cli.py          # CLI commands
│   └── app.py          # Business logic
├── web/                # Web agent
│   └── app.py          # HTTP fetch and ingest logic
├── webdav/             # WebDAV agent
│   ├── cli.py          # CLI commands
│   └── app.py          # Business logic
├── manifest/           # Manifest runner
│   ├── runner.py       # YAML loading, validation, agent dispatch
│   ├── haiku_loader.py # haiku-rag batch load subprocess
│   └── cli.py          # CLI commands
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
    ├── __init__.py     # App setup, CORS, scheduler, lifespan
    ├── auth.py         # API key and OAuth2 proxy auth
    ├── locks.py        # Per-manifest execution locks
    ├── haiku_queue.py  # Global FIFO queue serializing haiku-rag loads
    └── routes/         # Endpoint handlers (fs, scm, webdav, web, manifest)
```

## Configuration

Key environment variables:

```bash
# Required
DOWNLOAD_DIR=downloads                 # Where fetched documents are written
STATE_DIR=sync_state                   # Local sync state, one SQLite file per source

# haiku-rag loading (optional)
HAIKU_LOAD_ENABLED=true                # Queue a haiku-rag load after each manifest run
LANCEDB_DIR=/var/lib/lancedb           # Base dir for per-source <source>.lancedb
HAIKU_PATH=/etc/haiku                  # Base dir for haiku-rag config files

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
scm_git_repo_base_dir=/path/to/repos

# Server settings
SERVER_HOST=127.0.0.1
SERVER_PORT=8001
API_KEY=server-api-key
API_KEY_ENABLED=false
AUTH_TRUST_PROXY_HEADERS=false

# Manifest scheduling
MANIFEST_DIR=/path/to/manifests       # Directory with manifest .yml files
```

## Key Patterns

### Per-Source Storage

Each manifest maps to one `source`. Its documents live under
`<DOWNLOAD_DIR>/<sanitized-source>/`, with one SQLite sync-state file per
source under `STATE_DIR`. Recorded content hashes drive incremental
ingestion.

### haiku-rag Load Serialization

After each manifest run, a `haiku-ingester` load is queued for the source.
Inside the server one worker drains a global FIFO queue
(`server/haiku_queue.py`) so only one load runs at a time; the CLI runs
loads sequentially. See `manifest/haiku_loader.py`.

### Status Checking

Files are hashed and compared against the local sync state:
- **new:** File does not exist
- **mismatch:** File changed (hash differs)
- **match:** File unchanged (skipped)

### Incremental Sync (SCM)

The run-incremental command tracks the last processed commit SHA in local sync state to only fetch changed files on subsequent runs.

## Testing

```bash
# Unit tests (required)
uv run pytest tests/unit/

# Specific test file
uv run pytest tests/unit/test_manifest_runner.py

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
