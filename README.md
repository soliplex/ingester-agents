# Soliplex Agents

[![CI](https://github.com/soliplex/ingester-agents/actions/workflows/soliplex.yaml/badge.svg)](https://github.com/soliplex/ingester-agents/actions/workflows/soliplex.yaml)

Agents for ingesting documents into the [Soliplex Ingester](https://github.com/soliplex/ingester) system. This package provides tools to collect, validate, and ingest documents from multiple sources including local filesystems, and source code management platforms (GitHub, Gitea).

## Features

- **Filesystem Agent (`fs`)**: Ingest documents from local directories
  - Recursive directory scanning
  - Automatic SHA256 hash calculation
  - MIME type detection
  - Configuration validation
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
INGESTER_API_KEY=your-ingester-api-key

# GitHub Configuration - if needed
GH_TOKEN=your_github_token_here
GH_OWNER=your_github_username_or_org

# Gitea Configuration - if needed
GITEA_URL=https://your-gitea-instance.com
GITEA_TOKEN=your_gitea_token_here
GITEA_OWNER=admin
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
API_KEY=your-secret-key-here
API_KEY_ENABLED=false
AUTH_TRUST_PROXY_HEADERS=false
```

## Usage

The CLI tool `si-agent` provides three main modes of operation:
- **`fs`**: Filesystem agent for ingesting local documents
- **`scm`**: SCM agent for ingesting from Git repositories
- **`serve`**: REST API server exposing agent functionality via HTTP

### Filesystem Agent

#### 1. Build Configuration

Scan a directory and create an inventory file:

```bash
si-agent fs build-config /path/to/documents
```

This creates an `inventory.json` file containing metadata for all discovered files. This file can be modified with additional metadata if desired.

#### 2. Validate Configuration

Check if files in the inventory are supported:

```bash
si-agent fs validate-config /path/to/inventory.json
```

#### 3. Check Status

See which files need to be ingested:

```bash
si-agent fs check-status /path/to/inventory.json my-source-name
```

Add `--detail` flag to see the full list of files:

```bash
si-agent fs check-status /path/to/inventory.json my-source-name --detail
```

The status check compares file hashes against the Ingester database:
- **new**: File doesn't exist in the database
- **mismatch**: File exists but content has changed
- **match**: File is unchanged (will be skipped during ingestion)

#### 4. Load Inventory

Ingest documents from an inventory:

```bash
si-agent fs run-inventory /path/to/inventory.json my-source-name
```

**Advanced options:**

```bash
# Process a subset of files (e.g., files 10-50)
si-agent fs run-inventory inventory.json my-source --start 10 --end 50

# Resume a previous batch
si-agent fs run-inventory inventory.json my-source --resume-batch 123

# Start workflows after ingestion
si-agent fs run-inventory inventory.json my-source \
  --start-workflows \
  --workflow-definition-id my-workflow \
  --param-set-id my-params \
  --priority 10
```

You can also pass a directory path directly (it will build the config automatically):

```bash
si-agent fs run-inventory /path/to/documents my-source-name
```

### SCM Agent

#### 1. List Issues

List all issues from a repository:

```bash
# GitHub
si-agent scm list-issues github my-repo my-github-user

# Gitea
si-agent scm list-issues gitea my-repo --owner admin
```

#### 2. Get Repository Files

List files in a repository:

```bash
# GitHub
si-agent scm get-repo github my-repo my-github-user

# Gitea
si-agent scm get-repo gitea my-repo
```

#### 3. Load Inventory

Ingest **both files and issues** from a repository. Issues are rendered as Markdown documents with their comments.

```bash
# GitHub
si-agent scm run-inventory github my-repo my-github-user

# Gitea
si-agent scm run-inventory gitea my-repo admin
```

**Note on Workflows:** By default, `start_workflows=True`. To skip workflow triggering, explicitly set `--no-start-workflows`.

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

```bash
git clone https://github.com/soliplex/soliplex.git


# Set up environment, check ingester configuration for details
export ENDPOINT_URL=http://localhost:8000/api/v1

# Create inventory and ingest
uv run  si-agent fs build-config <path-to-checkout>soliplex/docs
#you may see messages about ignored files

#if you want to update the inventory.json file, do it here

uv run  si-agent fs validate-config <path-to-checkout>soliplex/docs/inventory.json
#if there are errors, fix them now

uv run  si-agent fs run-inventory <path-to-checkout>soliplex/docs soliplex-docs

#check that documents are in the ingester: (your batch id may be different)
curl -X 'GET' \
  'http://127.0.0.1:8000/api/v1/document/?batch_id=1' \
  -H 'accept: application/json'

```

### Example 2: Ingest GitHub Repository

```bash
# Set up environment
export ENDPOINT_URL=http://localhost:8000/api/v1
export GH_TOKEN=ghp_your_token_here
export GH_OWNER=mycompany

# Ingest repository
si-agent scm run-inventory github soliplex soliplex

#check that documents are in the ingester: (your batch id may be different)
curl -X 'GET' \
  'http://127.0.0.1:8000/api/v1/document/?batch_id=2' \
  -H 'accept: application/json'

```

### Example 3: Batch Processing with Workflows

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
export API_KEY=your-secret-key
export API_KEY_ENABLED=true
si-agent serve
```

Clients must include the API key in the `Authorization` header:
```bash
curl -H "Authorization: Bearer your-secret-key" http://localhost:8001/api/fs/status
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

#### Filesystem Routes (`/api/fs/`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/fs/build-config` | Build inventory from directory |
| `POST` | `/api/fs/validate-config` | Validate inventory file |
| `POST` | `/api/fs/check-status` | Check which files need ingestion |
| `POST` | `/api/fs/run-inventory` | Ingest documents from inventory |

**Example:**
```bash
curl -X POST http://localhost:8001/api/fs/build-config \
  -H "Content-Type: application/json" \
  -d '{"directory_path": "/path/to/docs"}'
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

See [DOCKERFILE_CHANGES.md](DOCKERFILE_CHANGES.md) for implementation details.

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

```
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
│   │       └── scm.py      # SCM API endpoints
│   ├── fs/                 # Filesystem agent
│   │   ├── app.py          # Core filesystem logic
│   │   └── cli.py          # Filesystem CLI commands
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
- `cli.py` - Main entry point with `fs`, `scm`, and `serve` commands
- Agent-specific CLI commands in `fs/cli.py` and `scm/cli.py`

**Server Layer:**
- `server/` - FastAPI application
- `server/auth.py` - Flexible authentication (none, API key, OAuth2 proxy)
- `server/routes/` - REST API endpoints mirroring CLI functionality

**Agent Layer:**
- `fs/app.py` - Filesystem operations (shared by CLI and API)
- `scm/app.py` - SCM operations (shared by CLI and API)
- `client.py` - HTTP client for Soliplex Ingester API

**Configuration:**
- `config.py` - Pydantic settings for all components
- Environment variables or `.env` file for configuration

## License

See LICENSE file for details.

## Support

For issues and questions, please open an issue on the repository.
