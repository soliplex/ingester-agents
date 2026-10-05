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
  - `manifest migrate` / `manifest vacuum` maintain those databases
  - Metadata providers add each document's sidecar and a PDF's page count and
    document information to its haiku-rag metadata; `manifest
    backfill-metadata` fills them in for documents already indexed

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

Agents write fetched documents to the download store -- the local filesystem
by default, or S3-compatible object storage (see
[Object Storage](#object-storage)).

```bash
# Directory where downloaded documents are written. Each run stores files
# under <DOWNLOAD_DIR>/<source>/, preserving the source directory structure.
# Every document is accompanied by a <filename>.meta.json sidecar containing
# its MIME type and any other available metadata.
DOWNLOAD_DIR=downloads

# Directory for local synchronization state (content hashes + SCM commit
# markers), one SQLite file per source. Stays on local disk even when documents
# are written to object storage -- SQLite cannot live on S3.
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
# LOG_CONFIG_FILE=/etc/ingester/logging.yaml   # extra handlers; see Custom Logging below
# LOG_CONFIG_STRICT=false                      # fail startup when that file can't be applied

# API Server Configuration
SERVER_HOST=127.0.0.1
SERVER_PORT=8001

# Authentication (for API server)
API_KEY=your-api-key
API_KEY_ENABLED=false
AUTH_TRUST_PROXY_HEADERS=false

# Manifests the server runs: on a cron (SCHEDULER_ENABLED=true) and on demand
# via POST /api/v1/manifest/run (always available)
MANIFEST_DIR=/path/to/manifests
# SCHEDULER_RECONCILE_CRON="*/1 * * * *"   # how often the dir is rescanned

# Pre-process spool: where each document is staged while its pre-process
# steps run (default: system temp dir; needs room for the largest document)
# PRE_PROCESS_SPOOL_DIR=/var/lib/ingester/spool

# haiku-rag loading (runs `haiku-ingester run-batch` after each manifest run)
HAIKU_LOAD_ENABLED=false
LANCEDB_DIR=/var/lib/lancedb          # read by the haiku-rag config, which
                                      # places <source>.lancedb under it
HAIKU_PATH=/etc/haiku                  # base dir for haiku-rag config files
# HAIKU_DEFAULT_CONFIG=haiku.rag.default.yaml   # config filename under HAIKU_PATH
# HAIKU_LOAD_COMMAND=haiku-ingester --config={haiku_cfg} run-batch
# HAIKU_LOAD_TIMEOUT=1800
# HAIKU_LOAD_CWD=/var/lib/ingester     # subprocess working dir (default: inherit)

# haiku-rag maintenance (`si-agent manifest migrate` / `vacuum` / `backfill-metadata`)
# HAIKU_MAINTENANCE_COMMAND=haiku-rag --config={haiku_cfg} {verb}   # not backfill-metadata
# HAIKU_MAINTENANCE_TIMEOUT=3600

# haiku subprocess output (load and maintenance) is logged in parts, inside
# the run's span: one record per chunk, split at a line break where possible
# HAIKU_OUTPUT_CHUNK_BYTES=65536        # size of one logged part
# HAIKU_OUTPUT_FLUSH_SECONDS=30         # log pending output at least this often
# HAIKU_OUTPUT_MAX_BYTES=0              # cap on logged output per stream; 0 = none

# Tracing (see Tracing with Logfire below)
# LOGFIRE_TOKEN=...                     # or /run/secrets/logfire_token
# LOGFIRE_SERVICE_NAME=ingester-agents
# HAIKU_TRACE_WRAPPER=false             # join haiku-ingester's spans to the agent's trace

# S3-compatible storage. S3_ENDPOINT_URL is shared between urls_file reads
# and the download store; the rest are the download store's credentials.
S3_ENDPOINT_URL=https://minio.example.com:9000
# S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY / S3_REGION -- omit to use the AWS
# default credential chain (environment, instance role, profile).
# S3_ALLOW_HTTP=true                  # required for an http:// endpoint

# Setting a bucket moves the download store into object storage. Accepts a
# bare name or an s3://bucket/prefix URI. See Object Storage below.
# DOWNLOAD_S3_BUCKET=my-documents
```

See [haiku-rag Loading](#haiku-rag-loading) for what these settings do. The
haiku-rag config file needs a further set of its own — model and service
endpoints, and the bucket variables in S3 mode — which fail the load when
unset; see [Variables the haiku-rag config
needs](#variables-the-haiku-rag-config-needs).

### Custom Logging

`LOG_CONFIG_FILE` names a YAML (or JSON) file in
[`logging.config.dictConfig`](https://docs.python.org/3/library/logging.config.html#logging-config-dictschema)
form, which is applied on top of the built-in setup. Use it to send particular
loggers somewhere else, for example to an HTTP log sink. `()` names any
factory importable in the environment, so a package installed alongside this
one can supply its own handler classes.

```yaml
version: 1
handlers:
  sink:
    class: logging.handlers.QueueHandler   # sends from a background thread
    level: ERROR
    listener: logging.handlers.QueueListener
    handlers: [sink_http]
    queue:
      (): queue.Queue
      maxsize: 1000                        # a down sink can't grow it forever
  sink_http:
    (): my_package.log_handlers.MyHTTPHandler
    level: ERROR
    host: logs.example.com
root:
  handlers: [sink]
```

How the file is applied:

- **It only adds.** The console handler, the SMTP handler and Logfire are
  installed whether or not the file names the root logger. `LOG_LEVEL` still
  sets the root level, unless the file sets `root.level`.
- **Existing loggers stay on.** `disable_existing_loggers` defaults to `false`.
  The stdlib default would silence every module logger, since they all exist
  by the time logging is configured.
- **Queue listeners are started for you.** `dictConfig` builds a
  `QueueHandler`'s listener but doesn't start it. Each time logging is
  configured, the previous listeners are stopped and the queued records
  delivered first; they are stopped the same way at exit. Put any handler that
  makes network calls behind a `QueueHandler`, so a slow destination never
  blocks the event loop.
- **A file that can't be applied doesn't stop the process.** This covers a
  missing file, invalid YAML, a document that isn't a mapping, and a config
  `dictConfig` rejects. The built-in setup is used, a warning is logged, and
  whatever the file had partly configured is removed. Set
  `LOG_CONFIG_STRICT=true` to fail instead.

The file covers this process only. The `haiku-ingester` and `haiku-rag`
subprocesses configure their own logging; their output reaches this process's
log through the `soliplex.agents.manifest.haiku_process` logger.

### Tracing with Logfire

With a Logfire token (`LOGFIRE_TOKEN`, or the `logfire_token` secret at
`/run/secrets/logfire_token`), **the server** sends its logs and traces to
Logfire. Without one, nothing is sent, and the spans below cost nothing.

- Each HTTP request is a span, except `/health`, which the container
  healthcheck polls.
- Each manifest run is one trace: a `manifest run` span with a `component`
  span per component, plus `list scm uris` / `delete stale` when they run.
  Every log line an agent writes nests under the component that wrote it,
  so every error from one run can be found under that run.
- The haiku load that a run queues stays in the run's trace: a `haiku load`
  span with the exact command, exit status and time spent in the queue, and
  a `post-process` span per step. The subprocess output is logged in parts
  (`haiku load <source> stderr output part <n>`) inside it.

**The CLI is opt-in:** pass `--otel` before the command.

```bash
si-agent --otel manifest vacuum /manifests/test.yaml
```

The whole command becomes one trace under a `cli` span, whose message is
the command line (`si-agent manifest vacuum /manifests/test.yaml`).
Password-like option values are recorded as `[redacted]`. Without `--otel`,
the CLI sends nothing even with a token. `serve` ignores `--otel`: the
server always configures itself when a token is available.

**haiku-ingester's own spans.** The agent always passes its trace context
to haiku subprocesses as `TRACEPARENT` / `TRACESTATE`. A haiku-rag that
reads it joins the agent's trace by itself. For one that doesn't, set
`HAIKU_TRACE_WRAPPER=true`. The agent then starts a console-script command
through `python -m soliplex.agents.traced_run`, which attaches the context
before the CLI runs. It only works when haiku-rag is installed in the same
environment as the agent, and a command that isn't a console script there
runs unchanged.

### Object Storage

The download store defaults to the local filesystem. Setting a bucket moves it
to S3-compatible object storage, where `DOWNLOAD_DIR` becomes the key prefix
rather than a directory:

```bash
DOWNLOAD_S3_BUCKET=my-documents
DOWNLOAD_DIR=ingester/downloads        # now a key prefix
S3_ENDPOINT_URL=http://minio:9000      # omit for AWS
S3_ALLOW_HTTP=true                     # required for an http:// endpoint
```

Documents then land at `s3://my-documents/ingester/downloads/<source>/<path>`,
with the same `.meta.json` sidecar beside each one. No extra install step:
`obstore` is a plain dependency, so object storage is available in every
install and only the configuration decides whether it is used.

Credentials come from `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` /
`S3_REGION`, or are omitted entirely to use the AWS default chain (environment,
instance role, profile).

`DOWNLOAD_S3_BUCKET` is named deliberately rather than `S3_BUCKET`: the mode is
inferred from its presence, so a generic variable that some other service in
the same environment happens to export must not be able to silently redirect
every download.

#### Bucket spelling

The bucket accepts a full `s3://` URI as well as a bare name, matching the
`S3_BUCKET` value the haiku-rag configs interpolate, so one deployment
variable can feed both the reader and the writer:

| `DOWNLOAD_S3_BUCKET` | `DOWNLOAD_DIR` | Documents land at |
|---|---|---|
| `my-documents` | `ingester/downloads` | `s3://my-documents/ingester/downloads/<source>/` |
| `s3://my-documents` | `ingester/downloads` | `s3://my-documents/ingester/downloads/<source>/` |
| `s3://my-documents/ingester` | `downloads` | `s3://my-documents/ingester/downloads/<source>/` |

A prefix on the bucket nests `DOWNLOAD_DIR` beneath it, so all three
spellings above address the same objects -- and are treated as the same
target, sharing one state file rather than re-fetching everything because a
prefix moved between two variables. A non-`s3` scheme is rejected when
configuration is read rather than at the first write.

#### Turning it off

A **blank** `DOWNLOAD_S3_BUCKET` means the same thing as an absent one: local
disk. Clearing the value is therefore how object storage is disabled from a
compose `.env`, where a key is always present once the compose file
references it:

```bash
DOWNLOAD_S3_BUCKET=              # object storage off, downloads go to disk
```

Whitespace counts as blank, so a stray trailing space disables the store
rather than failing the boot. Leading and trailing space is stripped from a
real value for the same reason.

**`STATE_DIR` stays local.** The per-source SQLite files hold the content
hashes that drive incremental ingestion, and SQLite cannot live on object
storage. Moving documents to S3 does not by itself make the agent stateless: a
persistent volume is still required for state.

#### Switching an installation

Where documents go is chosen **per installation**, not per manifest: every
source in an installation uses the same store. To move an installation to
object storage, set `DOWNLOAD_S3_BUCKET` and point the haiku-rag load at a
config whose source stanza reads from S3, either for the whole installation
with `HAIKU_DEFAULT_CONFIG=haiku.rag.s3.yaml` or for one manifest with its
`haiku_config`. `DOWNLOAD_URI` is injected for every load and holds the
resolved base URI in both modes, so a source stanza with `uri:
${DOWNLOAD_URI}` works either way.

A source's SQLite state file is qualified by its target, so after a switch
every source opens fresh state and re-fetches its documents from upstream into
the new store. On the indexing side:

- The document URIs change (`file://…` becomes `s3://…`), and the indexer keys
  its own state by URI, so every document is re-converted and re-embedded.
  This is the real cost of a switch.
- If the haiku-rag source stanza omits `id:`, its identity is derived from the
  target, so switching *replaces* one source with a different one and the old
  documents are never cleaned up. Set `id:` explicitly -- then a switch is
  self-cleaning -- or drop and rebuild each source's `.lancedb`.

Manifests used to accept a per-manifest `download_store` block. It was
applied by temporarily rewriting the shared settings, which the haiku load
and its post-process callbacks -- running later -- never saw, so they read
the installation default instead. It has been removed: a manifest that still
sets it is rejected with a message saying so.

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

All ingestion runs through **manifests**: YAML files that name a source
and the components (filesystem, SCM, WebDAV, web) feeding it. Run them from
the CLI with `si-agent manifest run`, or let the server run them on a cron
schedule or on demand (`POST /api/v1/manifest/run`).

The CLI tool `si-agent` has five command groups:

- **`manifest`**: Run manifests, and maintain the haiku-rag databases they load
- **`fs`**: Inspect a local directory before ingesting it
- **`scm`**: Inspect a Git repository and manage its incremental sync state
- **`webdav`**: Inspect a WebDAV directory, and export its file list for a manifest
- **`serve`**: REST API server: manifest scheduler, on-demand runs, and inspection routes

### Filesystem Agent

#### Quick Start

Write a manifest with an `fs` component:

```yaml
# docs.yml
id: local-docs
name: local docs
source: my-source-name
components:
  - name: docs
    type: fs
    path: /path/to/documents
```

Then run it:

```bash
si-agent manifest run docs.yml --no-load
```

That's it! The runner automatically:

1. Scans the directory
2. Builds the inventory
3. Validates files
4. Writes new and changed documents to the download store, and removes ones
   no longer in the directory

#### Inspecting before ingestion

The `fs` commands take the document **directory** and build the inventory by
scanning it. To review what a run would do first:

**1. Preview the inventory**

```bash
si-agent fs build-config /path/to/documents
```

Prints the inventory as JSON — paths, hashes, sizes, and detected MIME types —
without writing anything. Redirect it to a file if you want to keep a copy.

**2. Validate**

Check which files are supported:

```bash
si-agent fs validate-config /path/to/documents
```

**3. Check status**

See which files need to be ingested:

```bash
si-agent fs check-status /path/to/documents my-source-name
```

Add `--detail` to see the full list of files:

```bash
si-agent fs check-status /path/to/documents my-source-name --detail
```

The status check compares file hashes against the local sync state:

- **new**: File doesn't exist in local state
- **mismatch**: File exists but content has changed
- **match**: File is unchanged (will be skipped during the run)

To narrow *which* files are considered, set `extensions` in the manifest (or
`EXTENSIONS`) rather than editing an inventory by hand.

### SCM Agent

#### Ingesting a repository

Ingest **both files and issues** from a repository with an `scm` manifest
component. Issues are rendered as Markdown documents with their comments.

```yaml
id: my-repo
name: my repo
source: my-repo
components:
  - name: repo
    type: scm
    platform: github        # or gitea (needs base_url or SCM_BASE_URL)
    owner: myorg
    repo: my-repo
    incremental: true       # commit-based sync after the first full run
    # branch: main
    # content_filter: all   # all | files | issues
```

```bash
si-agent manifest run my-repo.yml --no-load
```

Files and issues are written under `<DOWNLOAD_DIR>/<source>/`. Issues are
saved as Markdown (`.md`) documents. See `example-manifests/scm.yml`.

With `incremental: true`, the first run performs a full sync and records the
latest commit; later runs process only files changed since then, which cuts
API calls and bandwidth substantially (see
[Incremental Sync](#incremental-sync-scm-agent)).

#### Inspecting a repository

```bash
# List issues
si-agent scm list-issues github myorg/my-repo
si-agent scm list-issues gitea admin/my-repo

# List repository files
si-agent scm get-repo github myorg/my-repo
si-agent scm get-repo gitea admin/my-repo
```

#### Sync State Management

View and manage the incremental sync state for a repository:

```bash
# View current sync state
si-agent scm get-sync-state gitea admin/my-repo

# Reset sync state (forces a full sync on the next manifest run)
si-agent scm reset-sync gitea admin/my-repo
```

### WebDAV Agent

The WebDAV agent ingests documents from WebDAV servers (like Nextcloud,
ownCloud, SharePoint, etc.).

#### Ingesting from WebDAV

Use a `webdav` manifest component. It takes exactly one of `path` (scan a
directory recursively), `urls` (an inline list of files) or `urls_file` (a
URL list file, read from a local path, S3, or the WebDAV server itself):

```yaml
id: webdav-docs
name: WebDAV Documents
source: my-source-name
components:
  # Scan an entire WebDAV directory recursively
  - name: shared-drive
    type: webdav
    url: https://webdav.example.com
    path: /documents

  # Ingest only a curated list of files
  - name: curated-files
    type: webdav
    url: https://webdav.example.com
    urls_file: urls.txt
```

```bash
export WEBDAV_USERNAME=your-username
export WEBDAV_PASSWORD=your-password
si-agent manifest run webdav.yml --no-load
```

Each URL in a URL list is processed independently: if a file fails to
download, the error is recorded and processing continues with the rest. See
`example-manifests/webdav.yml` for every source form.

#### Commands

**1. Export URLs**

Scan a WebDAV directory and export discovered file URLs to a file, for review
or to use as a component's `urls_file`. This uses only directory listing
(PROPFIND) and does not download file content:

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

Only files matching the configured `EXTENSIONS` filter are included. Edit the
list down to the files you want, then point a manifest component's
`urls_file` at it.

**2. Validate Configuration**

Check if files are supported (downloads files to compute hashes):

```bash
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

Maintain the databases those manifests load into (see
[Database Maintenance](#database-maintenance)):

```bash
si-agent manifest migrate           # every manifest in $MANIFEST_DIR
si-agent manifest vacuum --dry-run  # print the commands without running them
si-agent manifest backfill-metadata --missing page_count --check  # what a back-fill would change
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

Unknown keys are rejected at every level (top level, `config`, `schedule`,
post-process steps and components), so a typo such as `extentions` is a
validation error rather than a setting silently left at its default. The
server logs a rejected manifest once, at ERROR, and doesn't run it; `si-agent
manifest run` exits 1. `metadata` and a post-process step's `kwargs` are
free-form and accept any keys.

Top-level fields:

- **id** (required): Unique identifier for the manifest. Must be unique across all manifests when running from a directory.
- **name** (required): Human-readable name for display and logging.
- **source** (required): Source name; also the per-source folder name under `DOWNLOAD_DIR` (sanitized for filesystem safety).
- **schedule**: Optional cron schedule for automated execution via the REST API server.
  - **cron**: Cron expression (e.g., `"0 0 * * *"` for daily at midnight).
- **config**: Optional shared configuration applied to all components.
  - **metadata**: Key-value pairs attached to all ingested documents.
  - **extensions**: File extensions to include (overrides the global `EXTENSIONS` setting).
  - **delete_stale**: Remove locally-stored documents that no longer appear in any component (default: true). See [Stale Document Removal](#stale-document-removal) below.
  - **haiku_config**: Override the haiku-rag config file used when loading this manifest's source. Absolute paths are used as-is; relative values resolve under `HAIKU_PATH`. Defaults to `${HAIKU_PATH}/haiku.rag.default.yaml`. See [haiku-rag Loading](#haiku-rag-loading).
  - **pre_run**: Ordered steps run once before any component; one may skip the run. See [Pre-run steps](#pre-run-steps) below.
  - **pre_process**: Ordered steps run on each new or changed document before it is stored; one may skip or modify it. None run unless listed. See [Pre-process steps](#pre-process-steps) below.
  - **post_process**: Ordered callbacks run after the haiku-rag load completes. See [Post-process callbacks](#post-process-callbacks) below.
- **components** (required): List of ingestion components (see below).

#### Component Types

**Filesystem (`fs`):**

- **name** (required): Component name (must be unique within the manifest).
- **path** (required): Path to a local directory.
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
  fire-and-forget task), then never again unless triggered via the API
  (`POST /api/v1/manifest/run`).

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

- **At most one manifest runs at a time.** Due manifests are handed to a
  single-worker FIFO queue (`server/manifest_queue.py`) that drains them one
  at a time, so different manifests never run concurrently. This bounds
  resource use, since a single manifest can already fan out across its
  components. Serialization is structural — the worker does one thing at a
  time — rather than enforced by a lock each caller has to remember to take.
  The scheduler and `POST /api/v1/manifest/run` both feed this same queue,
  so an on-demand run never overlaps a scheduled one.
- **Manifests due at the same time all run, in order.** A cron firing while
  another manifest is in progress is queued behind it, not skipped, so no
  scheduled occurrence is silently lost.
- **A repeat occurrence of a still-pending manifest coalesces.** If a
  manifest comes due again while its previous run is still queued or
  running, the new occurrence folds into the pending one (logged) rather
  than queueing a redundant second run — the pending run reloads the
  manifest from disk and so covers the newer occurrence anyway. This bounds
  the queue at the number of manifests on disk, so a cron faster than its
  own runs cannot grow a backlog.
- **Shutdown cancels the in-flight manifest.** The worker is cancelled and
  awaited on shutdown rather than left orphaned; queued manifests are
  dropped and picked up again by the reconciler on the next start.
- **Single process only.** Because the cron state and run queue are held in
  memory, scheduling relies on the server running as a single worker (see
  [Starting the Server](#starting-the-server)). If you run multiple server
  instances, enable `SCHEDULER_ENABLED` on only one of them, and send
  on-demand runs to that same instance: each instance has its own queue, so
  two instances can run the same manifest at once.

#### On-demand Runs

`POST /api/v1/manifest/run` queues a manifest from `MANIFEST_DIR`, by id, and
returns `202 Accepted`:

```bash
curl -X POST http://localhost:8001/api/v1/manifest/run -F manifest_id=test-scm
# {"status": "queued", "manifest_id": "test-scm"}
```

The run happens on the queue described above, so it waits behind any
manifest already running. `"status": "already_queued"` means the manifest
was already queued or running and this request folded into it. The run queue
starts with the server whether or not `SCHEDULER_ENABLED` is set; that setting
controls only cron scheduling and the startup run of unscheduled manifests.
`GET /api/v1/manifest/queue` lists the ids still queued or running.

Only manifests in `MANIFEST_DIR` can be run this way, never an arbitrary path,
so the API can ingest only what the operator has put there.

#### haiku-rag Loading

When `HAIKU_LOAD_ENABLED=true`, a haiku-rag load is queued after **each**
manifest run (scheduled, startup, or CLI). The load indexes the documents
that the manifest just wrote to `${DOWNLOAD_DIR}/<source>/` into a
per-source LanceDB database. The default command is:

```bash
haiku-ingester --config=${HAIKU_CFG} run-batch
```

Note what is *not* on that command line: a database. The haiku-rag config
places it. See [Where the database comes from](#where-the-database-comes-from)
below — a config that does not place one per source is the one
misconfiguration here that fails quietly.

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
- **Environment.** The subprocess inherits the server's environment plus
  three injected variables:
  - `SOURCE` — the sanitized download-folder name, so a haiku-rag config
    using `root: ${DOWNLOAD_DIR}/${SOURCE}` resolves to the ingested
    documents, and one using
    `databases: {db: ${LANCEDB_DIR}/${SOURCE}.lancedb}` gets a database per
    source.
  - `DOWNLOAD_DIR` — `settings.download_dir`, so the path above resolves
    even when it was left at its default.
  - `DOWNLOAD_URI` — the resolved base URI of the download store, set in
    both filesystem and S3 mode so one config form works either way.

  Any other `${VAR}` interpolated by the haiku-rag config (`LANCEDB_DIR`
  included, along with e.g. `OLLAMA_BASE_URL`, `DOCLING1_BASE_URL`,
  `DOCLING2_BASE_URL`, `EMBEDDINGS_BASE_URL`) must be present in the server's
  environment.

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

##### Where the database comes from

The agent does not choose the database. It passes `--config` and nothing else,
and haiku-rag resolves the location from `lancedb.databases` in that file. So
the config must place exactly one database, and must place a *different* one
per source — which is what `${SOURCE}` is for:

```yaml
lancedb:
  databases:
    db: ${LANCEDB_DIR:-/lancedb}/${SOURCE}.lancedb
```

The entry name (`db` above) is arbitrary and never leaves the configuration.
`${SOURCE}` in the location is what separates one source from another. Four
cases, and only one of them is loud:

| `lancedb.databases` | Result |
|---|---|
| One entry interpolating `${SOURCE}` | Correct: one database per source |
| One entry, no `${SOURCE}` | Every source loads into **one shared database** |
| Absent | Everything lands in `haiku.rag.lancedb` under `storage.data_dir`, resolved relative to `HAIKU_LOAD_CWD` |
| Two or more entries | `haiku-ingester` refuses to start: it writes one database |

The middle two run to completion and silently merge every source into one
store, so check the resolved location in the load's log line the first time a
config is deployed:

```text
Starting haiku load for source 'synced-docs' -> /var/lib/lancedb/synced-docs.lancedb
```

`LANCEDB_DIR` is read twice, and the two readers must agree: the haiku-rag
config interpolates it to place the database, and the agent resolves
`${LANCEDB_DIR}/<slug>.lancedb` for that log line, the run report, and the
maintenance dedupe key. It is required even though the config is what actually
places the database. The two spellings differ for a source containing
whitespace — the agent slugifies (`composite source` →
`composite-source.lancedb`) while `${SOURCE}` sanitizes (`composite
source.lancedb`) — so prefer source ids without spaces.

Working examples for both storage modes are in
[`example-haiku-configs/`](example-haiku-configs/).

##### Variables the haiku-rag config needs

haiku expands `${VAR}` eagerly, when the config file is read — so a variable
that is unset **or empty** fails the load before any document is touched:

```text
MissingEnvVarError: Config references unset or empty environment variable
${QA_MODEL}. Set it, or use ${QA_MODEL:-default} to provide a fallback.
```

There is no partial start and nothing to inspect afterwards, so the whole set
has to be present up front. These are what
[`example-haiku-configs/`](example-haiku-configs/) reference; a config of your
own can of course need fewer or more.

| Variable | Supplied by | Required by | Purpose |
|---|---|---|---|
| `SOURCE` | injected per load | both examples | Sanitized source name. Separates each source's database, queue file and documents — **do not set it yourself** |
| `DOWNLOAD_DIR` | injected per load, from `settings.download_dir` | both examples | Where the manifest run wrote the documents |
| `DOWNLOAD_URI` | injected per load | neither example | Base URI of the download store; available for a config that wants one form across both storage modes |
| `STATE_DIR` | environment | both examples | Holds the per-source ingester queue file |
| `QA_MODEL` | environment | both examples | Vision model used to identify documents |
| `QA_BASE_URL` | environment | both examples | OpenAI-compatible endpoint serving `QA_MODEL` |
| `OLLAMA_BASE_URL` | environment | both examples | Ollama endpoint |
| `DOCLING1_BASE_URL` | environment | both examples | First docling-serve instance |
| `DOCLING2_BASE_URL` | environment | both examples | Second docling-serve instance |
| `EMBEDDINGS_BASE_URL` | environment | both examples | Endpoint serving the embedding model |
| `S3_BUCKET` | environment | `haiku.rag.s3.yaml` | Bucket URI holding the **databases** |
| `S3_REGION` | environment | `haiku.rag.s3.yaml` | Region for that bucket |
| `DOWNLOAD_S3_BUCKET` | environment | `haiku.rag.s3.yaml` | Bucket URI holding the **documents**; the same value the download store wrote with |
| `LANCEDB_DIR` | environment | optional in both (`:-/lancedb`) | Base dir, or S3 key prefix, for the database |
| `INGESTER_AUTH_TOKEN` | environment | only if `ingester.api.auth_token` is uncommented | Bearer token for the ingester's HTTP control plane |

Two notes on that table:

- **`LANCEDB_DIR` is only optional to the config.** The examples default it to
  `/lancedb`, but ingester-agents itself refuses to run a load without it —
  it resolves `${LANCEDB_DIR}/<slug>.lancedb` for the log line, the run report
  and the maintenance dedupe key. Set it, and keep the two in step.
- **Injected variables are not yours to set.** `SOURCE`, `DOWNLOAD_DIR` and
  `DOWNLOAD_URI` are overwritten per load from the run's `LoadContext`; a value
  exported in the server's environment is replaced, not merged. Post-process
  callbacks, which load the same config in-process, see all three mirrored into
  the process environment for the duration of the callbacks, then restored.

`${VAR}` inside a YAML comment is never expanded — the file is parsed
first, and expansion runs over the parsed data — so a commented-out setting
costs nothing.

#### Database Maintenance

Three verbs operate on the per-source LanceDB databases rather than on the
downloaded documents:

```bash
si-agent manifest migrate [PATH] [--json] [--timeout N] [--dry-run]
si-agent manifest vacuum  [PATH] [--json] [--timeout N] [--dry-run]
si-agent manifest backfill-metadata [PATH] [--missing KEY]... [--content-type TYPE]...
                                    [--filter SQL] [--db-name NAME] [--batch-size N]
                                    [--check] [--no-attachments] [--json] [--timeout N] [--dry-run]
```

- `migrate` runs pending haiku-rag schema migrations; `vacuum` optimizes and
  compacts the tables to reclaim disk space; `backfill-metadata` re-runs each
  source's haiku-rag `metadata_provider` over documents already indexed (see
  [Back-filling metadata](#back-filling-metadata)).
- `PATH` is optional and defaults to `all`:

  | `PATH`               | Scope                                            |
  |----------------------|--------------------------------------------------|
  | `all` (default)      | Every `*.yml` / `*.yaml` in `$MANIFEST_DIR`      |
  | a manifest YAML file | That manifest only                               |
  | a directory          | Every manifest in that directory                 |

  `all` is a reserved word, so a file or directory literally named `all`
  cannot be addressed by name.
- **Command** for `migrate` / `vacuum` is configurable via
  `HAIKU_MAINTENANCE_COMMAND` (default
  `haiku-rag --config={haiku_cfg} {verb}`). `backfill-metadata` always runs
  `python -m soliplex.agents.haiku_backfill` with the agent's own interpreter. Placeholders: `{verb}`,
  `{haiku_cfg}`, `{db}`, `{source}`, `{lancedb_dir}`, `{haiku_path}`. As with
  the load command, the template is tokenized before substitution, so values
  containing spaces cannot inject extra arguments.
- **Config file, database, and environment** resolve exactly as they do for a
  load: the config from `config.haiku_config` (or
  `${HAIKU_PATH}/${HAIKU_DEFAULT_CONFIG}`) places the database (see [Where the
  database comes from](#where-the-database-comes-from)), and the parent
  environment plus injected `SOURCE` / `DOWNLOAD_DIR` / `DOWNLOAD_URI` let its
  `${VAR}` references resolve. Output is streamed to the log line by line.
- **One at a time.** Operations run strictly sequentially, the same capacity
  constraint that applies to loads.
- **Deduplicated.** Manifests that share a source resolve to the same
  database; it is processed once and the rest are reported as skipped. The
  dedupe key is the agent's own `${LANCEDB_DIR}/<slug>.lancedb`, not the
  location the config resolves to, so it matches reality only while the config
  places the database under `${LANCEDB_DIR}` by source.
- **Timeout** defaults to `HAIKU_MAINTENANCE_TIMEOUT` (3600s — higher than
  the load timeout because a compaction can outlast a batch load) and can be
  overridden per invocation with `--timeout`.
- **Exit code** is 1 if any operation failed or timed out, so the verbs can
  be used directly in cron or a deploy script. A skipped duplicate is not a
  failure.

`--dry-run` resolves every target and prints the command lines that *would*
run, one per line, in execution order — nothing is spawned. Skips and
resolution failures appear as `#`-prefixed comments so the block stays
paste-safe:

```console
$ si-agent manifest vacuum --dry-run
haiku-rag --config=/etc/haiku/haiku.rag.default.yaml vacuum
haiku-rag --config=/etc/haiku/haiku.rag.web.yaml vacuum
# composite source: skipped (duplicate db)
```

Under `--dry-run` the exit code reflects resolution only, so a dry run
doubles as a config check. Add `--json` for the full per-target detail
(argv, resolved paths, return codes).

> **Note:** nothing coordinates a CLI maintenance run with a load already
> running inside the server — the FIFO queue in `server/haiku_queue.py` only
> serializes loads within that process. Run maintenance during a quiet
> window, or with the scheduler stopped.

#### Manifest hooks

A manifest can run three kinds of optional, ordered steps, named for when they
fire:

| Hook | Fires | Once per | Can stop |
| --- | --- | --- | --- |
| [`pre_run`](#pre-run-steps) | before any component runs | manifest run | the whole run (SKIP) |
| [`pre_process`](#pre-process-steps) | inside each document write, before it is stored | new or changed document | that document (SKIP) |
| [`post_process`](#post-process-callbacks) | after the haiku-ingester load | load | nothing |

```text
pre_run steps ─► components ─┬─► write ─► pre_process steps ─► store
                             │   (per new / changed document)
                             └─► stale reconcile ─► haiku load ─► post_process
```

Every step names a `method` -- a dotted import path, `pkg.mod:func` or
`pkg.mod.func`, importable in the agent's environment -- plus optional
`kwargs`. All `pre_run` and `pre_process` methods are imported before the run
starts, so a typo fails the manifest before any step (a "starting"
notification, say) has run.

#### Pre-run steps

`config.pre_run` runs once, before any component, for notifications and
pre-checks. Each step is called as `method(context, **kwargs)`, where
`context` has `manifest` (a copy -- a step cannot change what runs), `load`
(the source's resolved download target, store and sidecars) and `started_at`.

```yaml
config:
  pre_run:
    - method: soliplex.agents.manifest.pre_run_steps:notify_webhook
      kwargs:
        url_secret: INGEST_WEBHOOK_URL            # docker secret / env var
        secret_headers: { Authorization: INGEST_WEBHOOK_TOKEN }
      on_error: continue      # a failed notification must not block ingestion
      timeout: 15
    - method: soliplex.agents.manifest.pre_run_steps:check_free_space
      kwargs: { min_free_mb: 2048 }
```

A step returns `"continue"` (or `None`) to carry on, or `"skip"` to call the
run off -- optionally with a message, as `("skip", "maintenance window")`. Use
`PreRunStatus` from `soliplex.agents.manifest.pre_run` for the values.

- **A skipped run does nothing:** no component, no stale reconcile, no
  pre-processing, and no haiku load -- so no post-process either. The next
  scheduled run happens as normal. `si-agent manifest run` prints the
  manifest as `SKIPPED by <method>: <message>` and still exits `0`.
- **`on_error`** decides what a step that raises (or outlives its `timeout`)
  means: `fail` (the default) fails the manifest like a crashing component;
  `skip` turns it into a skip; `continue` logs it and moves on.
- **`timeout`** (seconds, default 300, `null` for none) keeps a slow webhook
  from holding up the single-worker manifest queue. An async step is
  cancelled; a sync step runs in a thread and cannot be interrupted, so the
  wait is abandoned while the thread finishes.
- **Outcomes are kept** in the source's state DB for the last 100 runs:
  `si-agent manifest pre-run-report <path|all> [--status skip] [--since <iso>]`
  answers "why didn't this source update last night?".

Built-in steps (`soliplex.agents.manifest.pre_run_steps`):

- **`notify_webhook`** POSTs `{"event": "manifest.started", "manifest_id",
  "manifest_name", "source", "started_at", "download_uri"}` as JSON. Give
  `url`, or `url_secret` naming a docker secret / env var that holds it
  (incoming-webhook URLs are credentials); `headers` are sent as written,
  `secret_headers` values are resolved like an SCM `auth_token`. A non-2xx
  response raises, so pair it with `on_error: continue`.
- **`check_free_space`** skips the run when the download directory (local
  store) or the pre-process spool directory has less than `min_free_mb` free
  (`include_spool: false` checks only the former). An S3 store has nothing to
  check and is reported as such.

Writing your own is a few lines -- for example, a maintenance-window flag:

```python
from pathlib import Path

from soliplex.agents.manifest.pre_run import PreRunStatus


def unless_paused(context, *, flag="/etc/ingester/paused"):
    if Path(flag).exists():
        return PreRunStatus.SKIP, f"paused by {flag}"
    return PreRunStatus.CONTINUE
```

#### Pre-process steps

`config.pre_process` runs on **each new or changed document** -- unchanged
documents are never fetched, so they are never pre-processed -- after it is
downloaded and **before** it is stored. Every agent writes through the same
call, so the steps apply to `fs`, `scm`, `webdav` and `web` components alike.

```yaml
config:
  pre_process:
    - method: soliplex.agents.manifest.pre_processors:check_pdf_password
      mime_types: [application/pdf]
      kwargs: { skip_invalid: true, skip_owner_restricted: false }
```

Each document is spooled to a private temp directory first, and each step is
called as `method(document, **kwargs)`. `document` carries `source`, `uri`,
`key` (its path in the store), `mime_type`, `path` (the spooled file -- treat
it as read-only), `workdir` (scratch space for output), `sha256` (of `path`),
and `read_bytes()`. Steps therefore see a local file whichever store the
document is headed for, so path-based tools (qpdf, ocrmypdf, pdfium by path)
work unchanged with S3. A step that also accepts `context` receives the
source's resolved store.

A step answers with one of:

| Return | Effect |
| --- | --- |
| `"continue"` / `None` | Nothing to do. |
| `"modified"`, with new content | Later steps see the new content, and it is what gets stored (and described by the `.meta.json`). Logged at INFO. |
| `"skip"` | The document is **not stored**. Any version already stored at its path is deleted (the next load drops it from the index). Later steps do not run. Logged at INFO. |

Any of these may carry a message for the log and the audit:
`return PreProcessStatus.SKIP, "password protected"`. MODIFIED needs the new
content, so it is returned as a `PreProcessResult`:

```python
from soliplex.agents.manifest.pre_process import PreProcessResult
from soliplex.agents.manifest.pre_process import PreProcessStatus


def redact(document):
    text = document.read_bytes().decode("utf-8")
    cleaned = text.replace("CONFIDENTIAL", "")
    if cleaned == text:
        return PreProcessStatus.CONTINUE
    return PreProcessResult(PreProcessStatus.MODIFIED, "removed markings", data=cleaned.encode("utf-8"))
```

Give exactly one of `data=` (bytes) or `path=` (a file the step wrote,
normally under `document.workdir`; relative paths resolve against it). A
MODIFIED whose content is byte-for-byte unchanged counts as CONTINUE.
`metadata={...}` is merged into the document's `.meta.json` under
`metadata.pre_process.<method>`.

- **`mime_types`** limits a step to documents of those detected types --
  the type the document is stored under, not its URI's extension (see [File
  Typing and Filtering](#file-typing-and-filtering)). Omit it to run on
  everything.
- **Nothing runs unless listed.** There are no default steps: a manifest
  without `pre_process` stores every document as fetched. To check PDFs or
  fix AsciiDoc, list the built-in steps below.
- **`on_error`** decides what a step that raises (or returns something
  invalid) means: `continue` (the default) logs it and keeps the document as
  it stood before that step; `skip` skips the document; `fail` fails that
  document's write, which the agent records like any download error -- no
  state row, retried next run, and the stale reconcile is skipped for the run.
- **A skipped document still gets its state row,** so it is not fetched again
  until it changes upstream (the reconcile tolerates its absence). After
  changing a step, `si-agent manifest reprocess <path|all> [--status
  skip|modified|continue|error|all] [--method <dotted>] [--dry-run]` forgets
  the matching documents so the next run fetches and checks them again (the
  default is `--status skip`). For an incremental SCM source this also costs
  one full listing.
- **Steps run one at a time** per run, even while webdav downloads
  concurrently -- pdfium is not thread-safe, and it bounds the spool to about
  one document. Sync steps run in a worker thread so the event loop stays
  responsive.
- **The spool** is `PRE_PROCESS_SPOOL_DIR` (default: the system temp dir) and
  needs room for the largest document. In containers `/tmp` is often tmpfs
  (RAM); point it at a volume for large corpora. Agents still hold each
  downloaded document in memory, so spooling does not lower peak memory.

Built-in steps (`soliplex.agents.manifest.pre_processors`):

- **`check_pdf_password`** skips PDFs pdfium cannot open without a password
  (`password protected`). Other open failures (truncated, not a PDF) are
  skipped as `unreadable PDF: ...` unless `skip_invalid: false`. A PDF
  encrypted with an owner password only opens fine -- printing or copying may
  be restricted -- so it is kept, with a message, unless
  `skip_owner_restricted: true`.
- **`fix_asciidoc`** rewrites AsciiDoc that docling's parser cannot handle:
  block attribute lines before a table, cell-format specifiers before pipes,
  `include::` / `image::` directives, and blank lines inside tables.

##### Inspecting pre-process outcomes

Every pre-processed write is recorded in the source's state DB:

- `pre_process_documents` -- the latest outcome per document: status,
  deciding method and message, `input_sha256` (the downloaded bytes),
  `output_sha256` (what was stored; empty when skipped),
  `previous_input_sha256` and `hash_changed_at`;
- `pre_process` -- the latest outcome per (document, step), with each step's
  input and output hash.

The hashes are SHA-256 of the bytes pre-processing saw and stored -- not the
upstream hash the agents use for change detection (SHA3-256 for SCM, and
sometimes absent for webdav). A re-fetch with identical content keeps
`hash_changed_at`; a real change records the old hash and logs
`content of <uri> changed (<old> -> <new>)`, and a changed document that is
skipped again logs `new version of <uri> still skipped`. Audit rows are
removed with the document's state row.

```bash
# Everything skipped, and why
si-agent manifest pre-process-report all --status skip
# Documents whose content changed since a date
si-agent manifest pre-process-report my-manifest.yml --changed-since 2026-10-01
# Every "password protected" document
si-agent manifest pre-process-report all --message "password protected" --json
```

#### Post-process callbacks

A manifest's `config.post_process` is an ordered list of callbacks invoked
**after** the haiku-rag load for that source finishes (and after its summary
has been streamed) — **whether the load succeeded, failed, or timed out**. Each
entry names a `method` (a dotted import path) and optional `kwargs`; the
callback is invoked as `method(source, **kwargs)` — `source` is the manifest's
source and `kwargs` are the configured extra args.

```yaml
config:
  haiku_config: haiku.rag.custom.yaml
  post_process:
    - method: soliplex.agents.manifest.post_processors:vacuum
      kwargs: { timeout: 1800 }
    - method: your_project.postprocess:identify_and_apply
      kwargs: { only_missing: true, overrides: /etc/agent/overrides.json }
```

- **Dotted path:** `pkg.mod:func` or `pkg.mod.func`. The module must be
  importable in the agent's environment.
- **Ordering:** steps run sequentially in the order listed.
- **Config auto-inject:** when a step omits `config` and the callable accepts
  one (an explicit `config` parameter or `**kwargs`), the manifest's resolved
  haiku config path is passed so the callback opens the store with the same
  config the load used. While the callbacks run, `SOURCE`, `DOWNLOAD_DIR` and
  `DOWNLOAD_URI` are set in the environment (as they are for the load
  subprocess) and restored afterwards, so a config interpolating them loads
  in-process too. Other `${VAR}` references must be present in the inherited
  environment — see [Variables the haiku-rag config
  needs](#variables-the-haiku-rag-config-needs).
- **Context auto-inject:** likewise for a `context` parameter, which receives
  the run's `LoadContext` — the resolved download target, document store and
  sidecar facade for this source. A callback that needs to read what the
  manifest just downloaded takes `context` instead of rediscovering the
  storage layout from the environment.
- **Load outcome (`ingester`):** callbacks fire regardless of the load
  result. The load's outcome is auto-injected as an `ingester` kwarg for
  callables that accept one: a `HaikuRun` with `returncode` (`0` on success,
  non-zero on failure, `None` on timeout), `timed_out`, and the last 1 MiB of
  `stdout` / `stderr` (the full output is in the log, in parts). The exit code
  alone is still injected as `ingester_exit_code`, so existing callbacks keep
  working.
- **Run outcome (`run_result`):** likewise, the result of the manifest run
  that queued the load -- its `summary` counts, per-component results,
  `pre_run` and `pre_process` outcomes. Under the server, loads are queued, so
  a later run of the same manifest may already have started by the time a
  load's callbacks fire.
- **Terminate on error:** a step that raises is logged and the exception
  propagates — the remaining steps do **not** run. The per-step outcomes are
  returned under the load result's `post_process` key only when every step
  succeeds. In a batch/directory run the failure is isolated to that manifest
  (recorded as `haiku_load_error` on its result); the other manifests still run.
- **Requires a load:** post-process only runs when a load runs — it is skipped
  with `--no-load`.

Built-in callbacks (`soliplex.agents.manifest.post_processors`):

- **`vacuum`** runs LanceDB maintenance (optimize + clean up table history) on
  the per-source database. It shells out to `haiku-rag vacuum` as a subprocess
  (like the load) — keeping LanceDB's async runtime out of the agent's event
  loop and making the pass killable via its `timeout` kwarg (default 1800s),
  so a stuck compaction can't hang the run. Retention comes from the haiku
  config's `storage.vacuum_retention_seconds`.
- **`backfill_metadata`** runs the same subprocess as `si-agent manifest
  backfill-metadata` for the manifest's source, right after its load, so
  nothing else is writing the database. Takes `missing` and `content_types`
  (lists), `doc_filter`, `full`, `database`, `batch_size`, `attachments`
  (default `true`) and `timeout` (default 1800s). It must be scoped -- `missing` or `doc_filter` -- or given
  `full: true`, since it runs after every load. Documents it could not fill
  are logged and do not stop the chain; the subprocess crashing or timing out
  raises. See [Back-filling metadata](#back-filling-metadata).
- **`notify_webhook`** POSTs `{"event": "load.finished", "source", "status",
  "returncode", "timed_out", "summary", "manifest_id"}`, where `status` is
  `ok`, `failed`, `timed_out` or `no_load`, plus `stderr_tail` (the last
  `stderr_lines`, default 20) when the load failed or timed out. It takes the
  same `url` / `url_secret` / `headers` / `secret_headers` as the
  [pre-run](#pre-run-steps) version. A failed delivery raises, which stops the
  chain, so list it last.

**Note:** All commands support WebDAV credentials via environment variables (`WEBDAV_URL`, `WEBDAV_USERNAME`, `WEBDAV_PASSWORD`) or command-line options (`--webdav-url`, `--webdav-username`, `--webdav-password`).

**Git Bash on Windows:** If using Git Bash on Windows, use double slashes for WebDAV paths to prevent path conversion (e.g., `//documents` instead of `/documents`).

## How It Works

### Document Ingestion Flow

1. **Discovery**: Files are discovered from the source (filesystem, WebDAV, SCM, or web)
2. **Hashing**: Each file's hash is calculated
   - Filesystem/WebDAV/Web sources: SHA256 hash
   - SCM sources: SHA3-256 hash for files, SHA256 for issues
3. **Status Check**: The system checks which files are new or changed against the local sync state, so only new or changed files are processed
4. **Write**: Each file is written to `<DOWNLOAD_DIR>/<source>/<source-relative-path>`, with a `<filename>.meta.json` sidecar (see [Metadata Sidecars](#metadata-sidecars)). The stored filename is given the extension implied by its detected MIME type (added when missing, replaced when it mismatches, left alone when already correct) — see [File Typing and Filtering](#file-typing-and-filtering)
   - **Pre-process** (manifest runs): before the write, the manifest's `pre_process` steps check or rewrite the document; a skipped document is not written, and any earlier stored version is removed (see [Pre-process steps](#pre-process-steps))
5. **State Update**: Content hashes (and, for SCM, the latest commit SHA) are recorded in local state
6. **Stale Removal** (optional): When `delete_stale` is enabled, the download folder is reconciled against the source — documents no longer present (dropped from the listing, or 404 on fetch) are deleted, along with untracked orphan files (see [Stale Document Removal](#stale-document-removal))
7. **haiku-rag Load** (optional): When `HAIKU_LOAD_ENABLED` is set, the downloaded documents are indexed into a per-source LanceDB database via `haiku-ingester` (see [haiku-rag Loading](#haiku-rag-loading))

### Metadata Sidecars

Every downloaded document is accompanied by a `<filename>.meta.json` sidecar
written next to it. The sidecar records:

| Field | Description |
| --- | --- |
| `mime_type` | Detected MIME type (see [File Typing and Filtering](#file-typing-and-filtering)) |
| `source` | Source identifier (the per-source folder name) |
| `source_uri` | Source URI the document was discovered at |
| `ingestion_type` | Method used to fetch the document: `fs`, `webdav`, `scm`, or `web` |
| `sha256` | SHA256 of the written bytes |
| `size` | Size of the written bytes |
| `metadata` | Any additional source-specific metadata; pre-process steps add theirs under `metadata.pre_process.<method>` |
| `source_url` | Full URL the document was fetched from (see below) |
| `downloaded_time` | When the document was last written, ISO 8601 with a UTC offset (see below) |

Example sidecar for a WebDAV download:

```json
{
  "mime_type": "text/markdown",
  "source": "webdav:docs",
  "source_uri": "handbook/readme.md",
  "ingestion_type": "webdav",
  "sha256": "…",
  "size": 1234,
  "metadata": {},
  "source_url": "https://dav.example.com/docs/handbook/readme.md",
  "downloaded_time": "2026-09-11T14:22:05.123456+00:00"
}
```

#### `source_url`

Every agent records one, but what it points at differs by source:

| Agent | `source_url` |
| --- | --- |
| `webdav` | The server URL joined with the document's path |
| `web` | The **requested** page URL. Redirects are followed when fetching, but the requested URL is what is recorded -- it is the stable identifier, and the one sync state is keyed on |
| `fs` | A `file://` URL for the resolved source path. Only meaningful on the host that ran the ingest, which is the only address a local document ever had |
| `scm` | The provider's browsable `html_url` for the file or issue, falling back to the contents API `url` when the provider did not return one |

The field is omitted entirely when an agent has no URL to record -- for
example an SCM provider whose response carried neither key. Consumers should
treat it as optional.

The `soliplex-sidecar-metadata` haiku-rag metadata provider copies the
sidecar into each document's haiku-rag metadata at load time; see [Document
Metadata in haiku-rag](#document-metadata-in-haiku-rag).

#### `downloaded_time`

Records when the document's bytes were last **written**, not when they were
last checked for changes. A document that passes its source's freshness check
(an unchanged content hash, a matching WebDAV ETag) is never rewritten, so its
sidecar keeps the timestamp of the fetch that did produce it.

This also means sidecars written before the field existed will never gain one,
since nothing rewrites an unchanged document. Treat it as optional too.

### Incremental Sync (SCM Agent)

An `scm` manifest component with `incremental: true` uses commit-based tracking for efficient synchronization:

1. **Sync State Check**: Retrieves last processed commit SHA from local state
2. **Commit Enumeration**: Fetches only commits since the last sync
3. **Change Detection**: Extracts changed and removed file paths from commits
4. **Selective Fetch**: Downloads only files that were modified
5. **Write**: Writes changed files to `DOWNLOAD_DIR` and deletes removed ones
6. **State Update**: Stores the latest commit SHA locally for subsequent syncs

This approach reduces API calls and bandwidth by 80-95% compared to full repository scans. On first run (or after `si-agent scm reset-sync`), a full sync is performed to establish the baseline.

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

**Ingest a checkout's docs directory:**

```bash
git clone https://github.com/soliplex/soliplex.git
```

```yaml
# soliplex-docs.yml
id: soliplex-docs
name: soliplex docs
source: soliplex-docs
components:
  - name: docs
    type: fs
    path: <path-to-checkout>/soliplex/docs
```

```bash
# Set up environment
export DOWNLOAD_DIR=./downloads

uv run si-agent manifest run soliplex-docs.yml --no-load

# Files land under ./downloads/soliplex-docs/, each with a .meta.json sidecar
ls ./downloads/soliplex-docs
```

**Reviewing first:**

```bash
# Preview the inventory as JSON (writes nothing)
uv run si-agent fs build-config <path-to-checkout>/soliplex/docs

# Check which files are supported
uv run si-agent fs validate-config <path-to-checkout>/soliplex/docs
# If there are errors, fix them now

uv run si-agent manifest run soliplex-docs.yml --no-load
```

### Example 2: Ingest GitHub Repository

```yaml
# soliplex-repo.yml
id: soliplex-repo
name: soliplex repo
source: soliplex-repo
components:
  - name: soliplex
    type: scm
    platform: github
    owner: mycompany
    repo: soliplex
```

```bash
# Set up environment
export DOWNLOAD_DIR=./downloads
export scm_auth_token=ghp_your_token_here

# Write repository contents
si-agent manifest run soliplex-repo.yml --no-load

# Files land under ./downloads/soliplex-repo/
ls ./downloads/soliplex-repo
```

### Example 3: Ingest from WebDAV Server

```yaml
# webdav-docs.yml
id: webdav-docs
name: webdav docs
source: webdav-docs
components:
  - name: project-docs
    type: webdav
    url: https://nextcloud.example.com/remote.php/dav/files/username
    path: /Documents/project-docs
```

```bash
# Set up environment
export DOWNLOAD_DIR=./downloads
export WEBDAV_USERNAME=your-username
export WEBDAV_PASSWORD=your-password

si-agent manifest run webdav-docs.yml --no-load

# Files land under ./downloads/webdav-docs/
ls ./downloads/webdav-docs
```

### Example 4: Index Ingested Documents with haiku-rag

```bash
# The haiku-rag config places the database; copy the example and keep its
# `lancedb.databases` entry interpolating ${SOURCE}.
mkdir -p ./haiku-config
cp example-haiku-configs/haiku.rag.default.yaml ./haiku-config/

# Set up environment. LANCEDB_DIR is what that config interpolates.
export DOWNLOAD_DIR=./downloads
export LANCEDB_DIR=./lancedb
export HAIKU_PATH=./haiku-config
export HAIKU_LOAD_ENABLED=true

# Run a manifest and load the result into ./lancedb/<source>.lancedb
si-agent manifest run /path/to/manifest.yml --load
```

The example config also interpolates `${STATE_DIR}` and the model/service URLs
(`QA_MODEL`, `QA_BASE_URL`, `OLLAMA_BASE_URL`, `DOCLING1_BASE_URL`,
`DOCLING2_BASE_URL`, `EMBEDDINGS_BASE_URL`); every one must be exported too, or
the load fails on the missing variable.

### Document Metadata in haiku-rag

`haiku-ingester` reads only the document bytes. This package registers haiku-rag
[metadata providers](https://github.com/ggozad/haiku.rag/blob/main/docs/ingester.md#metadata-providers)
that add more to each document's haiku-rag metadata. A haiku source names one
`metadata_provider`, so pick the one covering what you want:

| Provider | Adds |
| --- | --- |
| `soliplex-sidecar-metadata` | The document's [`.meta.json` sidecar](#metadata-sidecars), flattened |
| `soliplex-pdf-metadata` | A PDF's page count and document information |
| `soliplex-metadata` | Both; the sidecar is applied last, so manifest metadata wins a clash |

haiku-rag does not call providers for the PDF attachments it extracts into
documents of their own; [back-fill](#pdf-attachments) them.

Both example configs in `example-haiku-configs/` name `soliplex-metadata`:

```yaml
  sources:
    - type: fs
      id: ${SOURCE}
      root: ${DOWNLOAD_DIR}/${SOURCE}
      metadata_provider: soliplex-metadata
```

#### Sidecar metadata

`soliplex-sidecar-metadata` reads the sidecar the manifest run wrote beside
the document and returns it flattened, as
[Metadata Sidecars](#metadata-sidecars) describes: `mime_type`, `source`,
`source_uri`, `ingestion_type`, `sha256`, `size`, `source_url` and
`downloaded_time` when set, and the manifest's `metadata` entries at the top
level (nested values JSON-encoded). It finds the download store the same way
the agent does: the haiku source's `id` is the sanitized manifest source
(`${SOURCE}`), and `DOWNLOAD_DIR` / `DOWNLOAD_S3_*` come from the load's
environment, so it works on the local and S3 stores alike.

A missing, unreadable or malformed sidecar gives the document no sidecar keys
and a log line; it never fails the document. A sidecar that changes while its
document does not -- new manifest metadata -- is not picked up by a load,
because haiku-rag skips an unchanged document before calling any provider
(and does not ingest `*.meta.json` itself). Back-fill those with a
`--filter`, or a pass without `--missing`.

#### PDF metadata

| Key | Value |
| --- | --- |
| `page_count` | Number of pages (an integer) |
| `pdf_version` | PDF version from the header, e.g. `"1.7"` |
| `pdf_title`, `pdf_author`, `pdf_subject`, `pdf_keywords`, `pdf_creator`, `pdf_producer` | The PDF's document information entries |
| `pdf_creation_date`, `pdf_mod_date` | The PDF's dates, as ISO 8601 (kept as written if they do not parse) |

Only `page_count` is always present; an information entry the PDF leaves
empty is left out. A document is a PDF when its content type is
`application/pdf` or its first 1024 bytes hold a `%PDF-` header; anything else
gets no keys. A PDF pdfium cannot open (password protected, truncated) gets no
keys either, and a warning is logged -- the provider never fails the document.

The provider runs inside `haiku-ingester`, so this package must be installed
in the environment that runs it (the `haiku_load_command` default runs it from
the agent's own). haiku-rag calls a provider only when it fetches a new or
changed document: after enabling it, existing documents gain the keys the next
time they change, unless you back-fill them -- `--missing source_uri` for the
sidecar keys (every sidecar has one), `--missing page_count` for the PDF keys.

#### Back-filling metadata

`backfill-metadata` adds a provider's keys to documents indexed before the
provider was configured, without re-ingesting them -- nothing is converted,
chunked or embedded:

```bash
# What would change, without writing anything
si-agent manifest backfill-metadata --missing page_count --check

# Every manifest in $MANIFEST_DIR, PDFs still lacking a page count
si-agent manifest backfill-metadata --missing page_count --content-type application/pdf

# One manifest; print the command instead of running it
si-agent manifest backfill-metadata /manifests/handbook.yml --missing page_count --dry-run
```

For each source in the haiku config that names a `metadata_provider`, it
lists the documents that source ingested, fetches each selected one again
through that source (so the provider sees what the ingester would hand it),
calls the provider, and merges the keys it returns into the document's
metadata. The provider is the one code path: the ingester calls it for new
documents, the back-fill for old ones.

Which documents run:

- **Scoped in LanceDB.** `--missing KEY` becomes a `WHERE` clause on the
  stored metadata, `(metadata IS NULL OR metadata NOT LIKE '%"KEY"%' ...)`, so
  a document that already has every key is never listed, let alone fetched.
  `--filter` adds a clause of your own (AND-ed with it), e.g.
  `--filter "uri LIKE '%/reports/%'"`. A key may only contain letters, digits,
  `_`, `.` and `-`.
- **Then by type.** Of those, a document runs when its stored `content_type`
  is one of the `--content-type` values, if any are given.
- **Unscoped, everything.** With neither `--missing` nor `--filter` every
  document of the source is fetched again. Documents whose metadata the
  provider would not change are never written either way.
- **Database.** The haiku config must place the database
  (`lancedb.databases`), as the load's does; `--db-name` picks one when it
  places several. `--batch-size` (default 500) sizes the listing's pages, all
  of which are read before the first write.
- **Stale documents are left alone.** A document whose bytes changed since it
  was indexed (its stored `md5` differs) is counted as `stale`: the next load
  re-ingests it, which runs the provider anyway.
- **Providers can opt out.**  Its documents
  are counted as skipped and it is never called: run over stored documents it
  would record the time of the back-fill. Third-party providers of that kind
  should do the same.
- **Never alongside a load.** It writes the same database the load does, so
  do not run the CLI verb while a load for that source is in progress. As a
  post-process step it runs after the load by construction, and must be
  scoped (`missing` or `doc_filter`) or given `full: true`, so that a
  scheduled load with nothing new does not fetch every document again:

  ```yaml
  config:
    post_process:
      - method: soliplex.agents.manifest.post_processors:backfill_metadata
        kwargs:
          missing: [page_count]
          content_types: [application/pdf]
  ```

##### PDF attachments

haiku-rag stores each file embedded in a PDF as a document of its own:
`<parent uri>#attachment=<percent-encoded name>` (nested ones chaining
fragments), linked by `parent_uri` and owned by **no** source. It does not
call a metadata provider for them, and no source can fetch that URI, so the
back-fill derives each one from its parent instead:

1. walk `parent_uri` up to the top-level document, which a source owns;
2. fetch it once through that source, however many attachments it has;
3. extract the attachment from it, following nested ones down, exactly as
   haiku-rag does (same URI, content type and MD5);
4. call that source's provider with the attachment's bytes and
   `extra_metadata["parent_uri"]` set, and merge as for any document.

So `soliplex-pdf-metadata` gives an attached PDF its **own** page count, and
`soliplex-sidecar-metadata` gives every attachment its **parent's** sidecar:
an attachment has no download of its own. An attachment never gains a
`source_id`, and no provider can change its `parent_uri`. An attachment whose
top-level document changed since it was indexed, or whose stored `md5` no
longer matches what the parent embeds, is `stale`. One whose parent is no
longer indexed, or no longer embeds it, is `orphaned`: haiku-rag removes such
a child only when it re-ingests the parent, and not at all when the new
parent cannot be opened, so these are worth a look. `--no-attachments`
(`attachments: false`) leaves attachments alone.

##### Outcome

The outcome line reports what it did, e.g.
`(scanned 6: 3 updated (2 attachments), 1 unchanged, 0 stale, 1 orphaned,
1 skipped, 0 errors)` -- under `--check`, `would update`; `--json` has the full
counts. `skipped` covers documents no source with a provider owns
(`haiku-rag add-src` documents and their attachments), providers that opt out,
and ones `--content-type` or the exact `--missing` check left out. A document that fails (gone from the store, provider raised) is listed
and the run continues; the subprocess then exits 3 and the verb exits 1. A PDF
the provider cannot read gets no keys and counts as `unchanged`, so
`--missing page_count` selects it again on every run.

## Server API

The agents can be run as a REST API server using FastAPI. The server runs manifests (on a cron schedule, and on demand via `POST /api/v1/manifest/run`) and exposes read-only inspection routes for each source type, with support for authentication and interactive documentation. Ingestion over HTTP goes only through manifests.

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
scheduler keeps its cron state and run queue in memory, so running
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
curl -H "Authorization: Bearer your-api-key" http://localhost:8001/api/v1/manifest/queue
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

#### Manifest Routes (`/api/v1/manifest/`)

The only way to ingest over HTTP. See [On-demand Runs](#on-demand-runs).

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/manifest/run` | Queue a manifest from `MANIFEST_DIR` by id; returns 202 |
| `GET` | `/api/v1/manifest/queue` | Ids of manifests queued or running |
| `POST` | `/api/v1/manifest/validate` | Validate manifests without executing |

`POST /run` responds `202` with `"status": "queued"` or `"already_queued"`;
`404` if no valid manifest in `MANIFEST_DIR` has that id; `409` if more than
one file declares it; `503` if `MANIFEST_DIR` is unset or not a directory.

**Examples:**

```bash
# Queue a manifest run
curl -X POST http://localhost:8001/api/v1/manifest/run \
  -F "manifest_id=test-scm"

# What is still queued or running
curl http://localhost:8001/api/v1/manifest/queue

# Validate manifest files
curl -X POST http://localhost:8001/api/v1/manifest/validate \
  -F "path=/path/to/manifests"
```

#### Filesystem Routes (`/api/v1/fs/`)

Read-only inspection of a server-side directory.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/fs/build-config` | Build inventory from directory |
| `POST` | `/api/v1/fs/validate-config` | Validate the inventory built from a directory |
| `POST` | `/api/v1/fs/check-status` | Check which files need ingestion |

**Examples:**

```bash
# Build configuration from directory
curl -X POST http://localhost:8001/api/v1/fs/build-config \
  -F "path=/path/to/docs"

# Validate using a directory
curl -X POST http://localhost:8001/api/v1/fs/validate-config \
  -F "config_file=/path/to/docs"
```

#### SCM Routes (`/api/v1/scm/`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/scm/{scm}/issues` | List repository issues |
| `GET` | `/api/v1/scm/{scm}/repo` | List repository files |

`{scm}` is `github` or `gitea`; both take `repo_name` and `owner` query
parameters.

**Examples:**

```bash
# List GitHub issues
curl "http://localhost:8001/api/v1/scm/github/issues?repo_name=my-repo&owner=myuser"

# List repository files
curl "http://localhost:8001/api/v1/scm/github/repo?repo_name=my-repo&owner=myuser"
```

#### WebDAV Routes (`/api/v1/webdav/`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/webdav/validate-config` | Validate inventory from WebDAV path |
| `POST` | `/api/v1/webdav/check-status` | Check which files need ingestion |

**Example:**

```bash
# Validate using WebDAV path
curl -X POST http://localhost:8001/api/v1/webdav/validate-config \
  -F "config_path=/documents" \
  -F "webdav_url=https://webdav.example.com"
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
│   ├── haiku_metadata.py   # haiku-rag metadata providers (sidecar, PDF, combined)
│   ├── haiku_backfill.py   # Re-runs metadata providers over indexed documents
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
│   │   ├── haiku_loader.py # haiku-ingester batch load subprocess
│   │   ├── haiku_maint.py  # haiku-rag migrate/vacuum/backfill-metadata subprocesses
│   │   ├── post_processors.py  # Built-in post-process callbacks
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
├── example-haiku-configs/  # Example haiku-rag configs for the load step (local, S3)
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

**haiku-rag Layer:**

- `manifest/haiku_loader.py` - The `haiku-ingester run-batch` load after each manifest run
- `manifest/haiku_maint.py` - `migrate` / `vacuum` / `backfill-metadata` subprocesses
- `haiku_metadata.py` - Metadata providers run inside `haiku-ingester` (registered entry points)
- `haiku_backfill.py` - Back-fill subprocess re-running those providers over indexed documents

**Configuration:**

- `config.py` - Pydantic settings and manifest component models
- Environment variables or `.env` file for configuration
- YAML manifest files for declarative multi-source ingestion

## License

See LICENSE file for details.

## Support

For issues and questions, please open an issue on the repository.
