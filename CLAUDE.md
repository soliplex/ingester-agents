# AI Assistant Guide for Soliplex Ingester-Agents

## Purpose of This Document

This document is specifically designed for AI assistants (like Claude) working on the Soliplex Ingester-Agents codebase. Unlike README.md which focuses on user-facing documentation, this guide provides internal implementation details, architectural decisions, and development patterns to help AI assistants effectively understand, maintain, and extend the project.

**Critical Facts for AI Assistants:**

- **Project Type:** Python 3.13+ CLI tool for document ingestion using Typer framework
- **Architecture:** Provider-based plugin pattern for extensibility (currently supports GitHub and Gitea)
- **Core Constraint:** 100% branch coverage requirement for all non-CLI code
- **Async Throughout:** All I/O operations use async patterns (aiohttp for HTTP, aiofiles for filesystem)
- **Critical Gotcha:** Filesystem agent uses SHA256 hashing, SCM agent uses SHA3-256 hashing
- **Two Independent Systems:** Filesystem agent (`fs/`) and SCM agent (`scm/`) operate independently
- **Entry Point:** `src/soliplex/agents/cli.py:16` creates the main Typer app with subcommands

---

## Project Overview

### Purpose and Scope

Soliplex Ingester-Agents is a document ingestion system that collects, validates, and ingests documents from multiple sources into the Soliplex Ingester API. The system supports two primary source types:

1. **Local Filesystem:** Recursive directory scanning with inventory-based ingestion
2. **SCM Platforms:** GitHub and Gitea repository file and issue ingestion

The project emphasizes incremental ingestion (only processing new or changed documents), batch management for grouping related documents, and optional workflow triggering for post-ingestion processing.

### Key Capabilities

- **Filesystem Agent:** Scans directories, generates inventory files, validates file types, and ingests local documents
- **SCM Agent:** Fetches files and issues from Git repositories, renders issues as Markdown, and ingests both into the API
- **Status Checking:** Compares local files against server state to avoid re-ingesting unchanged documents
- **Batch Management:** Groups related documents and reuses existing batches for incremental ingestion
- **Workflow Integration:** Optional workflow triggering after successful ingestion for backend processing
- **Extensible Provider Pattern:** Easy to add new SCM platforms beyond GitHub and Gitea

### Tech Stack

**Core Dependencies:**
- **aiohttp** (>=3.13.2) - Async HTTP client for API communication
- **aiofiles** (>=25.1.0) - Async file operations
- **typer** (>=0.20.0) - CLI framework with command routing
- **pydantic-settings** (>=2.12.0) - Environment-based configuration management
- **jinja2** (>=3.1.6) - Template rendering for issues

**Development Tools:**
- **pytest** with **pytest-asyncio** - Testing framework
- **pytest-cov** - Code coverage analysis (100% requirement)
- **ruff** - Linting and formatting

### Development Requirements

- **Python Version:** 3.12 or higher
- **Package Manager:** uv (recommended) or pip
- **Build System:** pyproject.toml with modern Python packaging
- **Test Coverage:** 100% branch coverage enforced via pytest-cov

---

## Architecture & Design Patterns

### High-Level Architecture

```
CLI Entry Point (cli.py)
│
├─── Filesystem Agent (fs/)
│    ├── cli.py ────────> Command handlers
│    └── app.py ────────> Business logic
│         │
│         ├─> read/write inventory.json
│         ├─> scan directories recursively
│         ├─> compute SHA256 hashes
│         └─> call API Client
│
├─── SCM Agent (scm/)
│    ├── cli.py ────────> Command handlers
│    ├── app.py ────────> Orchestration logic
│    │    │
│    │    ├─> get_scm() factory
│    │    ├─> combine files + issues
│    │    └─> call API Client
│    │
│    ├── base.py ───────> BaseSCMProvider (abstract)
│    │    │
│    │    ├─> pagination logic
│    │    ├─> file listing
│    │    └─> issue fetching
│    │
│    ├── github/ ───────> GitHubProvider
│    ├── gitea/ ────────> GiteaProvider
│    └── lib/
│         ├── utils.py ─> SHA3-256, base64 decoding
│         └── templates/> Issue rendering (Jinja2)
│
└─── Shared Components
     ├── client.py ─────> API Client (HTTP to Ingester)
     │    │
     │    ├─> find/create batch
     │    ├─> check status
     │    ├─> ingest documents
     │    └─> start workflows
     │
     └── config.py ─────> Settings (Pydantic)
          │
          └─> Environment variables
```

### Design Patterns

#### 1. Strategy Pattern
**Location:** `src/soliplex/agents/scm/base.py:26-363`

`BaseSCMProvider` defines the interface for SCM providers, while `GitHubProvider` and `GiteaProvider` implement platform-specific behavior. This allows adding new SCM platforms without modifying core logic.

```python
# Abstract interface
class BaseSCMProvider(abc.ABC):
    @abc.abstractmethod
    def get_default_owner(self) -> str: ...

    @abc.abstractmethod
    def get_base_url(self) -> str: ...

# Concrete implementations
class GitHubProvider(BaseSCMProvider):
    def get_base_url(self) -> str:
        return "https://api.github.com"

class GiteaProvider(BaseSCMProvider):
    def get_base_url(self) -> str:
        return settings.gitea_url
```

#### 2. Template Method Pattern
**Location:** `src/soliplex/agents/scm/base.py:74-118`

The `paginate()` method implements a generic pagination algorithm while allowing subclasses to customize response validation via `validate_response()`.

#### 3. Factory Pattern
**Location:** `src/soliplex/agents/scm/app.py:16-22`

The `get_scm()` function returns the appropriate provider instance based on the SCM enum value:

```python
def get_scm(scm) -> BaseSCMProvider:
    if scm == SCM.GITEA:
        return gitea.GiteaProvider()
    elif scm == SCM.GITHUB:
        return github.GitHubProvider()
```

#### 4. Context Manager Pattern
**Location:** `src/soliplex/agents/client.py:22-26`

Async context managers ensure proper resource cleanup for HTTP sessions:

```python
async with get_session() as session:
    async with session.post(url, data=data) as response:
        return await response.json()
```

#### 5. Async Iterator Pattern
**Location:** `src/soliplex/agents/scm/base.py:306-346`

The `iter_repo_files()` method provides memory-efficient streaming of repository files:

```python
async for file in provider.iter_repo_files("repo"):
    process(file)
```

### Component Relationships

- **CLI Layer** routes commands to appropriate agent (fs or scm)
- **Agent Layer** (fs/app.py, scm/app.py) orchestrates workflows and calls API client
- **Provider Layer** (scm/base.py and implementations) abstracts SCM platform differences
- **Client Layer** (client.py) handles all HTTP communication with Ingester API
- **Configuration** (config.py) provides centralized settings accessible to all components

---

## Code Organization

```
C:\src\monkeytronics\enfold\ingester-agents\
│
├── src/soliplex/agents/
│   ├── __init__.py              # Package version, ValidationError exception
│   ├── cli.py                   # Main CLI entry, wires up fs and scm subcommands
│   ├── client.py                # HTTP client for Ingester API
│   ├── config.py                # Pydantic Settings with environment config
│   │
│   ├── fs/                      # Filesystem Agent Module
│   │   ├── __init__.py
│   │   ├── cli.py               # Commands: build-config, validate-config, check-status, run-inventory
│   │   └── app.py               # Core logic: file scanning, SHA256 hashing, ingestion
│   │
│   └── scm/                     # SCM Agent Module
│       ├── __init__.py          # Custom exceptions: SCMException, APIFetchError, GitHubAPIError
│       ├── cli.py               # Commands: list-issues, get-repo, run-inventory
│       ├── app.py               # Orchestration: combines files + issues, triggers ingestion
│       ├── base.py              # BaseSCMProvider abstract class (pagination, file listing)
│       │
│       ├── github/
│       │   └── __init__.py      # GitHubProvider: GitHub API integration
│       │
│       ├── gitea/
│       │   └── __init__.py      # GiteaProvider: Gitea API integration
│       │
│       └── lib/
│           ├── utils.py         # Utilities: SHA3-256 hashing, base64 decoding
│           └── templates/
│               ├── __init__.py  # Jinja2 template loading and rendering
│               └── issue.tpl    # Markdown template for rendering issues
│
├── tests/
│   ├── unit/                    # Unit tests (100% coverage required)
│   │   ├── conftest.py          # Shared fixtures: mock_response, mock_session, sample data
│   │   ├── test_base.py         # Tests for BaseSCMProvider (29 tests)
│   │   ├── test_client.py       # Tests for API client (27 tests)
│   │   ├── test_utils.py        # Tests for utility functions (13 tests)
│   │   └── test_exceptions.py   # Tests for custom exceptions (11 tests)
│   │
│   ├── functional/              # Integration tests (not run by default)
│   │   ├── test_github.py       # GitHub provider integration tests
│   │   └── test_gitea.py        # Gitea provider integration tests
│   │
│   └── test_fs/                 # Test fixtures: sample files for testing
│
├── pyproject.toml               # Project metadata, dependencies, tool configs
├── README.md                    # User-facing documentation
├── LICENSE                      # MIT License
└── CLAUDE.md                    # This file (AI assistant guide)
```

### Key Files by Responsibility

**Entry Points:**
- `src/soliplex/agents/cli.py:16` - Creates main Typer app
- `pyproject.toml:51` - Defines `si-agent` console script

**Configuration:**
- `src/soliplex/agents/config.py:11-22` - Settings class with all environment variables

**API Communication:**
- `src/soliplex/agents/client.py` - All HTTP interactions with Ingester API

**Business Logic:**
- `src/soliplex/agents/fs/app.py` - Filesystem agent implementation
- `src/soliplex/agents/scm/app.py` - SCM agent orchestration
- `src/soliplex/agents/scm/base.py` - SCM provider abstraction

**Extension Points:**
- `src/soliplex/agents/scm/github/__init__.py` - GitHub implementation
- `src/soliplex/agents/scm/gitea/__init__.py` - Gitea implementation

**Test Infrastructure:**
- `tests/unit/conftest.py` - Shared test fixtures and helpers

---

## Key Components Deep Dive

### Configuration Management

**File:** `src/soliplex/agents/config.py`

**Purpose:** Centralized configuration using Pydantic Settings that automatically loads from environment variables.

**Key Classes:**

#### `Settings` (lines 11-22)
Pydantic BaseSettings subclass with automatic environment variable loading:

```python
class Settings(BaseSettings):
    endpoint_url: str = "http://localhost:8000/api/v1"  # Ingester API
    gh_token: str | None = None                          # GitHub token
    gh_owner: str | None = None                          # GitHub default owner
    gitea_url: str | None = None                         # Gitea instance URL
    gitea_token: str | None = None                       # Gitea token
    gitea_owner: str | None = "admin"                    # Gitea default owner
    extensions: list[str] = ["md", "pdf", "doc", "docx"] # Allowed file extensions
    log_level: str = "INFO"                              # Logging level
```

**Usage Pattern:**
```python
from soliplex.agents.config import settings

# Access configuration anywhere in the codebase
api_url = settings.endpoint_url
allowed_exts = settings.extensions
```

**Environment Variable Mapping:**
- `ENDPOINT_URL` → `endpoint_url`
- `GH_TOKEN` → `gh_token`
- `EXTENSIONS` → `extensions` (comma-separated string converted to list)

---

### API Client

**File:** `src/soliplex/agents/client.py`

**Purpose:** HTTP client for all communication with the Soliplex Ingester REST API.

**Key Functions:**

#### `get_session()` (lines 22-26)
Creates authenticated aiohttp session with User-Agent header:

```python
@asynccontextmanager
async def get_session():
    async with aiohttp.ClientSession(headers={"User-Agent": "soliplex-agent"}) as session:
        yield session
```

#### `find_batch_for_source()` (lines 34-46)
**CRITICAL FUNCTION** - Looks for existing batch by source name to enable batch reuse:

```python
async def find_batch_for_source(source: str) -> int | None:
    """Search for existing batch by source name.
    Returns batch_id if found, None otherwise."""
    url = _build_url("/batch/")
    async with get_session() as session:
        async with session.get(url) as response:
            response.raise_for_status()
            batches = await response.json()
            for batch in batches:
                if batch.get("source") == source:
                    return batch["id"]
            return None
```

**Recent Addition:** This function was added to support incremental ingestion by reusing existing batches.

#### `create_batch()` (lines 78-94)
Creates a new batch for grouping documents:

```python
async def create_batch(source: str, name: str | None = None) -> dict[str, Any]:
    """Create a new batch. Returns dict with 'id' or 'error'."""
```

#### `check_status()` (lines 127-166)
Compares local files against server state to determine what needs ingestion:

```python
async def check_status(
    source: str,
    file_list: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Returns only files with status 'new' or 'mismatch'.

    Input: [{uri: "path/file.md", sha256: "hash...", ...}, ...]
    Output: Files that need processing (new or changed)
    """
```

**Status Values:**
- `STATUS_NEW = "new"` - File doesn't exist in database
- `STATUS_MISMATCH = "mismatch"` - File exists but hash differs
- `PROCESSABLE_STATUSES = {STATUS_NEW, STATUS_MISMATCH}` - Files requiring ingestion

#### `do_ingest()` (lines 169-224)
Uploads a document to the Ingester API:

```python
async def do_ingest(
    source: str,
    batch_id: int,
    uri: str,
    doc_body: bytes | str,
    metadata: dict[str, Any]
) -> dict[str, Any]:
    """Ingest a single document. Returns success dict or error dict."""
```

**Form Data Format:**
- `source`: Source identifier
- `batch_id`: Batch grouping ID
- `uri`: Unique document identifier within source
- `document`: File upload (multipart/form-data)
- `metadata`: JSON string with additional metadata

#### `do_start_workflows()` (lines 97-124)
Triggers backend workflow processing for a batch:

```python
async def do_start_workflows(
    batch_id: int,
    workflow_definition_id: str,
    param_id: str | None = None,
    priority: int = 5
) -> dict[str, Any]:
    """Start workflows for all documents in a batch."""
```

---

### BaseSCMProvider

**File:** `src/soliplex/agents/scm/base.py`

**Purpose:** Abstract base class defining the SCM provider interface with shared implementation for common operations.

**Abstract Methods (Must Implement):**

```python
@abc.abstractmethod
def get_default_owner(self) -> str:
    """Return default repository owner from settings."""

@abc.abstractmethod
def get_base_url(self) -> str:
    """Return API base URL (e.g., https://api.github.com)."""

@abc.abstractmethod
def get_auth_token(self) -> str:
    """Return authentication token."""

@abc.abstractmethod
def get_last_updated(self, rec: dict[str, Any]) -> str | None:
    """Extract last updated timestamp from file record."""
```

**Key Concrete Methods:**

#### `get_session()` (lines 53-58)
Creates authenticated HTTP session with Bearer token:

```python
@asynccontextmanager
async def get_session(self):
    headers = {"Authorization": f"Bearer {self.get_auth_token()}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        yield session
```

#### `paginate()` (lines 74-118)
Generic pagination algorithm with optional response processing:

```python
async def paginate(
    self,
    url: str,
    process_response: Callable | None = None
) -> list[Any]:
    """Fetch all pages from paginated endpoint.
    Continues until empty response.
    Optional process_response callback transforms each page."""
```

**Pagination Logic:**
- Starts at page 1
- Fetches pages until response is empty list
- Handles 404 as empty result
- Accumulates results across all pages
- Includes random delay (10-50ms) for rate limiting

#### `list_issues()` (lines 120-142)
Fetches all repository issues with optional comments:

```python
async def list_issues(
    self,
    repo: str,
    owner: str | None = None,
    add_comments: bool = False
) -> list[dict[str, Any]]:
    """Fetch all issues. If add_comments=True, includes comments array."""
```

#### `list_repo_files()` (lines 258-304)
**CORE METHOD** - Recursively fetches all files from a repository:

```python
async def list_repo_files(
    self,
    repo: str,
    owner: str | None = None,
    path: str = "",
    allowed_extensions: list[str] | None = None
) -> list[dict[str, Any]]:
    """Recursively fetch all files, filtering by allowed_extensions.
    Returns list of file dicts with decoded content and SHA3-256 hashes."""
```

**Process:**
1. Fetch directory contents from API
2. Separate files and subdirectories
3. Filter files by allowed extensions
4. Use `asyncio.gather()` to fetch file content and subdirectories concurrently
5. Flatten results into single list
6. Each file dict includes: uri, sha256 (actually SHA3-256), content, metadata

#### `parse_file_rec()` (lines 159-182)
Normalizes file records from API:

```python
async def parse_file_rec(self, rec: dict[str, Any]) -> dict[str, Any]:
    """Decode base64 content, compute SHA3-256 hash, add MIME type."""
```

**Transformations:**
- Decodes base64 content to bytes
- Computes SHA3-256 hash (via `utils.compute_file_hash()`)
- Adds MIME type using Python's `mimetypes` module
- Extracts last updated timestamp (if available)

#### `get_file_content()` (lines 197-214)
Extension point for fetching additional file content:

```python
async def get_file_content(
    self,
    rec: dict[str, Any],
    owner: str,
    repo: str
) -> dict[str, Any]:
    """Override to fetch additional content (e.g., GitHub blob API for large files).
    Default: returns record as-is."""
```

**GitHub Override:** Fetches large files via blob API (see `github/__init__.py:60-80`)

---

### Filesystem Agent

**File:** `src/soliplex/agents/fs/app.py`

**Purpose:** Handles local filesystem document ingestion with inventory-based approach.

**Key Functions:**

#### `build_config()` (lines 62-84)
Scans directory recursively and creates inventory.json:

```python
async def build_config(directory: str) -> None:
    """Scan directory, compute SHA256 hashes, write inventory.json."""
```

**Process:**
1. Call `recursive_listdir()` to get all files
2. Filter by allowed extensions from settings
3. For each file:
   - Read file bytes
   - Compute SHA256 hash using `hashlib.sha256()` (NOT sha3_256!)
   - Detect MIME type with overrides for Office formats
   - Create metadata dict: {size, content-type}
4. Write JSON array to `{directory}/inventory.json`

**MIME Type Overrides** (lines 17-21):
```python
MIME_OVERRIDES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}
```

#### `read_config()` (lines 45-59)
Loads inventory.json with format normalization:

```python
async def read_config(config_path: str) -> list[dict[str, Any]]:
    """Load inventory file. Supports list format or {data: [...]} format.
    Returns sorted list (by file size)."""
```

#### `validate_config()` (lines 24-43)
Validates files in inventory for supported types:

```python
async def validate_config(config_path: str) -> None:
    """Check if all files are supported. Prints summary."""
```

**Rejected Types:**
- `application/zip`
- `application/x-rar`
- `application/x-7z-compressed`
- `application/octet-stream`
- Extensions longer than 4 characters

#### `check_config()` (lines 101-124)
Validates individual file metadata:

```python
def check_config(file_dict: dict[str, Any]) -> tuple[bool, str]:
    """Returns (is_valid, reason)."""
```

#### `load_inventory()` (lines 127-212)
**MAIN INGESTION FUNCTION** for filesystem agent:

```python
async def load_inventory(
    config_or_dir: str,
    source: str,
    start: int | None = None,
    end: int | None = None,
    start_workflows: bool = False,
    workflow_definition_id: str | None = None,
    param_set_id: str | None = None,
    priority: int = 5
) -> dict[str, Any]:
    """Ingest documents from inventory or directory.
    Returns: {ingested: int, errors: int, workflow_result: dict|None}"""
```

**Workflow:**
1. Load config (or build if directory provided)
2. Filter invalid files
3. Apply start/end slice if specified
4. Call `client.check_status()` to get files needing processing
5. Find existing batch or create new one
6. Loop through files:
   - Read file bytes asynchronously
   - Call `client.do_ingest()`
   - Track successes and errors
7. If no errors and `start_workflows=True`, trigger workflows
8. Return summary dict

---

### SCM Agent Orchestration

**File:** `src/soliplex/agents/scm/app.py`

**Purpose:** Orchestrates SCM ingestion by combining repository files and issues.

**Key Functions:**

#### `get_scm()` (lines 16-22)
Factory function returning appropriate provider:

```python
def get_scm(scm: config.SCM) -> base.BaseSCMProvider:
    """Factory for SCM providers."""
    if scm == config.SCM.GITEA:
        return gitea.GiteaProvider()
    elif scm == config.SCM.GITHUB:
        return github.GitHubProvider()
    else:
        raise ValueError(f"Unknown SCM: {scm}")
```

#### `get_data()` (lines 104-145)
**CORE FUNCTION** - Fetches all data from repository (files + issues):

```python
async def get_data(
    scm: config.SCM,
    repo: str,
    owner: str
) -> list[dict[str, Any]]:
    """Fetch repository files and issues, return unified list."""
```

**Process:**

1. **Fetch Repository Files:**
   ```python
   impl = get_scm(scm)
   files = await impl.list_repo_files(repo, owner)
   ```
   - Returns files with SHA3-256 hashes
   - Already filtered by allowed extensions

2. **Filter Files** (redundant but safe):
   ```python
   file_list = [f for f in files if matches_extension(f["uri"], settings.extensions)]
   ```

3. **Fetch Issues with Comments:**
   ```python
   issues = await impl.list_issues(repo, owner, add_comments=True)
   ```

4. **Render Issues as Markdown:**
   ```python
   for issue in issues:
       body = await templates.render_issue(issue, owner, repo)
       # Compute SHA256 (NOT SHA3-256!) for issue content
       sha256_hash = hashlib.sha256(body.encode()).hexdigest()
       issue_list.append({
           "uri": f"/{owner}/{repo}/issues/{issue['number']}",
           "sha256": sha256_hash,
           "body": body,
           "metadata": {...}
       })
   ```

5. **Combine and Return:**
   ```python
   return file_list + issue_list
   ```

**Important:** Issues use SHA256 (line 141), while files use SHA3-256. This inconsistency is not explained in code.

#### `load_inventory()` (lines 25-101)
**MAIN INGESTION FUNCTION** for SCM agent:

```python
async def load_inventory(
    scm: config.SCM,
    repo: str,
    owner: str,
    start_workflows: bool = False,
    workflow_definition_id: str | None = None,
    priority: int = 5
) -> dict[str, Any]:
    """Ingest files and issues from SCM repository.
    Returns: {ingested: int, errors: int, workflow_result: dict|None}"""
```

**Workflow:**
1. Fetch all data via `get_data()`
2. Call `client.check_status()` to filter unchanged items
3. Create source identifier: `{scm}:{owner}:{repo}`
4. Find or create batch
5. Loop through items:
   - Strip internal metadata fields
   - Call `client.do_ingest()` with body bytes
   - Track successes and errors
6. If no errors and `start_workflows=True`, trigger workflows
7. Return summary dict

---

### Template Rendering

**File:** `src/soliplex/agents/scm/lib/templates/__init__.py`

**Purpose:** Renders GitHub/Gitea issues as Markdown documents using Jinja2.

**Key Functions:**

#### `get_template()` (lines 9-13)
Loads template file asynchronously:

```python
async def get_template(template_name: str) -> str:
    """Load template file from disk."""
    template_path = Path(__file__).parent / template_name
    async with aiofiles.open(template_path) as f:
        return await f.read()
```

#### `render_issue()` (lines 16-19)
Renders issue using Jinja2 template:

```python
async def render_issue(issue: dict[str, Any], owner: str, repo: str) -> str:
    """Render issue as Markdown document."""
    template_str = await get_template("issue.tpl")
    template = Template(template_str)
    return template.render(issue=issue, owner=owner, repo=repo)
```

**Template:** `issue.tpl`

Available context:
- `issue`: Full issue dict from API
- `owner`: Repository owner
- `repo`: Repository name

Template includes:
- Issue title and number
- Creator, state, assignee
- Full issue body
- All comments with metadata

---

### Utilities

**File:** `src/soliplex/agents/scm/lib/utils.py`

**Purpose:** Shared utility functions for SCM operations.

**Key Functions:**

#### `flatten_list()` (lines 8-24)
Recursively flattens nested lists:

```python
def flatten_list(nested: list[Any]) -> list[Any]:
    """Flatten nested list structure. Used after asyncio.gather()."""
```

**Use Case:** After `asyncio.gather()` returns mixed results of files and directory lists, this flattens to single file list.

#### `compute_file_hash()` (lines 27-37)
**CRITICAL** - Computes SHA3-256 hash for file content:

```python
def compute_file_hash(content: bytes) -> str:
    """Compute SHA3-256 hash of file content."""
    return hashlib.sha3_256(content, usedforsecurity=False).hexdigest()
```

**Warning:** This uses SHA3-256, different from filesystem agent's SHA256!

#### `decode_base64_if_needed()` (lines 40-52)
Handles string or bytes content from SCM APIs:

```python
def decode_base64_if_needed(content: str | bytes) -> bytes:
    """Decode base64 string to bytes, or return bytes as-is."""
    if isinstance(content, str):
        return base64.b64decode(content)
    return content
```

**Context:** GitHub/Gitea APIs return file content as base64-encoded strings.

---

## Workflows & Processes

### Filesystem Ingestion Workflow

**Command:** `si-agent fs run-inventory /path/to/inventory.json my-source`

**Step-by-Step Process:**

1. **Load Configuration** (`fs/app.py:138`)
   ```python
   config = await read_config(config_path)
   # Returns sorted list of file dicts with {path, sha256, metadata}
   ```

2. **Validate and Filter** (`fs/app.py:140-143`)
   ```python
   valid_files = [f for f in config if check_config(f)[0]]
   # Rejects unsupported types (zip, rar, 7z, octet-stream, long extensions)
   ```

3. **Status Check** (`fs/app.py:145`)
   ```python
   to_process = await client.check_status(source, valid_files)
   # API returns only files with status "new" or "mismatch"
   ```

4. **Batch Management** (`fs/app.py:155-164`)
   ```python
   batch_id = await client.find_batch_for_source(source)
   if not batch_id:
       result = await client.create_batch(source, f"{source}-{timestamp}")
       batch_id = result["id"]
   # Reuses existing batch for incremental ingestion
   ```

5. **Document Ingestion Loop** (`fs/app.py:168-200`)
   ```python
   for file_dict in to_process:
       async with aiofiles.open(file_path, "rb") as f:
           content = await f.read()

       result = await client.do_ingest(
           source=source,
           batch_id=batch_id,
           uri=file_dict["path"],
           doc_body=content,
           metadata=file_dict["metadata"]
       )

       if "error" in result:
           errors += 1
       else:
           ingested += 1
   ```

6. **Workflow Trigger** (`fs/app.py:202-208`)
   ```python
   if errors == 0 and start_workflows:
       workflow_result = await client.do_start_workflows(
           batch_id=batch_id,
           workflow_definition_id=workflow_definition_id,
           param_id=param_set_id,
           priority=priority
       )
   ```

---

### SCM Ingestion Workflow

**Command:** `si-agent scm run-inventory github my-repo my-owner`

**Step-by-Step Process:**

1. **Provider Selection** (`scm/app.py:35`)
   ```python
   impl = get_scm(scm)  # Returns GitHubProvider or GiteaProvider
   ```

2. **Data Collection** (`scm/app.py:36-38` via `get_data()`)

   **A. Fetch Repository Files:**
   ```python
   files = await impl.list_repo_files(repo, owner)
   # Process:
   # 1. GET /repos/{owner}/{repo}/contents (paginated)
   # 2. Recursively traverse directories
   # 3. For each file:
   #    - Fetch content (decode base64)
   #    - Compute SHA3-256 hash
   #    - Add MIME type
   # 4. Filter by allowed extensions
   # 5. Return flattened list
   ```

   **B. Fetch Issues with Comments:**
   ```python
   issues = await impl.list_issues(repo, owner, add_comments=True)
   # Process:
   # 1. GET /repos/{owner}/{repo}/issues (paginated)
   # 2. For each issue, GET comments endpoint
   # 3. Match comments to issues by issue_url
   # 4. Return issues with comments array
   ```

   **C. Render Issues as Markdown:**
   ```python
   for issue in issues:
       body = await templates.render_issue(issue, owner, repo)
       sha256_hash = hashlib.sha256(body.encode()).hexdigest()
       issue_list.append({
           "uri": f"/{owner}/{repo}/issues/{issue['number']}",
           "sha256": sha256_hash,
           "body": body,
           "metadata": {
               "title": issue["title"],
               "state": issue["state"],
               "content-type": "text/markdown"
           }
       })
   ```

3. **Status Check** (`scm/app.py:38`)
   ```python
   to_process = await client.check_status(source, combined_data)
   # Filters to only items with status "new" or "mismatch"
   ```

4. **Batch Management** (`scm/app.py:44-53`)
   ```python
   source = f"{scm.value}:{owner}:{repo}"  # e.g., "github:soliplex:ingester"
   batch_id = await client.find_batch_for_source(source)
   if not batch_id:
       result = await client.create_batch(source)
       batch_id = result["id"]
   ```

5. **Ingestion Loop** (`scm/app.py:57-89`)
   ```python
   for item in to_process:
       # Strip internal metadata fields
       clean_item = {k: v for k, v in item.items()
                     if k not in ["sha256", "body", "path"]}

       result = await client.do_ingest(
           source=source,
           batch_id=batch_id,
           uri=item["uri"],
           doc_body=item["body"].encode() if isinstance(item["body"], str) else item["body"],
           metadata=clean_item.get("metadata", {})
       )
   ```

6. **Workflow Trigger** (`scm/app.py:91-97`)
   ```python
   if errors == 0 and start_workflows:
       workflow_result = await client.do_start_workflows(...)
   ```

---

### Status Checking Process

**Function:** `client.check_status()` (`client.py:127-166`)

**How It Works:**

1. **Prepare Hash Dictionary** (`client.py:139-143`)
   ```python
   hash_dict = {item["uri"]: item["sha256"] for item in file_list}
   uri_to_item = {item["uri"]: item for item in file_list}
   # Creates mappings for efficient lookup
   ```

2. **API Call** (`client.py:145-156`)
   ```python
   # POST /source-status
   data = {"source": source, "data": hash_dict}
   response = await session.post(url, json=data)
   status_response = await response.json()
   # API compares {uri: sha256} against database
   ```

3. **Filter by Status** (`client.py:158-165`)
   ```python
   processable = []
   for uri, status_info in status_response.items():
       if status_info["status"] in PROCESSABLE_STATUSES:
           processable.append(uri_to_item[uri])
   return processable
   ```

**Status Values:**
- **`new`**: File URI doesn't exist in database for this source
- **`mismatch`**: File exists but SHA hash differs (content changed)
- **`match`**: File exists with same hash (skip ingestion)

**Only files with `new` or `mismatch` status are returned for processing.**

---

### Batch Management Lifecycle

**1. Batch Discovery** (`client.py:34-46`)
```python
async def find_batch_for_source(source: str) -> int | None:
    # GET /batch/ - List all batches
    batches = await response.json()
    for batch in batches:
        if batch.get("source") == source:
            return batch["id"]
    return None
```

**2. Batch Creation** (`client.py:78-94`)
```python
async def create_batch(source: str, name: str | None = None) -> dict:
    # POST /batch/ with {source, name}
    # Returns {id: batch_id, source: source, name: name, ...}
```

**3. Batch Reuse Pattern**
```python
# Both fs/app.py and scm/app.py follow this pattern:
batch_id = await client.find_batch_for_source(source)
if not batch_id:
    result = await client.create_batch(source, batch_name)
    batch_id = result["id"]
# Use batch_id for all subsequent ingestions
```

**Benefits:**
- Incremental ingestion: new files added to existing batch
- Easier tracking of related documents
- Workflow triggering applies to entire batch

**4. Workflow Triggering** (`client.py:97-124`)
```python
async def do_start_workflows(
    batch_id: int,
    workflow_definition_id: str,
    param_id: str | None = None,
    priority: int = 5
) -> dict:
    # POST /batch/start-workflows
    # Triggers backend processing for all documents in batch
```

---

## Testing Guidelines

### Coverage Requirements

**Critical:** 100% branch coverage required (`pyproject.toml:71`)

```toml
[tool.coverage.run]
branch = true

[tool.coverage.report]
fail_under = 100
```

**Coverage Exclusions** (`pyproject.toml:77-82`):
```toml
omit = [
    "*/cli.py",          # Command-line interface (integration-tested manually)
    "*/app.py",          # Orchestration layer (functional tests)
    "*/templates/*",     # Jinja2 templates (tested through rendering)
    "*/conftest.py",     # Test fixtures
]
```

**Why Excluded:**
- CLI and app.py are orchestration layers best tested through functional tests
- Templates are tested indirectly through rendering functions
- These files wire together business logic but contain minimal logic themselves

**Business logic MUST be in covered modules:** `client.py`, `base.py`, `utils.py`, etc.

---

### Test Structure

**Unit Tests:** `tests/unit/` (run by default)
```bash
uv run pytest  # Runs unit tests with coverage check
```

**Functional Tests:** `tests/functional/` (not run by default)
```bash
uv run pytest tests/functional/  # Run explicitly
```

**Test Organization:**
- `test_base.py` - BaseSCMProvider abstract class (29 tests)
- `test_client.py` - API client functions (27 tests)
- `test_utils.py` - Utility functions (13 tests)
- `test_exceptions.py` - Custom exceptions (11 tests)
- `conftest.py` - Shared fixtures and sample data

---

### Mocking Patterns

**Key Fixtures from conftest.py:**

#### 1. `mock_response` (lines 19-39)
Creates mock aiohttp.ClientResponse:

```python
@pytest.fixture
def mock_response():
    def _mock_response(status=200, json_data=None, text_data=""):
        mock_resp = MagicMock()
        mock_resp.status = status
        mock_resp.json = AsyncMock(return_value=json_data)
        mock_resp.text = AsyncMock(return_value=text_data)
        mock_resp.raise_for_status = MagicMock()
        return mock_resp
    return _mock_response
```

**Usage:**
```python
def test_api_call(mock_response):
    resp = mock_response(200, {"key": "value"})
    assert resp.status == 200
    data = await resp.json()
    assert data["key"] == "value"
```

#### 2. `mock_session` (lines 43-72)
Creates mock aiohttp.ClientSession with multiple responses:

```python
@pytest.fixture
def mock_session(mock_response):
    def _mock_session(responses=None):
        if responses is None:
            responses = [(200, [])]

        mock_sess = MagicMock()
        response_queue = [mock_response(status, data) for status, data in responses]

        # Mock different HTTP methods
        for method in ["get", "post", "put", "delete"]:
            mock_method = MagicMock()
            mock_method.return_value = create_async_context_manager(response_queue.pop(0))
            setattr(mock_sess, method, mock_method)

        return mock_sess
    return _mock_session
```

**Usage:**
```python
def test_pagination(mock_session):
    # Three API calls: page1, page2, empty (terminates)
    session = mock_session([
        (200, [{"id": 1}]),
        (200, [{"id": 2}]),
        (200, [])
    ])
    # Test pagination logic
```

#### 3. `create_async_context_manager` (lines 10-15)
Helper for mocking `async with` statements:

```python
def create_async_context_manager(return_value):
    """Create async context manager for mocking."""
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = return_value
    mock_cm.__aexit__.return_value = None
    return mock_cm
```

**Critical Usage:**
```python
# WRONG - doesn't work for async context managers
mock_session.get.return_value = mock_response

# CORRECT - wrap in async context manager
mock_session.get.return_value = create_async_context_manager(mock_response)
```

#### 4. Sample Data Fixtures

**`sample_file_record`** (lines 76-86):
```python
@pytest.fixture
def sample_file_record():
    return {
        "name": "test.md",
        "path": "docs/test.md",
        "type": "file",
        "content": base64.b64encode(b"# Test").decode(),
        "sha": "abc123"
    }
```

**`sample_issue`** (lines 90-101):
```python
@pytest.fixture
def sample_issue():
    return {
        "number": 1,
        "title": "Test Issue",
        "body": "Issue body",
        "state": "open",
        "user": {"login": "testuser"},
        "comments": []
    }
```

---

### Async Testing Patterns

**Required:** All async tests must use `@pytest.mark.asyncio` decorator.

#### Pattern 1: Basic Async Test
```python
@pytest.mark.asyncio
async def test_async_function():
    result = await some_async_function()
    assert result == expected
```

#### Pattern 2: Testing Async Context Managers
```python
@pytest.mark.asyncio
async def test_context_manager():
    async with provider.get_session() as session:
        assert isinstance(session, aiohttp.ClientSession)
```

#### Pattern 3: Testing AsyncMock
```python
@pytest.mark.asyncio
async def test_with_async_mock():
    mock_func = AsyncMock(return_value="result")
    result = await mock_func()
    assert result == "result"
    mock_func.assert_called_once()
```

#### Pattern 4: Testing Async Generators
```python
@pytest.mark.asyncio
async def test_async_generator():
    files = []
    async for file in provider.iter_repo_files("repo"):
        files.append(file)
    assert len(files) == 5
```

#### Pattern 5: Mocking Async HTTP Calls
```python
@pytest.mark.asyncio
async def test_http_call(mock_session):
    mock_sess = mock_session([(200, {"data": "value"})])

    with patch("module.get_session") as mock_get_session:
        mock_get_session.return_value = create_async_context_manager(mock_sess)
        result = await function_that_uses_http()
        assert result["data"] == "value"
```

---

### Running Tests

**Commands:**

```bash
# Run all unit tests with coverage (default)
uv run pytest

# Run with verbose output
uv run pytest -v

# Run specific test file
uv run pytest tests/unit/test_client.py

# Run specific test
uv run pytest tests/unit/test_base.py::test_paginate

# Run functional tests
uv run pytest tests/functional/

# Generate HTML coverage report
uv run pytest --cov-report=html
# Opens htmlcov/index.html

# Check coverage without running tests
uv run coverage report
```

**Coverage Enforcement:**
- Tests FAIL if coverage drops below 100%
- Use `--cov-report=term-missing` to see uncovered lines

---

## Development Patterns & Conventions

### Coding Conventions

**Enforced by Ruff** (`pyproject.toml:87-119`):

```toml
[tool.ruff]
line-length = 128
target-version = "py313"

[tool.ruff.lint]
select = [
    "F",      # pyflakes
    "E", "W", # pycodestyle
    "B",      # flake8-bugbear
    "UP",     # pyupgrade
    "I",      # isort
    "PD",     # pandas-vet
    "TRY",    # tryceratops
    "PT",     # flake8-pytest-style
]

[tool.ruff.lint.isort]
force-single-line = true  # Each import on separate line
```

**Commands:**
```bash
# Check code
uv run ruff check

# Auto-fix issues
uv run ruff check --fix

# Format code
uv run ruff format
```

**Line Length:** 128 characters max

**Import Style:** Force single-line imports
```python
# Correct
from typing import Any
from typing import Dict

# Wrong
from typing import Any, Dict
```

---

### Async Patterns

**1. Use Async Context Managers for Resources:**
```python
# HTTP sessions
async with get_session() as session:
    async with session.get(url) as response:
        data = await response.json()

# File operations
async with aiofiles.open(path, 'rb') as f:
    content = await f.read()
```

**2. Use asyncio.gather() for Concurrent Operations:**
```python
# Fetch multiple items concurrently
tasks = [fetch_file(url) for url in urls]
results = await asyncio.gather(*tasks)
```

**3. Avoid Blocking Operations:**
- Use `aiofiles` instead of `open()`
- Use `aiohttp` instead of `requests`
- Use `asyncio.sleep()` instead of `time.sleep()`

**4. Rate Limiting Pattern** (`base.py:234`):
```python
# Random delay to avoid hitting rate limits
await asyncio.sleep(random.randint(1, 5) * 0.01)  # 10-50ms
```

**5. Async Generators for Streaming:**
```python
async def iter_items() -> AsyncIterator[dict[str, Any]]:
    for item in items:
        processed = await process(item)
        yield processed
```

---

### Error Handling

**Exception Hierarchy:**

```python
# Configuration errors
Exception
└── ValidationError (agents/__init__.py:5-7)
    - Raised for invalid config files

# SCM errors
Exception
└── SCMException (scm/__init__.py:4-6)
    ├── APIFetchError (scm/__init__.py:11-13)
    │   - Raised when API fetch fails
    ├── GitHubAPIError (scm/__init__.py:16-18)
    │   - Raised for GitHub-specific errors
    └── UnexpectedResponseError (scm/__init__.py:21-23)
        - Raised for unexpected HTTP status
```

**Error Handling Pattern:**

**Client Layer** (returns dicts with "error" key):
```python
try:
    result = await api_operation()
    if result.status != 200:
        logger.error(f"API error: {result.status}")
        return {"error": f"HTTP {result.status}"}
    return {"result": "success", "data": data}
except Exception as e:
    logger.exception("Unexpected error")
    return {"error": str(e)}
```

**Provider Layer** (raises exceptions):
```python
try:
    response = await session.get(url)
    self.validate_response(response)
    return await response.json()
except aiohttp.ClientError as e:
    raise APIFetchError(f"Failed to fetch: {e}")
```

**Caller Decides:**
```python
result = await client.do_ingest(...)
if "error" in result:
    # Handle error (log, skip, retry, etc.)
    errors += 1
else:
    ingested += 1
```

---

### Logging

**Setup:** `cli.py:12-13`
```python
import logging
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
```

**Usage Pattern:**
```python
import logging
logger = logging.getLogger(__name__)

# Use appropriate levels
logger.debug(f"Fetching URL: {url}")
logger.info(f"Ingested {count} documents")
logger.warning(f"Unexpected status: {status}")
logger.error(f"Failed to process {uri}: {error}")
logger.exception("Error with stack trace")  # Use in except blocks
```

**Logging Levels:**
- **DEBUG:** Detailed data flow, API URLs, intermediate values
- **INFO:** Operation progress, counts, summary information
- **WARNING:** Unexpected but recoverable situations
- **ERROR:** Errors that prevent operation but allow continuation
- **CRITICAL:** Errors requiring immediate attention (rarely used)

---

### Type Hints

**Required:** Type hints on all function signatures.

**Common Patterns:**

```python
from typing import Any
from collections.abc import AsyncIterator

# Async functions
async def fetch_data(url: str) -> dict[str, Any]:
    ...

# Optional parameters
def process(data: str, config: dict | None = None) -> bool:
    ...

# Union types (Python 3.10+ syntax)
def parse(content: bytes | str) -> bytes:
    ...

# Async generators
async def iter_items() -> AsyncIterator[dict[str, Any]]:
    async for item in source:
        yield item

# Return type with error
def operation() -> dict[str, Any]:
    return {"result": "success"} or {"error": "message"}
```

**Dict vs dict:**
- Use `dict[str, Any]` (lowercase) for Python 3.9+
- Use `Dict[str, Any]` (imported from typing) for older versions
- Project targets Python 3.12+, so use lowercase

---

## Configuration Reference

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ENDPOINT_URL` | Yes | `http://localhost:8000/api/v1` | Soliplex Ingester API endpoint |
| `GH_TOKEN` | For GitHub | `None` | GitHub personal access token |
| `GH_OWNER` | For GitHub | `None` | Default GitHub username or organization |
| `GITEA_URL` | For Gitea | `None` | Gitea instance URL (e.g., `https://gitea.example.com`) |
| `GITEA_TOKEN` | For Gitea | `None` | Gitea API token |
| `GITEA_OWNER` | For Gitea | `"admin"` | Default Gitea username |
| `EXTENSIONS` | No | `"md,pdf,doc,docx"` | Comma-separated list of allowed file extensions |
| `LOG_LEVEL` | No | `"INFO"` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

### Configuration Loading

**File:** `src/soliplex/agents/config.py`

**Pattern:**
```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    endpoint_url: str = "http://localhost:8000/api/v1"
    extensions: list[str] = ["md", "pdf", "doc", "docx"]
    # ... other settings

    class Config:
        env_file = ".env"  # Optional .env file support
```

**Access:**
```python
from soliplex.agents.config import settings

api_url = settings.endpoint_url
allowed_extensions = settings.extensions
```

**Environment Variable Naming:**
- Pydantic automatically converts uppercase env vars to lowercase attributes
- `ENDPOINT_URL` → `endpoint_url`
- `GH_TOKEN` → `gh_token`

**List Handling:**
```bash
# Environment
export EXTENSIONS=md,pdf,txt,rst

# In Python
settings.extensions  # ["md", "pdf", "txt", "rst"]
```

---

## Common Tasks & Extension Recipes

### Adding a New SCM Provider

**Example: Adding GitLab Support**

#### Step 1: Create Provider Module

Create `src/soliplex/agents/scm/gitlab/__init__.py`:

```python
"""GitLab SCM provider implementation."""
from typing import Any
from soliplex.agents.scm.base import BaseSCMProvider
from soliplex.agents.config import settings


class GitLabProvider(BaseSCMProvider):
    """GitLab API provider."""

    def get_default_owner(self) -> str:
        """Return default owner from settings."""
        return settings.gitlab_owner or "root"

    def get_base_url(self) -> str:
        """Return GitLab API base URL."""
        return settings.gitlab_url or "https://gitlab.com/api/v4"

    def get_auth_token(self) -> str:
        """Return GitLab access token."""
        return settings.gitlab_token

    def get_last_updated(self, rec: dict[str, Any]) -> str | None:
        """Extract last updated timestamp from file record."""
        # GitLab includes last_commit_date in file records
        return rec.get("last_commit_date")

    async def validate_response(self, response) -> None:
        """Validate GitLab API response."""
        if response.status == 401:
            raise GitLabAPIError("Unauthorized: check token")
        if response.status == 404:
            return  # Empty result, not an error
        response.raise_for_status()
```

#### Step 2: Add Configuration

Update `src/soliplex/agents/config.py`:

```python
class SCM(str, enum.Enum):
    GITHUB = "github"
    GITEA = "gitea"
    GITLAB = "gitlab"  # Add this

class Settings(BaseSettings):
    # Existing settings...

    # Add GitLab settings
    gitlab_url: str | None = None
    gitlab_token: str | None = None
    gitlab_owner: str | None = None
```

#### Step 3: Update Factory

Modify `src/soliplex/agents/scm/app.py`:

```python
from . import github
from . import gitea
from . import gitlab  # Add import

def get_scm(scm: config.SCM) -> base.BaseSCMProvider:
    """Factory for SCM providers."""
    if scm == config.SCM.GITEA:
        return gitea.GiteaProvider()
    elif scm == config.SCM.GITHUB:
        return github.GitHubProvider()
    elif scm == config.SCM.GITLAB:
        return gitlab.GitLabProvider()  # Add this
    else:
        raise ValueError(f"Unknown SCM: {scm}")
```

#### Step 4: Add Exception (Optional)

Add to `src/soliplex/agents/scm/__init__.py`:

```python
class GitLabAPIError(SCMException):
    """GitLab API error."""
    pass
```

#### Step 5: Write Tests

Create `tests/unit/test_gitlab.py`:

```python
import pytest
from unittest.mock import patch, MagicMock
from soliplex.agents.scm.gitlab import GitLabProvider


class TestGitLabProvider:
    """Test GitLab provider implementation."""

    @pytest.fixture
    def provider(self):
        with patch("soliplex.agents.config.settings") as mock_settings:
            mock_settings.gitlab_url = "https://gitlab.example.com"
            mock_settings.gitlab_token = "glpat-test"
            mock_settings.gitlab_owner = "testuser"
            yield GitLabProvider()

    def test_get_base_url(self, provider):
        assert provider.get_base_url() == "https://gitlab.example.com"

    def test_get_auth_token(self, provider):
        assert provider.get_auth_token() == "glpat-test"

    # Add more tests...
```

---

### Adding Support for New File Types

**Scenario: Add support for .txt and .rst files**

#### Option 1: Update Default in Code

Modify `src/soliplex/agents/config.py:17`:

```python
class Settings(BaseSettings):
    extensions: list[str] = ["md", "pdf", "doc", "docx", "txt", "rst"]
```

#### Option 2: Set via Environment Variable

```bash
export EXTENSIONS=md,pdf,doc,docx,txt,rst
```

#### Option 3: Use .env File

Create `.env` in project root:
```bash
EXTENSIONS=md,pdf,doc,docx,txt,rst
```

**No code changes needed** - extension filtering is data-driven throughout the codebase.

---

### Customizing Issue Template

**File to Modify:** `src/soliplex/agents/scm/lib/templates/issue.tpl`

**Available Context:**
- `issue`: Full issue dict from API
- `owner`: Repository owner
- `repo`: Repository name

**Current Template:**
```jinja2
# {{ issue.title }}  Issue#{{ issue.number }} for {{ owner }}/{{ repo }}
created by {{ issue.user.login }} on {{ issue.created_at }} state={{ issue.state }}
{% if issue.assignee %}assigned to {{ issue.assignee.login }}{% endif %}

{{ issue.body }}

## Comments
{% for comment in issue.comments %}
-comment by {{ comment.user }} on {{ comment.created_at }}
{{ comment.body }}
{% endfor %}
```

**Example: Add Labels and Milestone:**

```jinja2
# {{ issue.title }}  Issue#{{ issue.number }}

**Repository:** {{ owner }}/{{ repo }}
**Created By:** {{ issue.user.login }} on {{ issue.created_at }}
**State:** {{ issue.state }}
{% if issue.assignee %}**Assigned To:** {{ issue.assignee.login }}{% endif %}

{% if issue.labels %}
**Labels:** {% for label in issue.labels %}{{ label.name }}{% if not loop.last %}, {% endif %}{% endfor %}
{% endif %}

{% if issue.milestone %}
**Milestone:** {{ issue.milestone.title }}
{% endif %}

---

{{ issue.body }}

{% if issue.comments %}
## Comments

{% for comment in issue.comments %}
### Comment by {{ comment.user }} on {{ comment.created_at }}
{{ comment.body }}

---
{% endfor %}
{% endif %}
```

**No code changes needed** - template is loaded dynamically.

---

### Adding New API Client Methods

**Example: Add batch deletion method**

#### Step 1: Add Function to client.py

```python
async def delete_batch(batch_id: int) -> dict[str, Any]:
    """Delete a batch by ID.

    Args:
        batch_id: Batch ID to delete

    Returns:
        Success dict or error dict
    """
    url = _build_url(f"/batch/{batch_id}")

    try:
        async with get_session() as session:
            async with session.delete(url) as response:
                response.raise_for_status()
                return {"result": "success", "batch_id": batch_id}
    except aiohttp.ClientError as e:
        logger.error(f"Failed to delete batch {batch_id}: {e}")
        return {"error": str(e)}
```

#### Step 2: Add Test to tests/unit/test_client.py

```python
@pytest.mark.asyncio
async def test_delete_batch_success(mock_session):
    """Test successful batch deletion."""
    mock_sess = mock_session([(200, {})])

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_get_session.return_value = create_async_context_manager(mock_sess)

        result = await client.delete_batch(123)

        assert "error" not in result
        assert result["result"] == "success"
        assert result["batch_id"] == 123


@pytest.mark.asyncio
async def test_delete_batch_not_found(mock_session):
    """Test batch deletion when batch doesn't exist."""
    mock_resp = MagicMock()
    mock_resp.status = 404
    mock_resp.raise_for_status.side_effect = aiohttp.ClientResponseError(
        request_info=MagicMock(),
        history=(),
        status=404
    )

    mock_sess = MagicMock()
    mock_sess.delete.return_value = create_async_context_manager(mock_resp)

    with patch("soliplex.agents.client.get_session") as mock_get_session:
        mock_get_session.return_value = create_async_context_manager(mock_sess)

        result = await client.delete_batch(999)

        assert "error" in result
```

---

### Implementing Retry Logic

**Pattern for Adding Retry to API Calls:**

```python
import asyncio
from functools import wraps
import logging

logger = logging.getLogger(__name__)


def retry_async(max_retries: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """Decorator for retrying async functions with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts
        delay: Initial delay between retries (seconds)
        backoff: Multiplier for delay after each retry
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            current_delay = delay

            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        # Last attempt, re-raise
                        raise

                    logger.warning(
                        f"{func.__name__} attempt {attempt + 1}/{max_retries} failed: {e}. "
                        f"Retrying in {current_delay}s..."
                    )
                    await asyncio.sleep(current_delay)
                    current_delay *= backoff

        return wrapper
    return decorator


# Usage example
@retry_async(max_retries=3, delay=2.0, backoff=2.0)
async def fetch_with_retry(url: str) -> dict:
    """Fetch URL with automatic retry."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.json()
```

---

## Critical File Reference

Quick lookup of important files with line numbers:

### Entry Points
- `src/soliplex/agents/cli.py:16` - Main Typer app creation
- `pyproject.toml:51` - Console script definition (`si-agent`)

### Configuration
- `src/soliplex/agents/config.py:11-22` - Settings class with all environment variables

### API Client
- `src/soliplex/agents/client.py:22-26` - Session management with User-Agent
- `src/soliplex/agents/client.py:34-46` - Find existing batch (batch reuse)
- `src/soliplex/agents/client.py:78-94` - Create new batch
- `src/soliplex/agents/client.py:127-166` - Status checking (filter changed files)
- `src/soliplex/agents/client.py:169-224` - Document ingestion
- `src/soliplex/agents/client.py:97-124` - Start workflows

### SCM Base Provider
- `src/soliplex/agents/scm/base.py:26-363` - BaseSCMProvider abstract class
- `src/soliplex/agents/scm/base.py:38-51` - Abstract methods (must implement)
- `src/soliplex/agents/scm/base.py:74-118` - Pagination algorithm
- `src/soliplex/agents/scm/base.py:120-142` - List issues with comments
- `src/soliplex/agents/scm/base.py:258-304` - List repository files (recursive)
- `src/soliplex/agents/scm/base.py:306-346` - Async file iterator
- `src/soliplex/agents/scm/base.py:159-182` - Parse and normalize file records

### SCM Implementations
- `src/soliplex/agents/scm/github/__init__.py:16-102` - GitHubProvider class
- `src/soliplex/agents/scm/github/__init__.py:40-58` - GitHub error validation
- `src/soliplex/agents/scm/github/__init__.py:60-80` - Large file handling (blob API)
- `src/soliplex/agents/scm/gitea/__init__.py:12-30` - GiteaProvider class

### SCM Orchestration
- `src/soliplex/agents/scm/app.py:16-22` - Provider factory (`get_scm()`)
- `src/soliplex/agents/scm/app.py:104-145` - Get all data (files + issues)
- `src/soliplex/agents/scm/app.py:25-101` - Main ingestion function

### Filesystem Agent
- `src/soliplex/agents/fs/app.py:17-21` - MIME type overrides for Office formats
- `src/soliplex/agents/fs/app.py:62-84` - Build config (scan directory)
- `src/soliplex/agents/fs/app.py:77` - SHA256 hash computation (filesystem)
- `src/soliplex/agents/fs/app.py:101-124` - Validate file config
- `src/soliplex/agents/fs/app.py:127-212` - Main ingestion function

### Utilities
- `src/soliplex/agents/scm/lib/utils.py:8-24` - Flatten nested lists
- `src/soliplex/agents/scm/lib/utils.py:27-37` - **SHA3-256 hash** (SCM only!)
- `src/soliplex/agents/scm/lib/utils.py:40-52` - Base64 decoding

### Templates
- `src/soliplex/agents/scm/lib/templates/__init__.py:9-13` - Load template file
- `src/soliplex/agents/scm/lib/templates/__init__.py:16-19` - Render issue as Markdown
- `src/soliplex/agents/scm/lib/templates/issue.tpl` - Jinja2 issue template

### Test Infrastructure
- `tests/unit/conftest.py:10-15` - Async context manager helper
- `tests/unit/conftest.py:19-39` - Mock response fixture
- `tests/unit/conftest.py:43-72` - Mock session fixture
- `tests/unit/conftest.py:76-113` - Sample data fixtures

### Configuration Files
- `pyproject.toml:16-25` - Project metadata
- `pyproject.toml:27-36` - Dependencies
- `pyproject.toml:38-44` - Dev dependencies
- `pyproject.toml:51-52` - Console scripts
- `pyproject.toml:70-82` - Pytest and coverage config
- `pyproject.toml:87-119` - Ruff linting config

---

## Pitfalls, Gotchas & Non-Obvious Decisions

### 1. Hash Algorithm Difference

**CRITICAL GOTCHA - Most Important!**

- **Filesystem Agent** uses **SHA256**: `hashlib.sha256()`
  - Location: `fs/app.py:77`
  - Used for: Local file hashing in inventory

- **SCM Agent** uses **SHA3-256**: `hashlib.sha3_256()`
  - Location: `scm/lib/utils.py:37`
  - Used for: Remote file content from SCM APIs

- **Issue Documents** use **SHA256**: `hashlib.sha256()`
  - Location: `scm/app.py:141`
  - Used for: Rendered issue Markdown

**Why the difference?**
- Not explicitly documented in code
- Likely historical reasons or different security requirements
- Be aware when debugging hash mismatches

**When extending:** Be consistent with the agent type you're working on.

---

### 2. Batch Reuse Behavior

**Recent Change (commit bd6e0c6):** Agents now search for existing batches before creating new ones.

- Implementation: `client.py:34-46` and usage in `fs/app.py` and `scm/app.py`
- **Behavior:** Running ingestion multiple times with same source reuses the same batch
- **Benefit:** Incremental ingestion - only new/changed files processed
- **Caveat:** If you want a fresh batch, use a different source name

**Pattern:**
```python
batch_id = await client.find_batch_for_source(source)
if not batch_id:
    result = await client.create_batch(source, batch_name)
    batch_id = result["id"]
```

---

### 3. Status Constants - Don't Hardcode

**Important:** Only specific statuses trigger re-ingestion.

```python
# In client.py
STATUS_NEW = "new"              # File doesn't exist
STATUS_MISMATCH = "mismatch"    # File exists but hash differs
PROCESSABLE_STATUSES = {STATUS_NEW, STATUS_MISMATCH}
```

**Don't hardcode strings** like `"new"` or `"mismatch"` in tests or code. Import and use these constants:

```python
from soliplex.agents.client import STATUS_NEW, STATUS_MISMATCH, PROCESSABLE_STATUSES
```

---

### 4. Coverage Exclusions Rationale

**Why certain files are excluded from 100% coverage requirement:**

- `*/cli.py` - Command-line interface handlers (Typer commands)
  - Best tested manually or through functional tests
  - Minimal logic, mostly wiring to business logic

- `*/app.py` - Orchestration layer
  - Combines multiple components
  - Tested through functional tests

- `*/templates/*` - Jinja2 templates
  - Tested indirectly through rendering functions

**Implication:** When adding business logic, put it in modules that ARE covered:
- `client.py`, `base.py`, `utils.py`, `config.py`, etc.
- Extract testable logic from CLI/app files

---

### 5. Async Context Manager Mocking Pattern

**Common Testing Mistake:**

```python
# WRONG - doesn't work for async context managers
mock_session.get.return_value = mock_response

# CORRECT - must wrap in async context manager
from conftest import create_async_context_manager
mock_session.get.return_value = create_async_context_manager(mock_response)
```

**Why:** `async with session.get(url)` requires `__aenter__` and `__aexit__` methods.

**Helper Location:** `tests/unit/conftest.py:10-15`

---

### 6. Pagination Termination Logic

**Pattern:** Pagination continues until empty response.

```python
# From base.py:93
while len(items) != 0 or page == 1:
    # Fetch next page
    # If items is empty, loop terminates
```

**Implication:** API **must** return empty list/array to signal end of pagination.

**Gotcha:** If API returns `null` instead of `[]`, pagination may fail or loop infinitely.

---

### 7. Base64 Content Handling

**GitHub and Gitea APIs return file content as base64-encoded strings.**

Always use `utils.decode_base64_if_needed()` when processing file content from SCM:

```python
from soliplex.agents.scm.lib.utils import decode_base64_if_needed

content = decode_base64_if_needed(rec["content"])  # Handles both str and bytes
```

**Don't assume** content is already bytes - it will be a base64 string from the API.

---

### 8. Extension Filtering Happens Twice (SCM)

**Filtering occurs in TWO places for SCM agent:**

1. **During file fetching** (`base.py:296`)
   ```python
   if allowed_extensions and not matches_extension(file["path"], allowed_extensions):
       continue  # Skip fetching content
   ```

2. **After fetching** (`app.py:112`)
   ```python
   file_list = [f for f in files if matches_extension(f["uri"], settings.extensions)]
   ```

**Why twice?**
- First filter: Avoid unnecessary API calls to fetch content
- Second filter: Final validation before ingestion
- Provides defense in depth

**Be aware:** Changing extension list requires application restart (settings loaded at startup).

---

### 9. MIME Type Handling and Validation

**Default MIME Type:** `application/octet-stream` if type unknown

**Filesystem Agent:**
- Uses Python's `mimetypes` module for detection
- Overrides for Office formats (`fs/app.py:17-21`)
- Validation rejects certain types (`fs/app.py:108-116`):
  - `application/zip`
  - `application/x-rar`
  - `application/x-7z-compressed`
  - `application/octet-stream`
  - Extensions > 4 characters

**SCM Agent:**
- Also uses `mimetypes` module (`base.py:178`)
- No validation (accepts all types from repository)

---

### 10. Test Execution Order

**pytest-asyncio:** Tests run sequentially by default.

**Important:**
- Don't rely on test execution order
- Each test should be independent
- Mock cleanup happens automatically between tests
- Use fixtures for shared setup

---

### 11. Error vs Exception Handling Pattern

**Pattern Divergence Across Layers:**

**Client Layer** (`client.py`):
- Returns dicts with `{"error": "message"}`
- Caller decides how to handle
- Example: `{"error": "HTTP 404"}`

**Provider Layer** (`base.py`, `github/`, `gitea/`):
- Raises `SCMException` subclasses
- Higher-level, more specific errors
- Example: `raise APIFetchError("Failed to fetch")`

**Why Different?**
- Client is lowest level (HTTP layer)
- Providers are higher level (business logic)

**When adding functions:** Follow the pattern of the module you're in.

---

### 12. Owner Parameter Defaulting

**SCM provider methods have optional `owner` parameter:**

```python
async def list_issues(self, repo: str, owner: str | None = None):
    if owner is None:
        owner = self.owner  # Use instance default
```

**Pattern:**
- If `owner` is None, uses `self.owner` (from `get_default_owner()`)
- Allows per-call override or global default
- Follow this pattern when adding new provider methods

---

### 13. Workflow Triggering Conditions

**Workflows only triggered if:**
1. `start_workflows=True` parameter passed
2. No errors during ingestion (`errors == 0`)
3. Workflow definition ID provided

**Code:**
```python
if errors == 0 and start_workflows:
    workflow_result = await client.do_start_workflows(...)
```

**Implication:** Single file failure prevents workflow triggering for entire batch.

---

### 14. Provider get_last_updated() Inconsistency

**GitHub:** Returns `None` (not provided by contents API)
```python
def get_last_updated(self, rec: dict[str, Any]) -> str | None:
    return None
```

**Gitea:** Returns `last_committer_date` from record
```python
def get_last_updated(self, rec: dict[str, Any]) -> str | None:
    return rec.get("last_committer_date")
```

**Implication:** Can't rely on last_updated being available across providers.

---

## Glossary & Terminology

| Term | Definition |
|------|------------|
| **Agent** | Component that collects documents from a source and ingests them into Soliplex Ingester. Two types: Filesystem and SCM. |
| **Batch** | Grouping of related documents for processing. Used to track ingestion operations and trigger workflows. Identified by unique ID. |
| **Ingester** | Backend system (Soliplex Ingester API) that receives and processes documents. Provides REST API for ingestion. |
| **Inventory** | JSON file containing metadata about files to be ingested. Used by filesystem agent. Format: `[{path, sha256, metadata}, ...]` |
| **Provider** | Implementation of SCM interface for specific platform (GitHub, Gitea, etc.). Follows Strategy pattern. |
| **SCM** | Source Code Management platform (GitHub, Gitea, GitLab, etc.). Generic term for Git hosting platforms. |
| **Source** | Identifier for the origin of documents. Format: `"my-docs"` (filesystem) or `"github:owner:repo"` (SCM). Used for batch lookup and status checking. |
| **Status** | State of a document in the Ingester: `"new"` (not in DB), `"mismatch"` (hash changed), `"match"` (unchanged). |
| **URI** | Unique identifier for document within a source. Examples: `"docs/readme.md"` (file) or `"/owner/repo/issues/1"` (issue). |
| **Workflow** | Backend processing pipeline triggered after document ingestion. Defined by workflow definition ID and parameters. |

---

## CLI Command Reference

### Filesystem Agent Commands

```bash
# Build configuration from directory
# Creates inventory.json in the directory
si-agent fs build-config /path/to/documents

# Validate configuration file
# Checks for unsupported file types and extensions
si-agent fs validate-config /path/to/inventory.json

# Check status (summary)
# Shows count of files needing ingestion
si-agent fs check-status /path/to/inventory.json my-source

# Check status (detailed list)
# Shows all files needing ingestion
si-agent fs check-status /path/to/inventory.json my-source --detail

# Ingest documents
# Basic ingestion from inventory file
si-agent fs run-inventory /path/to/inventory.json my-source

# Ingest from directory (builds config automatically)
si-agent fs run-inventory /path/to/documents my-source

# Ingest with workflow trigger
# Triggers processing after successful ingestion
si-agent fs run-inventory /path/to/inventory.json my-source \
  --start-workflows \
  --workflow-definition-id my-workflow \
  --param-set-id my-params \
  --priority 10

# Ingest subset of files
# Process files 10-50 from inventory
si-agent fs run-inventory /path/to/inventory.json my-source --start 10 --end 50
```

### SCM Agent Commands

```bash
# List issues from GitHub repository
si-agent scm list-issues github my-repo my-owner

# List issues from Gitea repository
si-agent scm list-issues gitea my-repo my-owner

# Get repository files (GitHub)
# Lists all files in repository with filtering
si-agent scm get-repo github my-repo my-owner

# Get repository files (Gitea)
si-agent scm get-repo gitea my-repo my-owner

# Ingest repository (GitHub)
# Ingests both files and issues
si-agent scm run-inventory github my-repo my-owner

# Ingest repository (Gitea)
si-agent scm run-inventory gitea my-repo my-owner

# Ingest with workflows (GitHub)
si-agent scm run-inventory github my-repo my-owner \
  --start-workflows \
  --workflow-definition-id document-analysis \
  --priority 5

# Ingest with workflows (Gitea)
si-agent scm run-inventory gitea my-repo my-owner \
  --start-workflows \
  --workflow-definition-id my-workflow \
  --param-set-id my-params \
  --priority 3
```

### Development Commands

```bash
# Run unit tests with coverage
uv run pytest

# Run tests verbosely
uv run pytest -v

# Run specific test file
uv run pytest tests/unit/test_client.py

# Run specific test
uv run pytest tests/unit/test_base.py::test_paginate

# Generate HTML coverage report
uv run pytest --cov-report=html

# Run functional tests
uv run pytest tests/functional/

# Check code with Ruff
uv run ruff check

# Auto-fix Ruff issues
uv run ruff check --fix

# Format code with Ruff
uv run ruff format
```

---

## Recent Changes & Version History

### Version 0.1.0 (Current)

**Recent Commit:** `bd6e0c6 - feat: change batch behavior to look for pre-existing batch if available`

**Changes:**
- Added `find_batch_for_source()` function to `client.py:34-46`
- Modified ingestion workflows in both `fs/app.py` and `scm/app.py` to reuse existing batches
- Source name is now used as key for batch identification

**Breaking Changes:** None (additive change)

**Migration Notes:**
- No action required - new behavior is automatic
- Previous behavior: Always created new batch for each ingestion
- New behavior: Searches for existing batch by source name and reuses it
- Incremental ingestion: Only new/changed files are processed

**Impact on AI Assistance:**
- When helping with batch-related code, remember batches are now reused
- Source name is critical for batch identification
- Multiple ingestion runs with same source will append to same batch
- To get a fresh batch, use a different source name

---

## Summary for AI Assistants

This document provides comprehensive internal documentation for working on the Soliplex Ingester-Agents codebase. Key takeaways:

1. **Two Independent Systems:** Filesystem agent and SCM agent operate independently
2. **Provider Pattern:** Easy to extend with new SCM platforms
3. **100% Coverage:** All business logic must have comprehensive tests
4. **Async Throughout:** All I/O operations use async patterns
5. **Hash Gotcha:** SHA256 for filesystem, SHA3-256 for SCM files
6. **Incremental Ingestion:** Status checking and batch reuse enable efficient updates
7. **Extensibility:** Well-defined extension points for providers, file types, and workflows

When making changes:
- Follow existing patterns in the module you're modifying
- Write tests first (TDD) to ensure coverage requirements
- Use async patterns consistently
- Document non-obvious decisions
- Test both success and error paths

For questions or clarifications, refer to specific sections above or examine the referenced source files.
