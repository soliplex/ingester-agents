# AGENTS.md

Instructions for AI coding agents working with Soliplex Agents.

## Project Overview

Document ingestion agents that collect files from multiple sources (filesystem, WebDAV, web, GitHub, Gitea) and write them to a local download directory, with an optional haiku-rag load step that indexes them into per-source LanceDB databases.

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
├── config.py           # Pydantic settings + manifest models
├── local_state.py      # Local sync state (content hashes, commit SHAs)
├── store.py            # DownloadTarget + DocumentStore (local | s3) -- where documents live
├── sidecar/            # Sidecar kinds (.meta.json), their format and addressing
├── local_store.py      # Writing documents + sidecars through the store
├── retry.py            # Retry helpers
├── common/
│   └── config.py       # File validation utilities
├── fs/                 # Filesystem agent (cli.py + app.py)
├── scm/                # Source control agent
│   ├── cli.py          # CLI commands
│   ├── app.py          # SCM orchestration
│   ├── base.py         # BaseSCMProvider abstract class
│   ├── git_cli.py      # Git CLI decorator (local clone mode)
│   ├── github/         # GitHub provider implementation
│   ├── gitea/          # Gitea provider implementation
│   └── lib/
│       ├── utils.py    # Hashing utilities
│       └── templates/  # Jinja2 templates
├── webdav/             # WebDAV agent (cli.py + app.py + async_client.py)
├── web/                # Web agent (app.py)
├── manifest/           # Declarative multi-source runner
│   ├── cli.py          # `manifest run` / `migrate` / `vacuum` / `reprocess` / reports
│   ├── runner.py       # resolve_manifests / run_manifest dispatch
│   ├── callables.py    # Shared hook plumbing: resolve, inject, invoke, normalize
│   ├── pre_run.py      # pre_run hook: once per run, may SKIP it
│   ├── pre_run_steps.py    # Built-in pre-run steps (notify_webhook, check_free_space)
│   ├── pre_process.py  # pre_process hook: per document, inside write_document
│   ├── pre_processors.py   # Built-in pre-process steps (check_pdf_password, fix_asciidoc)
│   ├── post_process.py # post_process hook: after the haiku load
│   ├── post_processors.py  # Built-in post-process callbacks (vacuum, notify_webhook)
│   ├── webhook.py      # JSON POST helper shared by both notify_webhook steps
│   ├── haiku_loader.py # haiku-rag batch load subprocess
│   └── haiku_maint.py  # haiku-rag migrate/vacuum subprocesses
└── server/             # FastAPI REST API
    ├── __init__.py     # App setup, CORS, scheduler, lifespan
    ├── auth.py         # Authentication
    ├── manifest_queue.py  # Single-worker queue serializing manifest runs
    ├── haiku_queue.py  # Global FIFO queue serializing haiku-rag loads
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
from soliplex.agents.config import settings
from soliplex.agents.manifest import runner

# Incorrect
from soliplex_agents.config import settings
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
uv run pytest tests/unit/test_manifest_runner.py
```

**Requirements:**

- 100% branch coverage for non-excluded code
- Unit tests in `tests/unit/`
- Functional tests in `tests/functional/` (skipped by default)
- Mock external services and subprocesses (GitHub, Gitea, `haiku-ingester`)

**Coverage Exclusions:**

- `*/cli.py` - CLI modules
- `*/app.py` - App orchestration
- `*/templates/*` - Jinja2 templates
- `*/server/*` - Server modules

## Configuration

### Required

```bash
DOWNLOAD_DIR=downloads                       # Where fetched documents are written
                                             # (a key prefix when DOWNLOAD_S3_BUCKET is set)
STATE_DIR=sync_state                         # Local sync state (one SQLite file per source)
```

### haiku-rag Loading

```bash
HAIKU_LOAD_ENABLED=true                       # Queue a haiku-rag load after each manifest run
LANCEDB_DIR=/var/lib/lancedb                  # Interpolated by the haiku-rag config to place
                                              # <source>.lancedb; also the agent's dedupe key
HAIKU_PATH=/etc/haiku                         # Base dir for haiku-rag config files
# HAIKU_LOAD_COMMAND, HAIKU_DEFAULT_CONFIG, HAIKU_LOAD_TIMEOUT, HAIKU_LOAD_CWD also available
# HAIKU_MAINTENANCE_COMMAND, HAIKU_MAINTENANCE_TIMEOUT for `manifest migrate` / `manifest vacuum`
# HAIKU_OUTPUT_CHUNK_BYTES, HAIKU_OUTPUT_FLUSH_SECONDS, HAIKU_OUTPUT_MAX_BYTES shape how the
# subprocess output is logged (in parts, inside the run's span)
# HAIKU_TRACE_WRAPPER=true runs haiku commands via `python -m soliplex.agents.traced_run`,
# so haiku-ingester's spans join the agent's trace (TRACEPARENT is always exported)
```

Tracing: the server sends traces and logs to Logfire whenever a token is set
(`LOGFIRE_TOKEN` / `/run/secrets/logfire_token`); the CLI only with
`si-agent --otel <command>`. Spans are created through `soliplex.agents.telemetry`
(OpenTelemetry API, so they're no-ops when Logfire isn't configured); tests
capture them with the `spans` fixture in `tests/unit/conftest.py`.

The haiku-rag config file needs its own set on top of these — haiku expands
`${VAR}` eagerly when the file is read, so one that is unset **or empty** fails
the load with `MissingEnvVarError` before any document is touched. The example
configs need `STATE_DIR`, `QA_MODEL`, `QA_BASE_URL`, `OLLAMA_BASE_URL`,
`DOCLING1_BASE_URL`, `DOCLING2_BASE_URL` and `EMBEDDINGS_BASE_URL`, plus
`S3_BUCKET`, `S3_REGION` and `DOWNLOAD_S3_BUCKET` in S3 mode. `SOURCE`,
`DOWNLOAD_DIR` and `DOWNLOAD_URI` are injected per load and must not be set by
hand. Each file in `example-haiku-configs/` lists its own set in a header
comment; the README table is
["Variables the haiku-rag config needs"](README.md#variables-the-haiku-rag-config-needs).

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
├── manifest
│   ├── run <path> [--json] [--load/--no-load]   # Run manifest(s); optionally haiku-rag load
│   ├── migrate [path|all] [--json] [--timeout] [--dry-run]  # haiku-rag DB migrations
│   └── vacuum [path|all] [--json] [--timeout] [--dry-run]   # haiku-rag DB compaction
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

### Per-Source Storage

Each manifest maps to one `source`. All of a source's documents live under
`<DOWNLOAD_DIR>/<sanitized-source>/`, with one SQLite sync-state file per
source under `STATE_DIR`. Content hashes recorded in sync state enable
incremental ingestion (only new/changed files are written).

Where that is depends on the resolved **download target** (`store.py`): the
local filesystem, or an S3 bucket when `DOWNLOAD_S3_BUCKET` is set, or
whatever a manifest's `config.download_store` overrides it to. Nothing outside
`store.py` branches on the backend -- callers pass source-relative keys and the
target owns every layer of prefixing.

A configured bucket may be a bare name or an `s3://bucket/prefix` URI; both
go through `split_bucket()`, and a prefix there becomes the outermost layer,
ahead of `DOWNLOAD_DIR`. `DownloadTarget.bucket` therefore holds whatever was
configured -- use `bucket_name` when an API wants the bucket itself.

Two consequences worth knowing before changing anything here:

- The state filename is qualified by target (a digest suffix; a local default
  keeps the historical unqualified name). Swapping a source's store therefore
  opens fresh state and re-fetches, rather than reporting everything unchanged
  and writing nothing.
- The reconcile sweep deletes every object under a source that is not in
  `expected`, and `expected` is derived from the registered sidecar kinds. Add
  a sidecar kind by registering a `SidecarKind` in `sidecar/`; do not hard-code
  a suffix anywhere, or the sweep will delete it.

### haiku-rag Load Serialization

After each manifest run (scheduler, startup, or CLI), a `haiku-ingester`
load is queued for the source. Inside the server a single worker drains a
global FIFO queue (`server/haiku_queue.py`), so only one load runs at a
time; the CLI runs loads sequentially for the same effect. The subprocess
inherits the parent environment plus the vars from the run's `LoadContext`
(`manifest/context.py`): `SOURCE` (sanitized download-folder name),
`DOWNLOAD_DIR`, and `DOWNLOAD_URI` (the resolved base URI, set in both storage
modes so one config form works either way). See `manifest/haiku_loader.py`.

**The database comes from the haiku-rag config, not the command line.** The
default `HAIKU_LOAD_COMMAND` passes `--config` and no `--db`, so
`lancedb.databases` in that file places the database and must interpolate
`${SOURCE}` to get one per source:

```yaml
lancedb:
  databases:
    db: ${LANCEDB_DIR:-/lancedb}/${SOURCE}.lancedb
```

A config with one entry and no `${SOURCE}`, or with no `lancedb.databases` at
all, merges every source into a single store *without erroring*; two or more
entries is the only case haiku-ingester refuses. `resolve_db_path` still
computes `${LANCEDB_DIR}/<slug>.lancedb` for the log line, the run report and
the maintenance dedupe key, so `LANCEDB_DIR` stays required — and note that
`slugify_source` (whitespace to hyphens) and `sanitize_source` (the `${SOURCE}`
the config sees) disagree for a source containing whitespace. See
`example-haiku-configs/` for working configs, and the README's "Where the
database comes from".

### haiku-rag Database Maintenance

`si-agent manifest {migrate,vacuum} [path|all]` runs
`haiku-rag --config=<cfg> <verb>` per manifest source, reusing the load's
config/DB/env resolution — including the config placing the database (see
`manifest/haiku_maint.py`). `all`
(the default) means every manifest in `MANIFEST_DIR`. Operations run
sequentially, manifests sharing a database are deduplicated, no post-process
callbacks fire, and `--dry-run` prints the command lines without spawning.
`post_processors.vacuum` delegates to the same `run_verb` code path but
raises on failure, because the post-process chain stops on the first error.

### Manifest Hooks

A manifest has three optional, ordered hooks (README "Manifest hooks"):

- `pre_run` (`manifest/pre_run.py`) -- once, before any component, inside
  `runner.run_manifest`. SKIP returns a result with `skipped` set; callers
  (`runner.run_manifests`, `server/manifest_queue.run_manifest_now`) must not
  load a skipped run.
- `pre_process` (`manifest/pre_process.py`) -- per new or changed document,
  inside `local_store.write_document`, the one call every agent makes. The run
  installs a `PreProcessRun` in a ContextVar (`pre_process.activate`); outside
  a manifest run there is none and writes are unchanged. SKIP writes nothing
  and deletes any stored version; MODIFIED stores the new bytes.
- `post_process` (`manifest/post_process.py`) -- after the haiku load, with the
  load's `HaikuRun` (`ingester`) and the queuing run's result (`run_result`).

All three resolve dotted paths and inject keywords through
`manifest/callables.py`. Outcomes are audited in the source's state DB
(`pre_run`, `pre_process`, `pre_process_documents` tables in
`local_state.py`); the `pre_process*` rows follow the document's `files` row.

### Incremental Sync (SCM)

Commit-based tracking for efficient syncing:

1. Get last processed commit SHA from local sync state
2. Fetch commits since that SHA
3. Extract changed file paths
4. Download only modified files
5. Store new commit SHA in local sync state

## File Organization

When adding features:

- Agent logic goes in `{agent}/app.py`
- CLI commands go in `{agent}/cli.py`
- API endpoints go in `server/routes/{agent}.py`
- Tests go in `tests/unit/test_{module}.py`

## Critical Constraints

- Do not mix hashing algorithms (SHA256 vs SHA3-256)
- Always use async/await for I/O operations
- Manifest IDs must be unique when running a directory of manifests
- Only one haiku-rag load runs at a time (capacity constraint)
- WebDAV requires SSL verification by default
- SCM providers must implement `BaseSCMProvider` interface
- Content checks and rewrites belong in pre-process steps, not in agents; the
  only pre-process call site is `local_store.write_document`
- A pre-process SKIP must still let the agent record the state row, or the
  document is re-fetched (and re-skipped) on every full run
- Pre-process steps receive a spooled local file and never open stores, so
  they behave the same on the local and S3 backends
- A run skipped by `pre_run` must not queue a haiku load
- Notification steps should use `on_error: continue`, so a webhook outage
  cannot block ingestion

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
