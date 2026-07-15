# Soliplex Agents

[![CI](https://github.com/soliplex/ingester-agents/actions/workflows/soliplex.yaml/badge.svg)](https://github.com/soliplex/ingester-agents/actions/workflows/soliplex.yaml)

Agents for collecting documents from multiple sources — local filesystems, WebDAV servers, web pages, and source code management platforms (GitHub, Gitea) — and writing them to a local download directory for downstream processing. Each document is written with a `.meta.json` sidecar capturing its MIME type and other metadata, and synchronization state is tracked locally so subsequent runs only fetch what changed.

MIME types are detected from file **content** (via [puremagic](https://pypi.org/project/puremagic/)) rather than trusting the filename extension, and the stored file is given the extension implied by its detected type. This means files with no extension (or the wrong one) are classified correctly and filtered consistently across every source. See [File Typing and Filtering](#file-typing-and-filtering).

## Features

- **Filesystem Agent (`fs`)**: Ingest documents from local directories
  - Recursive directory scanning
  - Content-based MIME type detection (extension-less files supported)
  - Configuration validation
  - Status checking to avoid re-ingesting unchanged files

- **WebDAV Agent (`webdav`)**: Ingest documents from WebDAV servers
  - Support for any WebDAV-compliant server (Nextcloud, ownCloud, SharePoint, etc.)
  - Recursive directory scanning
  - MIME type from the server `Content-Type` header, falling back to content sniffing
  - Authentication support (username/password)
  - Status checking to avoid re-ingesting unchanged files
  - URL export for reviewing discovered files before ingestion
  - URL-based ingestion from a curated file list with per-URL error tracking
  - Skip hash check option for faster ingestion when re-downloading is acceptable

- **Web Agent (`web`)**: Ingest web pages via HTTP
  - Fetch and ingest HTML content from URLs
  - URL list support (inline, file, or single URL)

- **SCM Agent (`scm`)**: Ingest files and issues from Git repositories
  - Support for GitHub and Gitea platforms
  - Content-based file type filtering (extension-less files supported)
  - Issue ingestion with comments (rendered as Markdown)
  - Status checking to avoid re-ingesting unchanged files

- **Manifest Runner (`manifest`)**: Declarative multi-source ingestion
  - YAML-based manifest files defining ingestion components
  - Supports all agent types (fs, scm, webdav, web) in a single manifest
  - Shared configuration (metadata, extensions, haiku-rag load config)
  - Stale document removal (`delete_stale`) across all components in a manifest
  - Cron-based scheduling via the REST API server
  - Per-component credential and extension overrides
  - Directory-level execution for running multiple manifests at once

- **haiku-rag Loading**: Index downloaded documents into LanceDB
  - Runs `haiku-ingester run-batch` after each manifest run
  - One per-source `.lancedb` database, configurable command and config file
  - Globally serialized — only one load runs at a time

- **REST API Server**: Run agents as a web service
  - FastAPI-based HTTP endpoints for all operations
  - Multiple authentication methods (API key, OAuth2 proxy)
  - Interactive API documentation with Swagger UI
  - Health check endpoint for monitoring
  - Container-ready with Docker support

## Installation

**Requirements:**
- Python 3.13 or higher

Documents are written to the local filesystem (`DOWNLOAD_DIR`). Indexing
into a vector store is handled by an optional haiku-rag load step (see
[haiku-rag Loading](#haiku-rag-loading)), which runs `haiku-ingester`
against the downloaded files.

### Using uv (Recommended)

```bash
uv add soliplex.agents
```

### Using pip

```bash
pip install soliplex.agents
```

### From Source

```bash
git clone <repository-url>
cd ingester-agents
uv sync
```

## Configuration

The agents use environment variables for configuration. Create a `.env` file or export these variables:

### Required Configuration

Agents write fetched documents to the local filesystem.

```bash
# Directory where downloaded documents are written. Each run stores files
# under <DOWNLOAD_DIR>/<source>/, preserving the source directory structure.
# Every document is accompanied by a <filename>.meta.json sidecar containing
# its MIME type and any other available metadata.
DOWNLOAD_DIR=downloads

# Directory for local synchronization state (content hashes + SCM commit
# markers), one SQLite file per source.
STATE_DIR=sync_state
```

### SCM Configuration

The agents use unified authentication settings that work across all SCM providers (GitHub, Gitea, etc.):

```bash
# SCM authentication token (GitHub personal access token or Gitea API token)
scm_auth_token=your_scm_token_here

# SCM base URL (required for Gitea, optional for GitHub)
# For Gitea: Full API URL including /api/v1
# For GitHub: Defaults to https://api.github.com if not specified
scm_base_url=https://your-gitea-instance.com/api/v1
```

**Examples:**

For GitHub:

```bash
export scm_auth_token=ghp_YourGitHubToken
# scm_base_url not needed for public GitHub
```

For Gitea:

```bash
export scm_auth_token=your_gitea_token
export scm_base_url=https://gitea.example.com/api/v1
```

### WebDAV Configuration

```bash
# WebDAV server URL
WEBDAV_URL=https://webdav.example.com

# WebDAV authentication
WEBDAV_USERNAME=your-username
WEBDAV_PASSWORD=your-password

# Disable TLS certificate verification (default: true)
SSL_VERIFY=true
```

All WebDAV credentials can also be provided via command-line options (`--webdav-url`, `--webdav-username`, `--webdav-password`), which override the environment variables.

### Optional Configuration

```bash
# File extensions to include (default: md,pdf,doc,docx)
EXTENSIONS=md,pdf,doc,docx

# Logging level (default: INFO)
LOG_LEVEL=INFO

# API Server Configuration
SERVER_HOST=127.0.0.1
SERVER_PORT=8001

# Authentication (for API server)
API_KEY=your-api-key
API_KEY_ENABLED=false
AUTH_TRUST_PROXY_HEADERS=false

# Manifest scheduling (requires SCHEDULER_ENABLED=true)
MANIFEST_DIR=/path/to/manifests
# SCHEDULER_RECONCILE_CRON="*/1 * * * *"   # how often the dir is rescanned

# haiku-rag loading (runs `haiku-ingester run-batch` after each manifest run)
HAIKU_LOAD_ENABLED=false
LANCEDB_DIR=/var/lib/lancedb          # holds <source>.lancedb per source
HAIKU_PATH=/etc/haiku                  # base dir for haiku-rag config files
# HAIKU_DEFAULT_CONFIG=haiku.rag.default.yaml   # config filename under HAIKU_PATH
# HAIKU_LOAD_COMMAND=haiku-ingester --config={haiku_cfg} run-batch --db={db}
# HAIKU_LOAD_TIMEOUT=1800
# HAIKU_LOAD_CWD=/var/lib/ingester     # subprocess working dir (default: inherit)

# S3-compatible storage (for urls_file references using s3:// URLs)
S3_ENDPOINT_URL=https://minio.example.com:9000
```

See [haiku-rag Loading](#haiku-rag-loading) for what these settings do.

### Git CLI Mode

For large repositories or rate-limited APIs, you can use the git command-line for file synchronization instead of API calls. This clones the repository locally and reads files from the filesystem.

```bash
# Enable git CLI mode
scm_use_git_cli=true

# Optional: Custom directory for cloned repos (default: system temp directory)
scm_git_repo_base_dir=/var/lib/soliplex/repos

# Optional: Timeout for git operations in seconds (default: 300)
scm_git_cli_timeout=600
```

**How it works:**

1. **First sync**: Clones the repository to a local temp directory (shallow clone, single branch)
2. **Subsequent syncs**: Pulls latest changes using `git pull --ff-only`
3. **Pull failure**: If pull fails, deletes the local clone and re-clones
4. **After sync**: Runs `git clean -fd` to remove untracked files

**Notes:**

- Issues are still fetched via API (git doesn't provide issue data)
- Requires git to be installed in the runtime environment
- The Docker image includes git by default
- All credentials are masked in log output for security

**Security:** Git CLI mode uses strict input sanitization to prevent command injection. Only alphanumeric characters, dashes, underscores, dots, and forward slashes are allowed in repository names and paths.

## Usage

The CLI tool `si-agent` provides six main modes of operation:
- **`fs`**: Filesystem agent for ingesting local documents
- **`web`**: Web agent for ingesting HTML pages from URLs
- **`scm`**: SCM agent for ingesting from Git repositories
- **`webdav`**: WebDAV agent for ingesting documents from WebDAV servers
- **`manifest`**: Manifest runner for declarative multi-source ingestion
- **`serve`**: REST API server exposing agent functionality via HTTP

### Filesystem Agent

#### Quick Start

Ingest documents directly from a directory:

```bash
si-agent fs run-inventory /path/to/documents my-source-name
```

That's it! The tool automatically:
1. Scans the directory
2. Builds the configuration
3. Validates files
4. Ingests documents

#### Traditional Workflow (with inventory.json)

If you want to review or modify the inventory before ingestion:

**1. Build Configuration (Optional)**

Scan a directory and create an inventory file:

```bash
si-agent fs build-config /path/to/documents
```

This creates an `inventory.json` file containing metadata for all discovered files. You can edit this file to add custom metadata or exclude specific files.

**2. Validate Configuration**

Check if files are supported (accepts file OR directory):

```bash
# Validate existing inventory file
si-agent fs validate-config /path/to/inventory.json

# Or validate directory directly (builds config on-the-fly)
si-agent fs validate-config /path/to/documents
```

**3. Check Status**

See which files need to be ingested (accepts file OR directory):

```bash
# Using inventory file
si-agent fs check-status /path/to/inventory.json my-source-name

# Or using directory directly
si-agent fs check-status /path/to/documents my-source-name
```

Add `--detail` flag to see the full list of files:

```bash
si-agent fs check-status /path/to/documents my-source-name --detail
```

The status check compares file hashes against the local sync state:
- **new**: File doesn't exist in local state
- **mismatch**: File exists but content has changed
- **match**: File is unchanged (will be skipped during the run)

**4. Load Inventory**

Ingest documents (accepts file OR directory):

```bash
# From inventory file
si-agent fs run-inventory /path/to/inventory.json my-source-name

# Or from directory directly (recommended!)
si-agent fs run-inventory /path/to/documents my-source-name
```

**Advanced options:**

```bash
# Process a subset of files (e.g., files 10-50)
si-agent fs run-inventory inventory.json my-source --start 10 --end 50
```

### SCM Agent

#### 1. List Issues

List all issues from a repository:

```bash
# GitHub
si-agent scm list-issues github myorg/my-repo

# Gitea
si-agent scm list-issues gitea admin/my-repo
```

#### 2. Get Repository Files

List files in a repository:

```bash
# GitHub
si-agent scm get-repo github myorg/my-repo

# Gitea
si-agent scm get-repo gitea admin/my-repo
```

#### 3. Load Inventory

Ingest **both files and issues** from a repository. Issues are rendered as Markdown documents with their comments.

```bash
# GitHub
si-agent scm run-inventory github myorg/my-repo

# Gitea
si-agent scm run-inventory gitea admin/my-repo
```

Files and issues are written under `<DOWNLOAD_DIR>/<source>/`. Issues are
saved as Markdown (`.md`) documents.

#### 4. Incremental Sync

Run commit-based incremental synchronization. Only processes files that changed since the last sync, significantly reducing API calls and bandwidth usage.

```bash
# First run performs full sync and establishes sync state
si-agent scm run-incremental gitea admin/my-repo

# Subsequent runs only process changes since last sync
si-agent scm run-incremental gitea admin/my-repo --branch main
```

**Output JSON format:**

```bash
si-agent scm run-incremental gitea admin/my-repo --do-json
```

#### 5. Sync State Management

View and manage sync state for repositories:

```bash
# View current sync state
si-agent scm get-sync-state gitea admin/my-repo

# Reset sync state (forces full sync on next run)
si-agent scm reset-sync gitea admin/my-repo
```

### WebDAV Agent

The WebDAV agent allows you to ingest documents directly from WebDAV servers (like Nextcloud, ownCloud, SharePoint, etc.).

#### Quick Start

Ingest documents directly from a WebDAV directory:

```bash
# Set up environment
export WEBDAV_URL=https://webdav.example.com
export WEBDAV_USERNAME=your-username
export WEBDAV_PASSWORD=your-password

# Ingest documents from WebDAV path
si-agent webdav run-inventory /documents my-source-name
```

That's it! The tool automatically:
1. Connects to the WebDAV server
2. Scans the directory
3. Builds the configuration
4. Validates files
5. Ingests documents

#### Commands

**1. Export URLs**

Scan a WebDAV directory and export discovered file URLs to a file for review. This uses only directory listing (PROPFIND) and does not download file content:

```bash
si-agent webdav export-urls /documents urls.txt \
  --webdav-url https://webdav.example.com \
  --webdav-username user \
  --webdav-password pass
```

The output file contains one absolute WebDAV path per line:

```text
/documents/report.md
/documents/sub/readme.pdf
/documents/notes.docx
```

Only files matching the configured `EXTENSIONS` filter are included.

**2. Validate Configuration**

Check if files are supported (downloads files to compute hashes):

```bash
# Validate WebDAV directory directly
si-agent webdav validate-config /documents \
  --webdav-url https://webdav.example.com \
  --webdav-username user \
  --webdav-password pass
```

**3. Check Status**

See which files need to be ingested:

```bash
si-agent webdav check-status /documents my-source-name \
  --webdav-url https://webdav.example.com \
  --webdav-username user \
  --webdav-password pass
```

Add `--detail` flag to see the full list of files.

**4. Load Inventory**

Ingest documents from a WebDAV directory:

```bash
si-agent webdav run-inventory /documents my-source-name
```

**Advanced options:**

```bash
si-agent webdav run-inventory /documents my-source \
  --webdav-url https://webdav.example.com \
  --webdav-username user \
  --webdav-password pass
```

**5. Run from URL List**

Ingest specific files from a URL list file instead of scanning an entire directory. This is useful when you want to ingest only a curated subset of files:

```bash
si-agent webdav run-from-urls urls.txt my-source-name
```

Each URL in the file is processed independently. If a file fails to download, the error is recorded and processing continues with the remaining URLs. Results are written to a JSON file named `<input-file>.results.<timestamp>.json`:

```json
[
  {"url": "/documents/report.md", "status": "success"},
  {"url": "/documents/broken.pdf", "status": "error", "error_message": "404 Not Found"}
]
```

Use `--skip-hash-check` to skip downloading files for hash comparison, which avoids downloading each file twice (once for hashing, once for ingestion):

```bash
si-agent webdav run-from-urls urls.txt my-source-name --skip-hash-check
```

### Manifest Runner

The manifest runner executes declarative YAML manifests that define multi-source ingestion jobs. A single manifest can combine filesystem, WebDAV, SCM, and web components under a shared source and configuration.

#### Quick Start

Run a single manifest file:

```bash
si-agent manifest run /path/to/manifest.yml
```

Run all manifests in a directory:

```bash
si-agent manifest run /path/to/manifests/
```

Output results as JSON:

```bash
si-agent manifest run /path/to/manifest.yml --json
```

#### Manifest YAML Format

A manifest file defines the ingestion source, optional shared configuration, and one or more components:

```yaml
id: my-ingestion
name: My Document Ingestion
source: my-source-name
schedule:
  cron: "0 0 * * *"
config:
  metadata:
    project: my-project
  extensions:
    - md
    - pdf
  delete_stale: true
components:
  - name: local-docs
    type: fs
    path: /path/to/documents

  - name: web-pages
    type: web
    urls:
      - https://example.com/page1
      - https://example.com/page2

  - name: repo-docs
    type: scm
    platform: github
    owner: myorg
    repo: my-repo
    incremental: true

  - name: shared-drive
    type: webdav
    url: https://webdav.example.com
    path: /documents
```

#### Manifest Fields

Top-level fields:

- **id** (required): Unique identifier for the manifest. Must be unique across all manifests when running from a directory.
- **name** (required): Human-readable name for display and logging.
- **source** (required): Source name; also the per-source folder name under `DOWNLOAD_DIR` (sanitized for filesystem safety).
- **schedule**: Optional cron schedule for automated execution via the REST API server.
  - **cron**: Cron expression (e.g., `"0 0 * * *"` for daily at midnight).
- **config**: Optional shared configuration applied to all components.
  - **metadata**: Key-value pairs attached to all ingested documents.
  - **extensions**: File extensions to include (overrides the global `EXTENSIONS` setting).
  - **delete_stale**: Remove locally-stored documents that no longer appear in any component (default: false). See [Stale Document Removal](#stale-document-removal) below.
  - **haiku_config**: Override the haiku-rag config file used when loading this manifest's source. Absolute paths are used as-is; relative values resolve under `HAIKU_PATH`. Defaults to `${HAIKU_PATH}/haiku.rag.default.yaml`. See [haiku-rag Loading](#haiku-rag-loading).
- **components** (required): List of ingestion components (see below).

#### Component Types

**Filesystem (`fs`):**

- **name** (required): Component name (must be unique within the manifest).
- **path** (required): Path to a local directory or inventory file.
- **extensions**: Override extensions for this component.
- **metadata**: Additional metadata merged with config-level metadata.

**Web (`web`):**

- **name** (required): Component name.
- **url**: Single URL to fetch.
- **urls**: List of URLs to fetch.
- **urls_file**: Path to a file containing URLs (one per line). Supports local paths, `s3://bucket/key` URLs, and `http(s)://` WebDAV URLs.
- Exactly one of `url`, `urls`, or `urls_file` must be specified.
- **extensions**: Override extensions for this component.
- **metadata**: Additional metadata merged with config-level metadata.

**SCM (`scm`):**

- **name** (required): Component name.
- **platform** (required): `github` or `gitea`.
- **owner** (required): Repository owner or organization.
- **repo** (required): Repository name.
- **incremental**: Use commit-based incremental sync (default: false).
- **branch**: Branch to sync (default: `main`).
- **content_filter**: What to ingest: `all`, `files`, or `issues` (default: `all`).
- **base_url**: Override SCM base URL (uses `scm_base_url` env var if not set).
- **auth_token**: Override auth token name (resolved via Docker secrets or env vars).
- **extensions**: Override extensions for this component.
- **metadata**: Additional metadata merged with config-level metadata.

**WebDAV (`webdav`):**

- **name** (required): Component name.
- **url** (required): WebDAV server URL.
- **path**: WebDAV directory path to scan recursively.
- **urls**: List of specific WebDAV file paths to ingest.
- **urls_file**: Path to a file containing WebDAV URLs (one per line). Supports local paths, `s3://bucket/key` URLs, and `http(s)://` WebDAV URLs (fetched using the same WebDAV credentials).
- Exactly one of `path`, `urls`, or `urls_file` must be specified.
- **username**: Override WebDAV username (resolved via Docker secrets or env vars).
- **password**: Override WebDAV password (resolved via Docker secrets or env vars).
- **extensions**: Override extensions for this component.
- **metadata**: Additional metadata merged with config-level metadata.

#### Configuration Precedence

Settings are resolved in the following order (highest priority first):

1. Component-level settings (e.g., `extensions` on a component)
2. Manifest config-level settings (e.g., `config.extensions`)
3. Global environment settings (e.g., `EXTENSIONS` env var)

For metadata, config-level and component-level values are merged, with component values taking precedence for duplicate keys.

#### Stale Document Removal

When `delete_stale: true` is set in a manifest's `config` block, the runner removes locally-stored documents that no longer appear in any of the manifest's components. This keeps the download directory in sync with the actual source data.

**How it works:**

1. All components execute sequentially, collecting every discovered URI and its hash.
2. After **all** components complete, the consolidated URI set is *reconciled against the actual download folder* for the source (all components in a manifest share one source, hence one download folder and state DB). Reconciliation is two-pass:
   - **State pass:** any document tracked in local state whose URI is **not** in the consolidated set has its file, `.meta.json` sidecar, and state entry deleted.
   - **Disk sweep:** the source download folder is then walked, and any file (plus its sidecar) that doesn't back a surviving URI is deleted — catching *orphans that were never tracked in state* (e.g. files left behind by an earlier run), not just files with a state row.

**WebDAV 404 handling:**

- A WebDAV file that returns **404 (Not Found)** during download is treated as a definitive removal, not a transient error. When `delete_stale` is on, its local copy is deleted (via the reconcile above) even if it still appears in a stale listing. A 404 does **not** block the reconcile.
- This is distinct from *transient* errors (timeouts, 5xx) — see Safety below.

**Safety:**

- If **any** component raises, hits an unknown type, or reports a **transient per-file error** (timeout / 5xx), `delete_stale` is **skipped entirely** for that manifest run. This prevents accidental deletions when the URI set may be incomplete. (A 404 is a removal signal, not a transient error, so it does not trigger this skip.)
- Components that succeed still have their documents ingested normally — only the stale deletion step is skipped.

**Example:**

```yaml
id: synced-docs
name: Synced Documentation
source: docs-source
config:
  delete_stale: true
components:
  - name: local-docs
    type: fs
    path: /data/docs
  - name: shared-drive
    type: webdav
    url: https://webdav.example.com
    path: /shared/docs
```

If a file is removed from `/data/docs` or from the WebDAV server (dropped from the listing, or returning 404 on fetch), the next manifest run detects that its URI is no longer present and deletes it — and its sidecar — from the download directory.

**Note:** SCM components using `incremental: true` only return files changed since the last sync, not the full file listing. When `delete_stale` is enabled with incremental SCM components, the stale detection may not have complete URI coverage for those components. Consider using full inventory mode (`incremental: false`) when `delete_stale` is needed with SCM sources.

#### Scheduling

When the REST API server is started with `SCHEDULER_ENABLED=true` and `MANIFEST_DIR` is set, manifests with a `schedule` block are automatically registered as cron jobs:

```bash
export SCHEDULER_ENABLED=true
export MANIFEST_DIR=/path/to/manifests
si-agent serve
```

The server loads all manifests from the directory at startup, validates that all manifest IDs are unique, and then:

- **Manifests with a `schedule`** are registered and run when their cron
  expression is due.
- **Manifests without a `schedule`** are run once when first seen (a
  fire-and-forget task), then never again unless triggered via the API.

**Hot-reloading schedules:**

The manifest directory is rescanned on a fixed interval (every minute by
default; configurable via `SCHEDULER_RECONCILE_CRON`), so changes take
effect **without restarting the server**:

- **Added files** are picked up on the next scan — new schedules register,
  and new unscheduled manifests run once.
- **Removed files** are unregistered and stop firing.
- **Edited `schedule` blocks** are re-read; the manifest is rescheduled to
  its new cron expression (the next fire is computed from the change time,
  not backfilled). Adding a `schedule` to a previously unscheduled manifest
  starts scheduling it; removing the `schedule` stops it.

A manifest that is invalid or introduces a duplicate id mid-edit is skipped
for that scan (logged) and retried on the next one, so a bad save never
takes down the scheduler.

**Execution behavior:**

- **At most one manifest runs at a time.** A process-global lock serializes
  all manifest execution (scheduled runs, startup runs, and API-triggered
  runs), so different manifests never run concurrently. This bounds resource
  use, since a single manifest can already fan out across its components.
- **Overlapping schedules are dropped, not queued.** If a cron fires while
  any manifest is still running, that fire is skipped (logged) instead of
  being queued behind the in-progress run. This prevents pile-ups from
  frequent schedules or long-running loads. Startup runs and explicit API
  calls instead wait for the lock rather than being skipped.
- **Single process only.** Because the cron state and locks are held in
  memory, scheduling relies on the server running as a single worker (see
  [Starting the Server](#starting-the-server)). If you run multiple server
  instances, enable `SCHEDULER_ENABLED` on only one of them.

#### haiku-rag Loading

When `HAIKU_LOAD_ENABLED=true`, a haiku-rag load is queued after **each**
manifest run (scheduled, startup, or CLI). The load indexes the documents
that the manifest just wrote to `${DOWNLOAD_DIR}/<source>/` into a
per-source LanceDB database. The default command is:

```bash
haiku-ingester --config=${HAIKU_CFG} run-batch --db=${LANCEDB_DIR}/<source>.lancedb
```

- **One load at a time.** Inside the server, loads are drained from a
  single global FIFO queue by one worker, so only one `haiku-ingester`
  process runs at any moment (a capacity constraint). The CLI achieves the
  same by running loads sequentially after each manifest.
- **Command** is fully configurable via `HAIKU_LOAD_COMMAND`. Supported
  placeholders: `{haiku_cfg}`, `{db}`, `{source}`, `{lancedb_dir}`,
  `{haiku_path}`. The template is tokenized before substitution, so values
  containing spaces cannot inject extra arguments.
- **Config file** resolves from the manifest's `config.haiku_config`
  (absolute path used as-is; relative resolved under `HAIKU_PATH`),
  falling back to `${HAIKU_PATH}/${HAIKU_DEFAULT_CONFIG}`
  (`haiku.rag.default.yaml`).
- **Database** path is `${LANCEDB_DIR}/<slug>.lancedb`, where `<slug>` is
  the source with whitespace replaced by hyphens
  (`composite source` → `composite-source.lancedb`).
- **Environment.** The subprocess inherits the server's environment plus
  two injected variables:
  - `SOURCE` — the sanitized download-folder name, so a haiku-rag config
    using `root: ${DOWNLOAD_DIR}/${SOURCE}` resolves to the ingested
    documents.
  - `DOWNLOAD_DIR` — `settings.download_dir`, so the path above resolves
    even when it was left at its default.

  Any other `${VAR}` interpolated by the haiku-rag config (e.g.
  `OLLAMA_BASE_URL`, `DOCLING1_BASE_URL`, `DOCLING2_BASE_URL`,
  `EMBEDDINGS_BASE_URL`) must be present in the server's environment.

```bash
export HAIKU_LOAD_ENABLED=true
export LANCEDB_DIR=/var/lib/lancedb
export HAIKU_PATH=/etc/haiku
export SCHEDULER_ENABLED=true
export MANIFEST_DIR=/path/to/manifests
si-agent serve
```

The CLI honors the same `HAIKU_LOAD_ENABLED` default; override per
invocation with `si-agent manifest run <path> --load` / `--no-load`.

**Note:** All commands support WebDAV credentials via environment variables (`WEBDAV_URL`, `WEBDAV_USERNAME`, `WEBDAV_PASSWORD`) or command-line options (`--webdav-url`, `--webdav-username`, `--webdav-password`).

**Git Bash on Windows:** If using Git Bash on Windows, use double slashes for WebDAV paths to prevent path conversion (e.g., `//documents` instead of `/documents`).

## How It Works

### Document Ingestion Flow

1. **Discovery**: Files are discovered from the source (filesystem, WebDAV, SCM, or web)
2. **Hashing**: Each file's hash is calculated
   - Filesystem/WebDAV/Web sources: SHA256 hash
   - SCM sources: SHA3-256 hash for files, SHA256 for issues
3. **Status Check**: The system checks which files are new or changed against the local sync state, so only new or changed files are processed
4. **Write**: Each file is written to `<DOWNLOAD_DIR>/<source>/<source-relative-path>`, with a `<filename>.meta.json` sidecar containing its MIME type and other metadata. The stored filename is given the extension implied by its detected MIME type (added when missing, replaced when it mismatches, left alone when already correct) — see [File Typing and Filtering](#file-typing-and-filtering)
5. **State Update**: Content hashes (and, for SCM, the latest commit SHA) are recorded in local state
6. **Stale Removal** (optional): When `delete_stale` is enabled, the download folder is reconciled against the source — documents no longer present (dropped from the listing, or 404 on fetch) are deleted, along with untracked orphan files (see [Stale Document Removal](#stale-document-removal))
7. **haiku-rag Load** (optional): When `HAIKU_LOAD_ENABLED` is set, the downloaded documents are indexed into a per-source LanceDB database via `haiku-ingester` (see [haiku-rag Loading](#haiku-rag-loading))

### Incremental Sync (SCM Agent)

The `run-incremental` command uses commit-based tracking for efficient synchronization:

1. **Sync State Check**: Retrieves last processed commit SHA from local state
2. **Commit Enumeration**: Fetches only commits since the last sync
3. **Change Detection**: Extracts changed and removed file paths from commits
4. **Selective Fetch**: Downloads only files that were modified
5. **Write**: Writes changed files to `DOWNLOAD_DIR` and deletes removed ones
6. **State Update**: Stores the latest commit SHA locally for subsequent syncs

This approach reduces API calls and bandwidth by 80-95% compared to full repository scans. On first run (or after reset), a full sync is performed to establish the baseline.

### File Typing and Filtering

MIME types are determined from file **content**, not the filename. Detection
resolves in this order:

1. **Explicit `Content-Type` header** (WebDAV only — the GET response header,
   or the PROPFIND `getcontenttype` property), unless it is generic
   (`application/octet-stream`).
2. **Content sniffing** via [puremagic](https://pypi.org/project/puremagic/),
   which recognises binary formats (PDF, PNG, Office documents, …) by their
   magic bytes.
3. **Filename extension** via the standard library, plus overrides for Office
   and text formats.
4. **Plain-text default** (filesystem and git only): an extension-less file
   whose bytes look like UTF-8 text is treated as `text/plain`. WebDAV does
   **not** apply this default — it relies on the server-provided type.
5. Otherwise `application/octet-stream`.

Once typed, the document is written with the extension implied by its MIME
type (e.g. an extension-less PDF is stored as `<name>.pdf`; an extension-less
text file on the fs/git agents is stored as `<name>.txt`).

**Detect-then-filter.** Files are filtered by the `EXTENSIONS` configuration
against their **detected** type, not their original filename. The default
extensions are `md`, `pdf`, `doc`, `docx`. Extension-less files are no longer
skipped up front — they are read/downloaded, classified by content, and only
then filtered. A file survives when the extension implied by its detected MIME
type is in `EXTENSIONS`.

To add more types (for example, to keep extension-less text files, whose
detected type is `text/plain` → `txt`):

```bash
export EXTENSIONS=md,pdf,doc,docx,txt,rst
```

> **Note:** puremagic identifies binary formats by signature but cannot
> recognise plain text or Markdown (which have no magic bytes); those still
> resolve via their extension or, on the fs/git agents, the `text/plain`
> default above.

The `validate-config` / `check-status` commands additionally reject files
whose recorded content type is an archive or opaque binary:

- ZIP archives
- RAR archives
- 7z archives
- Generic binary files without proper MIME types

### Issues as Documents

For SCM sources, issues (including their comments) are rendered as Markdown documents and ingested alongside repository files. This enables full-text search and analysis of issue discussions.

## Examples

As an example, the soliplex [documentation](https://github.com/soliplex/soliplex/tree/main/docs)) can be loaded using both the filesystem and via git.

### Example 1: Ingest Local Documents

**Quick version (NEW - no inventory.json needed):**

```bash
git clone https://github.com/soliplex/soliplex.git

# Set up environment
export DOWNLOAD_DIR=./downloads

# Write directly from directory!
uv run si-agent fs run-inventory <path-to-checkout>/soliplex/docs soliplex-docs

# Files land under ./downloads/soliplex-docs/, each with a .meta.json sidecar
ls ./downloads/soliplex-docs
```

**Traditional version (with inventory.json):**

```bash
git clone https://github.com/soliplex/soliplex.git

# Set up environment
export DOWNLOAD_DIR=./downloads

# Create inventory (optional - only if you want to review/modify it)
uv run si-agent fs build-config <path-to-checkout>/soliplex/docs
# You may see messages about ignored files

# If you want to update the inventory.json file, do it here

# Validate configuration
uv run si-agent fs validate-config <path-to-checkout>/soliplex/docs/inventory.json
# If there are errors, fix them now

# Write
uv run si-agent fs run-inventory <path-to-checkout>/soliplex/docs/inventory.json soliplex-docs

ls ./downloads/soliplex-docs
```

### Example 2: Ingest GitHub Repository

```bash
# Set up environment
export DOWNLOAD_DIR=./downloads
export scm_auth_token=ghp_your_token_here

# Write repository contents
si-agent scm run-inventory github mycompany/soliplex

# Files land under ./downloads/github_mycompany_soliplex_all/
ls ./downloads
```

### Example 3: Ingest from WebDAV Server

```bash
# Set up environment
export DOWNLOAD_DIR=./downloads
export WEBDAV_URL=https://nextcloud.example.com/remote.php/dav/files/username
export WEBDAV_USERNAME=your-username
export WEBDAV_PASSWORD=your-password

# Write directly from WebDAV directory
si-agent webdav run-inventory /Documents/project-docs webdav-docs

# Files land under ./downloads/webdav-docs/
ls ./downloads/webdav-docs
```

### Example 4: Index Ingested Documents with haiku-rag

```bash
# Set up environment
export DOWNLOAD_DIR=./downloads
export LANCEDB_DIR=./lancedb
export HAIKU_PATH=./haiku-config
export HAIKU_LOAD_ENABLED=true

# Run a manifest and load the result into ./lancedb/<source>.lancedb
si-agent manifest run /path/to/manifest.yml --load
```

## Server API

The agents can be run as a REST API server using FastAPI. This exposes all agent operations as HTTP endpoints with support for authentication and interactive documentation.

### Starting the Server

```bash
# Basic
si-agent serve

# Custom host and port
si-agent serve --host 0.0.0.0 --port 8080

# Development mode with auto-reload
si-agent serve --reload
```

**The server always runs as a single worker process.** The manifest
scheduler keeps its cron state and execution locks in memory, so running
multiple workers would make each worker register every cron and run every
manifest independently, with no cross-process coordination. Multi-worker
mode is therefore intentionally not exposed, and any `WEB_CONCURRENCY`
environment variable is ignored. Scale out with multiple single-worker
instances behind a load balancer instead (note that scheduling should only
be enabled on one instance — see [Scheduling](#scheduling)).

### Authentication

The server supports multiple authentication methods:

#### 1. No Authentication (Default)

```bash
si-agent serve
# All requests allowed
```

#### 2. API Key Authentication

```bash
export API_KEY=your-api-key
export API_KEY_ENABLED=true
si-agent serve
```

Clients must include the API key in the `Authorization` header:

```bash
curl -H "Authorization: Bearer your-api-key" http://localhost:8001/api/fs/status
```

#### 3. OAuth2 Proxy Headers

```bash
export AUTH_TRUST_PROXY_HEADERS=true
si-agent serve
```

The server will trust authentication headers from a reverse proxy (e.g., OAuth2 Proxy):
- `X-Auth-Request-User`
- `X-Forwarded-User`
- `X-Forwarded-Email`

### API Endpoints

#### Filesystem Routes (`/api/v1/fs/`)

**NEW:** All endpoints now accept both file paths (inventory.json) and directory paths!

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/fs/build-config` | Build inventory from directory |
| `POST` | `/api/v1/fs/validate-config` | Validate inventory (file or directory) |
| `POST` | `/api/v1/fs/check-status` | Check which files need ingestion (file or directory) |
| `POST` | `/api/v1/fs/run-inventory` | Ingest documents (file or directory) |

**Examples:**

```bash
# Build configuration from directory
curl -X POST http://localhost:8001/api/v1/fs/build-config \
  -F "path=/path/to/docs"

# Validate using directory (no inventory.json needed)
curl -X POST http://localhost:8001/api/v1/fs/validate-config \
  -F "config_file=/path/to/docs"

# Or validate using existing inventory file
curl -X POST http://localhost:8001/api/v1/fs/validate-config \
  -F "config_file=/path/to/docs/inventory.json"

# Ingest directly from directory
curl -X POST http://localhost:8001/api/v1/fs/run-inventory \
  -F "config_file=/path/to/docs" \
  -F "source=my-source"
```

#### SCM Routes (`/api/scm/`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/scm/{platform}/{repo}/issues` | List repository issues |
| `GET` | `/api/scm/{platform}/{repo}/files` | List repository files |
| `POST` | `/api/scm/{platform}/{repo}/ingest` | Ingest repo files and issues |

**Example:**

```bash
# List GitHub issues
curl http://localhost:8001/api/scm/github/my-repo/issues?owner=myuser

# Ingest repository
curl -X POST http://localhost:8001/api/scm/github/my-repo/ingest \
  -H "Content-Type: application/json" \
  -d '{"owner": "myuser", "source": "my-source"}'
```

#### WebDAV Routes (`/api/v1/webdav/`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/webdav/validate-config` | Validate inventory from WebDAV path |
| `POST` | `/api/v1/webdav/check-status` | Check which files need ingestion |
| `POST` | `/api/v1/webdav/run-inventory` | Ingest documents from WebDAV directory |
| `POST` | `/api/v1/webdav/run-from-file` | Ingest documents from an uploaded URL list file |

**Examples:**

```bash
# Validate using WebDAV path
curl -X POST http://localhost:8001/api/v1/webdav/validate-config \
  -F "config_path=/documents" \
  -F "webdav_url=https://webdav.example.com"

# Ingest from WebDAV directory
curl -X POST http://localhost:8001/api/v1/webdav/run-inventory \
  -F "config_path=/documents" \
  -F "source=my-source" \
  -F "webdav_url=https://webdav.example.com" \
  -F "webdav_username=user" \
  -F "webdav_password=pass"

# Ingest from uploaded URL list file (with skip hash check)
curl -X POST http://localhost:8001/api/v1/webdav/run-from-file \
  -F "file=@urls.txt" \
  -F "source=my-source" \
  -F "skip_hash_check=true" \
  -F "webdav_url=https://webdav.example.com" \
  -F "webdav_username=user" \
  -F "webdav_password=pass"
```

#### Web Routes (`/api/v1/web/`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/web/run-inventory` | Ingest web pages from a JSON array of URLs |
| `POST` | `/api/v1/web/run-from-file` | Ingest web pages from an uploaded URL list file |

**Examples:**

```bash
# Ingest web pages from URL list
curl -X POST http://localhost:8001/api/v1/web/run-inventory \
  -F "urls=[\"https://example.com/page1\", \"https://example.com/page2\"]" \
  -F "source=my-source"

# Ingest web pages with extra metadata
curl -X POST http://localhost:8001/api/v1/web/run-inventory \
  -F "urls=[\"https://example.com/page1\"]" \
  -F "source=my-source" \
  -F "metadata={\"project\": \"test\"}"

# Ingest web pages from uploaded file
curl -X POST http://localhost:8001/api/v1/web/run-from-file \
  -F "file=@urls.txt" \
  -F "source=my-source"
```

#### Manifest Routes (`/api/v1/manifest/`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/manifest/run` | Run manifests from a file or directory path |
| `POST` | `/api/v1/manifest/run-single` | Run a single manifest file |
| `POST` | `/api/v1/manifest/validate` | Validate manifests without executing |

**Examples:**

```bash
# Run all manifests in a directory
curl -X POST http://localhost:8001/api/v1/manifest/run \
  -F "path=/path/to/manifests"

# Run a single manifest
curl -X POST http://localhost:8001/api/v1/manifest/run-single \
  -F "path=/path/to/manifest.yml"

# Validate manifest files
curl -X POST http://localhost:8001/api/v1/manifest/validate \
  -F "path=/path/to/manifests"
```

#### Health Check

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Server health check |

**Example:**

```bash
curl http://localhost:8001/health
# Returns: {"status": "healthy"}
```

### API Documentation

Interactive API documentation is available at:
- **Swagger UI:** `http://localhost:8001/docs`
- **ReDoc:** `http://localhost:8001/redoc`
- **OpenAPI JSON:** `http://localhost:8001/openapi.json`

### Docker Deployment

The server is designed to run in containers. The `Dockerfile` is a
multi-stage build exposing two selectable targets:

| Target | Purpose | Dependencies | Default command |
|--------|---------|--------------|-----------------|
| `production` | Minimal runtime image (**default target**) | Runtime only (`uv sync --no-dev`) | `si-agent serve --host=0.0.0.0` |
| `development` | Local dev with live reload | Runtime **and** dev deps (`uv sync`) | `si-agent serve --host=0.0.0.0 --reload` |

Both stages run as a non-root `appuser` (uid/gid `1000` by default,
overridable via the `APP_UID`/`APP_GID` build args), include `git` for SCM
CLI mode, expose port `8001`, and define a `/health` healthcheck.

#### Production

`production` is the last stage, so it is built when no `--target` is given:

```bash
# Build the production image (default target)
docker build -t ingester-agents:latest .

# Run with environment variables
docker run -d \
  -p 8001:8001 \
  -e DOWNLOAD_DIR=/data/downloads \
  -e API_KEY_ENABLED=true \
  -e API_KEY=your-secret-key \
  -v "$(pwd)/downloads:/data/downloads" \
  ingester-agents:latest

# Check health
curl http://localhost:8001/health
```

#### Development

The `development` target includes the full toolchain and starts uvicorn
with `--reload`. Bind-mount the source so code changes reload live:

```bash
# Build the development image
docker build --target development -t ingester-agents:dev .

# Run with the source bind-mounted for live reload
docker run --rm -it \
  -p 8001:8001 \
  -v "$(pwd):/app" \
  ingester-agents:dev
```

To match file ownership on bind mounts to your host user, pass build args:

```bash
docker build --target development \
  --build-arg APP_UID="$(id -u)" \
  --build-arg APP_GID="$(id -g)" \
  -t ingester-agents:dev .
```

The Docker image includes:
- Non-root user for security
- Health checks for orchestration
- Proper signal handling
- Production-ready uvicorn configuration

## Troubleshooting

### Authentication Errors

Ensure your tokens have the required permissions:
- **GitHub**: `repo` scope for private repositories, public access for public repos
- **Gitea**: Access token with read permissions

### Connection Errors

For SCM and WebDAV sources, verify the source server is reachable and any
required credentials (`scm_auth_token`, `WEBDAV_URL`/`WEBDAV_USERNAME`/
`WEBDAV_PASSWORD`) are set. Downloaded files are written under `DOWNLOAD_DIR`.

### File Not Found Errors

For SCM agents, ensure the repository name and owner are correct. Use the exact repository name, not the URL.

## Development

### Setup

```bash
# Clone repository
git clone <repository-url>
cd ingester-agents

# Install dependencies with dev tools
uv sync

# Run tests
uv run pytest

# Run linter
uv run ruff check
```

### Testing

The project uses pytest with 100% code coverage requirements:

```bash
# Run unit tests with coverage
uv run pytest

# Run specific tests
uv run pytest tests/unit/test_client.py

# Generate coverage report
uv run pytest --cov-report=html
```

### Code Quality

The project uses Ruff for linting and code formatting:

```bash
# Check code
uv run ruff check

# Auto-fix issues
uv run ruff check --fix

# Format code
uv run ruff format
```

## Architecture

```text
soliplex.agents/
├── src/soliplex/agents/
│   ├── cli.py              # Main CLI entry point (includes 'serve' command)
│   ├── local_store.py      # Writes downloaded documents + .meta.json sidecars
│   ├── local_state.py      # Per-source SQLite sync state (hashes + commit SHA)
│   ├── config.py           # Configuration, settings, and manifest models
│   ├── server/             # FastAPI server
│   │   ├── __init__.py     # FastAPI app initialization, scheduler
│   │   ├── auth.py         # Authentication (API key & OAuth2 proxy)
│   │   └── routes/
│   │       ├── __init__.py
│   │       ├── fs.py       # Filesystem API endpoints
│   │       ├── scm.py      # SCM API endpoints
│   │       ├── webdav.py   # WebDAV API endpoints
│   │       ├── web.py      # Web API endpoints
│   │       └── manifest.py # Manifest API endpoints
│   ├── common/              # Shared utilities
│   │   ├── urls_file.py     # URL list reader (local, S3, WebDAV)
│   │   ├── s3.py            # S3 object reader
│   │   ├── mime.py          # Content-based MIME detection + extension logic
│   │   └── config.py        # Inventory read/validate helpers
│   ├── fs/                 # Filesystem agent
│   │   ├── app.py          # Core filesystem logic
│   │   └── cli.py          # Filesystem CLI commands
│   ├── web/                # Web agent
│   │   └── app.py          # Core web fetching logic
│   ├── webdav/             # WebDAV agent
│   │   ├── app.py          # Core WebDAV logic
│   │   └── cli.py          # WebDAV CLI commands
│   ├── manifest/           # Manifest runner
│   │   ├── runner.py       # YAML loading, validation, dispatch
│   │   └── cli.py          # Manifest CLI commands
│   └── scm/                # SCM agent
│       ├── app.py          # Core SCM logic
│       ├── cli.py          # SCM CLI commands
│       ├── base.py         # Base SCM provider interface
│       ├── github/         # GitHub implementation
│       ├── gitea/          # Gitea implementation
│       └── lib/
│           ├── templates/  # Issue rendering templates
│           └── utils.py    # Utility functions
├── example-manifests/      # Example manifests (fs, scm, web, webdav, composite, delete-stale)
├── tests/                  # Test suite
│   └── unit/
│       ├── test_server_*.py  # Server API tests
│       └── ...
├── Dockerfile              # Production container
├── .dockerignore           # Build context exclusions
└── DOCKERFILE_CHANGES.md   # Docker implementation documentation
```

### Key Components

**CLI Layer:**
- `cli.py` - Main entry point with `fs`, `web`, `scm`, `webdav`, `manifest`, and `serve` commands
- Agent-specific CLI commands in `fs/cli.py`, `webdav/cli.py`, `scm/cli.py`, and `manifest/cli.py`

**Server Layer:**
- `server/` - FastAPI application
- `server/auth.py` - Flexible authentication (none, API key, OAuth2 proxy)
- `server/routes/` - REST API endpoints mirroring CLI functionality

**Agent Layer:**
- `fs/app.py` - Filesystem operations (shared by CLI and API)
- `web/app.py` - Web page fetching and ingestion (shared by CLI and API)
- `webdav/app.py` - WebDAV operations (shared by CLI and API)
- `scm/app.py` - SCM operations (shared by CLI and API)
- `manifest/runner.py` - Manifest loading, validation, and dispatch to agents
- `local_store.py` - Writes fetched documents and metadata sidecars to `DOWNLOAD_DIR`
- `local_state.py` - Local synchronization state (content hashes + SCM commit markers)

**Configuration:**
- `config.py` - Pydantic settings and manifest component models
- Environment variables or `.env` file for configuration
- YAML manifest files for declarative multi-source ingestion

## License

See LICENSE file for details.

## Support

For issues and questions, please open an issue on the repository.
