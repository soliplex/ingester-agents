# Gitea Test Environment

This directory contains a Docker-based Gitea instance configured with SQLite for testing the SCM agent.

## Configuration

- **Database**: SQLite (stored in `./gitea_data/gitea/gitea.db`)
- **Port**: 3000 (HTTP) and 2222 (SSH)
- **Data**: Stored in `./gitea_data/`
- **Image**: gitea/gitea:1.21

## Quick Start

### 1. Start Gitea

```bash
# Start the container
docker-compose up -d

# Check logs
docker-compose logs -f
```

### 2. Create Admin User

**On Linux/Mac:**
```bash
chmod +x init-admin.sh
./init-admin.sh
```

**On Windows (PowerShell):**
```powershell
.\init-admin.ps1
```

**Or manually:**
```bash
docker exec -it gitea gitea admin user create \
  --username admin \
  --password admin123 \
  --email admin@example.com \
  --admin
```

### 3. Access Gitea

Open http://localhost:3000 in your browser.

**Default credentials:**
- Username: `admin`
- Password: `admin123`

### 4. Generate API Token

The init scripts will automatically generate an API token. If you need to create additional tokens:

**Via CLI:**
```bash
docker exec gitea gitea admin user generate-access-token \
  --username admin \
  --token-name "my-token" \
  --scopes "write:repository,write:issue,write:user"
```

**Via Web UI:**
1. Log in to Gitea (http://localhost:3000)
2. Click your profile → Settings → Applications
3. Generate New Token
4. Select scopes: `write:repository`, `write:issue`, `write:user`
5. Copy the token

### 5. Configure Environment Variables

Set these environment variables for the ingester-agents:

```bash
export GITEA_URL=http://localhost:3000
export GITEA_TOKEN=<your-api-token>
export GITEA_OWNER=admin
```

**Windows PowerShell:**
```powershell
$env:GITEA_URL = "http://localhost:3000"
$env:GITEA_TOKEN = "<your-api-token>"
$env:GITEA_OWNER = "admin"
```

## SQLite Configuration

The Gitea instance is configured to use SQLite with the following settings:

- **DB Type**: sqlite3
- **DB Path**: `/data/gitea/gitea.db` (inside container)
- **Host Path**: `./gitea_data/gitea/gitea.db`

SQLite configuration in `docker-compose.yml`:
```yaml
environment:
  - GITEA__database__DB_TYPE=sqlite3
  - GITEA__database__PATH=/data/gitea/gitea.db
```

## Useful Commands

### Container Management

```bash
# Start
docker-compose up -d

# Stop
docker-compose down

# Restart
docker-compose restart

# View logs
docker-compose logs -f

# Shell access
docker exec -it gitea sh
```

### User Management

```bash
# List users
docker exec gitea gitea admin user list

# Create user
docker exec gitea gitea admin user create \
  --username testuser \
  --password test123 \
  --email test@example.com

# Change password
docker exec gitea gitea admin user change-password \
  --username admin \
  --password newpassword
```

### Repository Management

```bash
# List repositories
docker exec gitea gitea admin repo list

# Create repository (requires API token or web UI)
```

### Database

```bash
# Access SQLite database
docker exec -it gitea sqlite3 /data/gitea/gitea.db

# Backup database
docker exec gitea sqlite3 /data/gitea/gitea.db .dump > backup.sql

# View tables
docker exec gitea sqlite3 /data/gitea/gitea.db ".tables"
```

## Testing with ingester-agents

After setting up Gitea and configuring environment variables, you can test the SCM agent:

```bash
# List repositories
si-agent scm get-repo gitea <repo-name> admin

# List issues
si-agent scm list-issues gitea <repo-name> admin

# Run full inventory
si-agent scm run-inventory gitea <repo-name> admin
```

## Troubleshooting

### Port Already in Use

If port 3000 is already in use, edit `docker-compose.yml`:
```yaml
ports:
  - "3001:3000"  # Change host port to 3001
```

Then update `GITEA_URL=http://localhost:3001`

### Database Locked

If you see "database is locked" errors:
```bash
# Stop the container
docker-compose down

# Remove lock file if exists
rm gitea_data/gitea/gitea.db-*

# Restart
docker-compose up -d
```

### Reset Everything

To completely reset the Gitea instance:
```bash
# Stop container
docker-compose down

# Remove all data
rm -rf gitea_data

# Restart
docker-compose up -d

# Recreate admin user
./init-admin.sh  # or .\init-admin.ps1 on Windows
```

### Check SQLite Database

```bash
# Verify SQLite is being used
docker exec gitea cat /data/gitea/conf/app.ini | grep -A 5 "\[database\]"

# Check database file exists
docker exec gitea ls -lh /data/gitea/gitea.db

# Check database size
docker exec gitea du -h /data/gitea/gitea.db
```

## Test Data

The `build_test_data.py` script can be used to populate test issues. It expects:
- A running Gitea instance
- Valid `GITEA_URL`, `GITEA_TOKEN`, and `GITEA_OWNER` environment variables
- CSV file with test issues (`issues.csv`)

## Architecture

```
┌─────────────────────────────────┐
│   Docker Host (Windows)         │
│                                 │
│  ┌───────────────────────────┐ │
│  │   Gitea Container         │ │
│  │   ┌─────────────────┐     │ │
│  │   │  Gitea App      │     │ │
│  │   │  (Port 3000)    │     │ │
│  │   └─────────────────┘     │ │
│  │   ┌─────────────────┐     │ │
│  │   │  SQLite DB      │     │ │
│  │   │  /data/gitea/   │     │ │
│  │   │  gitea.db       │←────┼─┼──── ./gitea_data/gitea/gitea.db
│  │   └─────────────────┘     │ │
│  └───────────────────────────┘ │
│           ↑                     │
│           │ Port 3000           │
└───────────┼─────────────────────┘
            │
            ↓
    http://localhost:3000
```

## Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `GITEA_URL` | `http://localhost:3000` | Gitea instance URL |
| `GITEA_TOKEN` | - | API token for authentication |
| `GITEA_OWNER` | `admin` | Default repository owner |
| `USER_UID` | `1000` | User ID for file permissions |
| `USER_GID` | `1000` | Group ID for file permissions |

## Security Notes

⚠️ **This is a TEST environment only!**

- Default password is `admin123` (insecure for production)
- Registration is enabled
- No SSL/TLS (uses HTTP)
- SQLite is suitable for testing but not recommended for production
- Tokens have broad scopes for testing purposes

**Do not expose this instance to the internet or use it for production data.**
