# Soliplex Agents

[![CI](https://github.com/soliplex/ingester-agents/actions/workflows/soliplex.yaml/badge.svg)](https://github.com/soliplex/ingester-agents/actions/workflows/soliplex.yaml)

Agents for loading documents into the [Soliplex Ingester](https://github.com/soliplex/ingester) system. This package provides tools to collect, validate, and ingest documents from multiple sources including local filesystems, WebDAV servers, and source code management platforms (GitHub, Gitea).

## Features

- **Filesystem Agent (`fs`)**: Ingest documents from local directories
  - Recursive directory scanning
  - MIME type detection
  - Configuration validation
  - Status checking to avoid re-ingesting unchanged files

- **WebDAV Agent (`webdav`)**: Ingest documents from WebDAV servers
  - Support for any WebDAV-compliant server (Nextcloud, ownCloud, SharePoint, etc.)
  - Recursive directory scanning
  - MIME type detection
  - Authentication support (username/password)
  - Status checking to avoid re-ingesting unchanged files

- **SCM Agent (`scm`)**: Ingest files and issues from Git repositories
  - Support for GitHub and Gitea platforms
  - Automatic file type filtering
  - Issue ingestion with comments (rendered as Markdown)
  - Batch processing with workflow support
  - Status checking to avoid re-ingesting unchanged files

- **REST API Server**: Run agents as a web service
  - FastAPI-based HTTP endpoints for all operations
  - Multiple authentication methods (API key, OAuth2 proxy)
  - Interactive API documentation with Swagger UI
  - Health check endpoint for monitoring
  - Container-ready with Docker support

## Installation

**Requirements:**
- Python 3.13 or higher
- Soliplex Ingester running and accessible

Before using these tools, a working version of [Soliplex Ingester](https://github.com/soliplex/ingester) must be available. The URL will need to be configured in the environment variables to function.

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

```bash
# Soliplex Ingester API endpoint
ENDPOINT_URL=http://localhost:8000/api/v1

# Ingester API authentication (for connecting to protected Ingester instances)
INGESTER_API_KEY=your-api-key
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
```

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

The CLI tool `si-agent` provides four main modes of operation:
- **`fs`**: Filesystem agent for ingesting local documents
- **`scm`**: SCM agent for ingesting from Git repositories
- **`webdav`**: WebDAV agent for ingesting documents from WebDAV servers
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

The status check compares file hashes against the Ingester database:
- **new**: File doesn't exist in the database
- **mismatch**: File exists but content has changed
- **match**: File is unchanged (will be skipped during ingestion)

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

# Start workflows after ingestion
si-agent fs run-inventory /path/to/documents my-source \
  --start-workflows \
  --workflow-definition-id my-workflow \
  --param-set-id my-params \
  --priority 10
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

**Note on Workflows:** By default, `start_workflows=True`. To skip workflow triggering, explicitly set `--no-start-workflows`.

#### 4. Incremental Sync

Run commit-based incremental synchronization. Only processes files that changed since the last sync, significantly reducing API calls and bandwidth usage.

```bash
# First run performs full sync and establishes sync state
si-agent scm run-incremental gitea admin/my-repo

# Subsequent runs only process changes since last sync
si-agent scm run-incremental gitea admin/my-repo --branch main
```

**With workflow triggering:**

```bash
si-agent scm run-incremental gitea admin/my-repo \
  --start-workflows \
  --workflow-definition-id my-workflow \
  --param-set-id my-params \
  --priority 5
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

**1. Build Configuration**

Scan a WebDAV directory and create an inventory file:

```bash
si-agent webdav build-config /documents \
  --webdav-url https://webdav.example.com \
  --webdav-username user \
  --webdav-password pass \
  --output inventory.json
```

**2. Validate Configuration**

Check if files are supported:

```bash
# Validate existing inventory file
si-agent webdav validate-config inventory.json

# Or validate WebDAV directory directly
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

Ingest documents:

```bash
# From WebDAV directory (recommended!)
si-agent webdav run-inventory /documents my-source-name

# Or from local inventory file
si-agent webdav run-inventory inventory.json my-source-name
```

**Advanced options:**

```bash
# Override all configuration via command line
si-agent webdav run-inventory /documents my-source \
  --start-workflows \
  --workflow-definition-id my-workflow \
  --param-set-id my-params \
  --priority 10 \
  --webdav-url https://webdav.example.com \
  --webdav-username user \
  --webdav-password pass \
  --endpoint-url http://custom-ingester:8000/api/v1
```

**Note:** All commands support both local inventory files and direct WebDAV paths. All configuration (WebDAV credentials, Ingester endpoint URL) can be provided via:
- **Environment variables** (recommended for security): `WEBDAV_URL`, `WEBDAV_USERNAME`, `WEBDAV_PASSWORD`, `ENDPOINT_URL`
- **Command-line options** (useful for scripts or testing): `--webdav-url`, `--webdav-username`, `--webdav-password`, `--endpoint-url`

**Git Bash on Windows:** If using Git Bash on Windows, use double slashes for WebDAV paths to prevent path conversion (e.g., `//documents` instead of `/documents`).

## How It Works

### Document Ingestion Flow

1. **Discovery**: Files are discovered from the source (filesystem or SCM)
2. **Hashing**: Each file's hash is calculated
   - Filesystem sources: SHA256 hash
   - SCM sources: SHA3-256 hash for files, SHA256 for issues
3. **Status Check**: The system checks which files have changed or are new against the ingester database
4. **Batch Management**:
   - The system searches for an existing batch matching the source name
   - If found, new documents are added to the existing batch (incremental ingestion)
   - If not found, a new batch is created
   - This enables efficient re-ingestion: only new or changed files are processed
5. **Ingestion**: Files are uploaded to the Soliplex Ingester API
6. **Workflow Trigger** (optional): Workflows can be started to process the ingested documents. See Ingester documentation for details.

### Incremental Sync (SCM Agent)

The `run-incremental` command uses commit-based tracking for efficient synchronization:

1. **Sync State Check**: Retrieves last processed commit SHA from the ingester
2. **Commit Enumeration**: Fetches only commits since the last sync
3. **Change Detection**: Extracts changed and removed file paths from commits
4. **Selective Fetch**: Downloads only files that were modified
5. **Ingestion**: Uploads changed files to the ingester
6. **State Update**: Stores the latest commit SHA for subsequent syncs

This approach reduces API calls and bandwidth by 80-95% compared to full repository scans. On first run (or after reset), a full sync is performed to establish the baseline.

### File Filtering

Both agents filter files by the `EXTENSIONS` configuration. The default extensions are: `md`, `pdf`, `doc`, `docx`.

To add more types:

```bash
export EXTENSIONS=md,pdf,doc,docx,txt,rst
```

It also validates that files have supported content types and rejects:
- ZIP archives
- RAR archives
- 7z archives
- Generic binary files without proper MIME types

#### SCM Agent

The SCM agent only includes files with extensions specified in the `EXTENSIONS` configuration (default: `md`, `pdf`, `doc`, `docx`).

### Issues as Documents

For SCM sources, issues (including their comments) are rendered as Markdown documents and ingested alongside repository files. This enables full-text search and analysis of issue discussions.

## Examples

As an example, the soliplex [documentation](https://github.com/soliplex/soliplex/tree/main/docs)) can be loaded using both the filesystem and via git.

### Example 1: Ingest Local Documents

**Quick version (NEW - no inventory.json needed):**

```bash
git clone https://github.com/soliplex/soliplex.git

# Set up environment
export ENDPOINT_URL=http://localhost:8000/api/v1

# Ingest directly from directory!
uv run si-agent fs run-inventory <path-to-checkout>/soliplex/docs soliplex-docs

# Check that documents are in the ingester (your batch id may be different)
curl -X 'GET' \
  'http://127.0.0.1:8000/api/v1/document/?batch_id=1' \
  -H 'accept: application/json'
```

**Traditional version (with inventory.json):**

```bash
git clone https://github.com/soliplex/soliplex.git

# Set up environment
export ENDPOINT_URL=http://localhost:8000/api/v1

# Create inventory (optional - only if you want to review/modify it)
uv run si-agent fs build-config <path-to-checkout>/soliplex/docs
# You may see messages about ignored files

# If you want to update the inventory.json file, do it here

# Validate configuration
uv run si-agent fs validate-config <path-to-checkout>/soliplex/docs/inventory.json
# If there are errors, fix them now

# Ingest
uv run si-agent fs run-inventory <path-to-checkout>/soliplex/docs/inventory.json soliplex-docs

# Check that documents are in the ingester (your batch id may be different)
curl -X 'GET' \
  'http://127.0.0.1:8000/api/v1/document/?batch_id=1' \
  -H 'accept: application/json'
```

### Example 2: Ingest GitHub Repository

```bash
# Set up environment
export ENDPOINT_URL=http://localhost:8000/api/v1
export scm_auth_token=ghp_your_token_here

# Ingest repository
si-agent scm run-inventory github mycompany/soliplex

#check that documents are in the ingester: (your batch id may be different)
curl -X 'GET' \
  'http://127.0.0.1:8000/api/v1/document/?batch_id=2' \
  -H 'accept: application/json'

```

### Example 3: Ingest from WebDAV Server

```bash
# Set up environment
export ENDPOINT_URL=http://localhost:8000/api/v1
export WEBDAV_URL=https://nextcloud.example.com/remote.php/dav/files/username
export WEBDAV_USERNAME=your-username
export WEBDAV_PASSWORD=your-password

# Ingest directly from WebDAV directory
si-agent webdav run-inventory /Documents/project-docs webdav-docs

# Check that documents are in the ingester (your batch id may be different)
curl -X 'GET' \
  'http://127.0.0.1:8000/api/v1/document/?batch_id=3' \
  -H 'accept: application/json'
```

### Example 4: Batch Processing with Workflows

```bash
# Ingest and trigger processing workflows
si-agent fs run-inventory ./documents my-docs \
  --start-workflows \
  --workflow-definition-id document-analysis \
  --priority 5
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

# Production with multiple workers
si-agent serve --workers 4
```

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

All endpoints accept both local inventory files and WebDAV paths!

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/webdav/build-config` | Build inventory from WebDAV directory |
| `POST` | `/api/v1/webdav/validate-config` | Validate inventory (file or WebDAV path) |
| `POST` | `/api/v1/webdav/check-status` | Check which files need ingestion |
| `POST` | `/api/v1/webdav/run-inventory` | Ingest documents from WebDAV |

**Examples:**

```bash
# Build configuration from WebDAV
curl -X POST http://localhost:8001/api/v1/webdav/build-config \
  -F "webdav_path=/documents" \
  -F "webdav_url=https://webdav.example.com" \
  -F "webdav_username=user" \
  -F "webdav_password=pass"

# Validate using WebDAV path
curl -X POST http://localhost:8001/api/v1/webdav/validate-config \
  -F "config_path=/documents" \
  -F "webdav_url=https://webdav.example.com"

# Ingest directly from WebDAV with custom endpoint
curl -X POST http://localhost:8001/api/v1/webdav/run-inventory \
  -F "config_path=/documents" \
  -F "source=my-source" \
  -F "webdav_url=https://webdav.example.com" \
  -F "webdav_username=user" \
  -F "webdav_password=pass" \
  -F "endpoint_url=http://custom-ingester:8000/api/v1"
```

**Note:** The `endpoint_url` parameter allows you to override the Ingester API endpoint on a per-request basis.

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

The server is designed to run in containers:

```bash
# Build image
docker build -t ingester-agents:latest .

# Run with environment variables
docker run -d \
  -p 8001:8000 \
  -e ENDPOINT_URL=http://ingester:8000/api/v1 \
  -e API_KEY_ENABLED=true \
  -e API_KEY=your-secret-key \
  ingester-agents:latest

# Check health
curl http://localhost:8001/health
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

Verify the `ENDPOINT_URL` is correct and the Ingester API is running:

```bash
curl http://localhost:8000/api/v1/batch/
```

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
│   ├── client.py           # Soliplex Ingester API client
│   ├── config.py           # Configuration and settings
│   ├── server/             # FastAPI server
│   │   ├── __init__.py     # FastAPI app initialization
│   │   ├── auth.py         # Authentication (API key & OAuth2 proxy)
│   │   └── routes/
│   │       ├── __init__.py
│   │       ├── fs.py       # Filesystem API endpoints
│   │       ├── scm.py      # SCM API endpoints
│   │       └── webdav.py   # WebDAV API endpoints
│   ├── fs/                 # Filesystem agent
│   │   ├── app.py          # Core filesystem logic
│   │   └── cli.py          # Filesystem CLI commands
│   ├── webdav/             # WebDAV agent
│   │   ├── app.py          # Core WebDAV logic
│   │   └── cli.py          # WebDAV CLI commands
│   └── scm/                # SCM agent
│       ├── app.py          # Core SCM logic
│       ├── cli.py          # SCM CLI commands
│       ├── base.py         # Base SCM provider interface
│       ├── github/         # GitHub implementation
│       ├── gitea/          # Gitea implementation
│       └── lib/
│           ├── templates/  # Issue rendering templates
│           └── utils.py    # Utility functions
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
- `cli.py` - Main entry point with `fs`, `scm`, `webdav`, and `serve` commands
- Agent-specific CLI commands in `fs/cli.py`, `webdav/cli.py`, and `scm/cli.py`

**Server Layer:**
- `server/` - FastAPI application
- `server/auth.py` - Flexible authentication (none, API key, OAuth2 proxy)
- `server/routes/` - REST API endpoints mirroring CLI functionality

**Agent Layer:**
- `fs/app.py` - Filesystem operations (shared by CLI and API)
- `webdav/app.py` - WebDAV operations (shared by CLI and API)
- `scm/app.py` - SCM operations (shared by CLI and API)
- `client.py` - HTTP client for Soliplex Ingester API

**Configuration:**
- `config.py` - Pydantic settings for all components
- Environment variables or `.env` file for configuration

## License

See LICENSE file for details.

## Support

For issues and questions, please open an issue on the repository.
