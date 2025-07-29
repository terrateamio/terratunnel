# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common Development Commands

### Development Setup
```bash
# Install dependencies
pip install -r requirements.txt

# Create .env file from example
cp .env.example .env
# Edit .env and add your OAuth credentials and JWT secret
```

### Running Terratunnel
```bash
# Server mode
python -m terratunnel server --domain yourdomain.com

# Client mode (with API key)
export TERRATUNNEL_API_KEY=your_api_key_here
python -m terratunnel client --local-endpoint http://localhost:3000

# Enable debug logging
python -m terratunnel --log-level DEBUG server
```

### Building and Running
```bash
# Build Docker image locally
docker build -t terratunnel .

# Run Docker image
docker run --rm ghcr.io/terrateamio/terratunnel:latest --help
docker run --rm ghcr.io/terrateamio/terratunnel:latest server --help
docker run --rm ghcr.io/terrateamio/terratunnel:latest client --help
```

### Linting
```bash
# Install flake8
pip install flake8

# Run linting (same as CI pipeline)
flake8 terratunnel --count --select=E9,F63,F7,F82 --show-source --statistics
flake8 terratunnel --count --exit-zero --max-complexity=10 --max-line-length=127 --statistics
```

### Database Management
```bash
# Connect to SQLite database
sqlite3 terratunnel.db

# View schema
.schema

# Check users and their tunnel URLs
SELECT id, username, tunnel_subdomain FROM users;

# Check API keys
SELECT * FROM api_keys WHERE is_active = 1;
```

### Deployment (Fly.io)
```bash
# Deploy to Fly.io
fly deploy

# SSH into container
fly ssh console

# Access database in production
fly ssh console -C "sqlite3 /data/terratunnel.db"

# Download database from Fly.io
fly ssh sftp get /data/terratunnel.db ./terratunnel-prod.db

# Check logs
fly logs

# Destroy and recreate volume (for fresh start)
fly volumes destroy terratunnel_data
fly volumes create terratunnel_data --size 1
```

### CI/CD and Publishing

**Docker Images**: Automatically published to `ghcr.io/terrateamio/terratunnel` via GitHub Actions:
- `latest` tag: Updated on every push to main branch
- Version tags: Created for git tags (e.g., `v1.0.0`)
- Multi-platform builds: linux/amd64 and linux/arm64

The Docker publish workflow (`.github/workflows/publish.yml`) runs on:
- Pushes to main branch
- Git tags
- Pull requests (build only, no push)
- Manual workflow dispatch

## High-Level Architecture

### Core Components

**Terratunnel** is a secure HTTP tunneling solution that uses WebSocket connections to forward webhooks from public URLs to local development environments. The architecture consists of:

1. **Server Component** (`terratunnel/server/app.py`)
   - FastAPI-based server handling WebSocket connections
   - OAuth2 authentication (GitHub, GitLab)
   - SQLite database for user management and audit logging
   - Static tunnel URLs per user (e.g., `happy-panda-dancing.yourdomain.com`)
   - One active API key per tunnel with rotation capability
   - Thread-safe connection management with locks
   - Middleware-based subdomain routing
   - Admin dashboard for tunnel monitoring

2. **Client Component** (`terratunnel/client/app.py`)
   - WebSocket client that maintains persistent connection to server
   - API key authentication via Authorization header
   - Local HTTP server forwarding requests to user's endpoint
   - Optional webhook dashboard (port 8080) and JSON API (port 8081)
   - Thread-safe webhook history management
   - Automatic reconnection on connection loss

3. **CLI Interface** (`terratunnel/cli.py`)
   - Click-based command-line interface
   - Environment variable support for all options
   - Centralized logging configuration

4. **Database Layer** (`terratunnel/server/database.py`)
   - SQLite with foreign key constraints
   - Tables: users, api_keys, tunnels, tunnel_audit_log
   - Automatic migrations on startup
   - Human-readable subdomain generation using coolname library

### Request Flow

1. External webhook hits `https://subdomain.domain.com`
2. Server middleware intercepts subdomain requests
3. Server identifies user by static subdomain
4. Server forwards request over WebSocket to connected client
5. Client makes HTTP request to local endpoint
6. Client sends response back through WebSocket
7. Server returns response to original webhook sender

### Authentication Flow

1. **OAuth Login**: User visits `/auth/login` → GitHub/GitLab OAuth → Callback → JWT session
2. **API Key Generation**: Authenticated user generates API key (replaces any existing key)
3. **Client Connection**: Client connects with API key in Authorization header
4. **Tunnel Creation**: Server assigns user's static subdomain to the tunnel

### Key Design Patterns

- **Async/Await**: Entire codebase uses Python asyncio for concurrent operations
- **Thread Safety**: All shared state protected by threading locks
- **Graceful Shutdown**: Signal handlers ensure clean connection termination
- **Environment-First Config**: All options configurable via environment variables
- **Modular Architecture**: Clear separation between server, client, and CLI layers
- **Static Tunnels**: Each user gets one permanent subdomain regardless of API key changes
- **Single API Key**: Users limited to one active API key per tunnel at a time with rotation

### Important Routes and Endpoints

**Server Routes** (in order of registration - important for route matching):
1. `/ws` - WebSocket endpoint for tunnel clients
2. `/_health` - Health check endpoint (no auth required)
3. `/auth/login` - OAuth login page
4. `/auth/{provider}` - OAuth provider redirect (github, gitlab)
5. `/auth/{provider}/callback` - OAuth callback handlers
6. `/api/auth/exchange` - Exchange OAuth token for API key
7. `/api/auth/me` - Get current user info
8. `/api/auth/keys` - List user's API keys
9. `/api/auth/keys/{key_id}` - Revoke API key
10. `/admin/database` - Database browser (admin only)
11. `/` - Home page with login/dashboard
12. `/tunnels/new` - Create new tunnel (admin only)
13. `/tunnels/{tunnel_id}/keys/new` - Generate/rotate API key for tunnel
14. `/{path:path}` - Catch-all proxy route (handles subdomain requests)

**Client Routes** (when dashboard enabled):
- Dashboard on port 8080: `/`, `/api/webhooks`, `/api/status`
- API on port 8081: `/status`, `/webhooks`, `/health`

### Security Features

- **OAuth2 Authentication**: Support for GitHub and GitLab
- **JWT Sessions**: Secure session management with configurable expiration
- **API Key Authentication**: Bearer token authentication for programmatic access
- **Admin Access Control**: Restricted to users in TERRATUNNEL_{PROVIDER}_ADMIN_USERS
- **Static Subdomain Assignment**: Each tunnel gets a predictable URL for security auditing
- **Tunnel-Based Access**: Each tunnel has its own API key that can be rotated
- **User Limits**: Non-admin users limited to 1 tunnel, admin users can create multiple
- **Audit Logging**: All tunnel connections logged with timestamps and user info

### Database Schema

```sql
-- Users table
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    auth_provider TEXT NOT NULL,
    provider_user_id TEXT NOT NULL,
    provider_username TEXT NOT NULL,
    email TEXT,
    name TEXT,
    avatar_url TEXT,
    tunnel_subdomain TEXT UNIQUE,  -- Legacy, migrated to tunnels table
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active BOOLEAN DEFAULT 1,
    UNIQUE(auth_provider, provider_user_id),
    UNIQUE(auth_provider, provider_username)
);

-- Tunnels table (each tunnel has a subdomain and can have one API key)
CREATE TABLE tunnels (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    subdomain TEXT UNIQUE NOT NULL,
    name TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active BOOLEAN DEFAULT 1,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- API keys (belong to tunnels, not directly to users)
CREATE TABLE api_keys (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    tunnel_id INTEGER REFERENCES tunnels(id),
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    name TEXT,
    is_active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP,
    expires_at TIMESTAMP,
    scopes TEXT DEFAULT 'tunnel:create',
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Audit log for connections
CREATE TABLE tunnel_audit_log (
    id INTEGER PRIMARY KEY,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    subdomain TEXT NOT NULL,
    hostname TEXT NOT NULL,
    local_endpoint TEXT NOT NULL,
    client_ip TEXT,
    user_agent TEXT,
    connection_id TEXT NOT NULL,
    event_type TEXT DEFAULT 'connect',
    user_id INTEGER,
    username TEXT,
    api_key_id INTEGER,
    additional_data TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (api_key_id) REFERENCES api_keys(id) ON DELETE SET NULL
);
```

### Environment Variables

Required for production:
```bash
# OAuth credentials (at least one provider required)
GITHUB_CLIENT_ID=your_github_client_id
GITHUB_CLIENT_SECRET=your_github_client_secret

GITLAB_CLIENT_ID=your_gitlab_client_id
GITLAB_CLIENT_SECRET=your_gitlab_client_secret

# Security
JWT_SECRET=your_secure_secret_key  # Generate with: openssl rand -hex 32

# Admin users (comma-separated)
TERRATUNNEL_GITHUB_ADMIN_USERS=username1,username2
TERRATUNNEL_GITLAB_ADMIN_USERS=username1,username2

# Database (for Fly.io deployment)
TERRATUNNEL_DB_PATH=/data/terratunnel.db

# Request logging (optional - disabled by default)
# Set this to enable detailed request/response logging to a file
# TERRATUNNEL_REQUEST_LOG=./terratunnel_requests.log

# File size limits (optional - defaults shown)
# Maximum request/response size to prevent OOM issues
# TERRATUNNEL_MAX_REQUEST_SIZE=52428800  # 50MB
# TERRATUNNEL_MAX_RESPONSE_SIZE=52428800  # 50MB
```

### Common Issues and Solutions

1. **"Cannot add a UNIQUE column" during migration**
   - Solution: Start fresh with new database or manually migrate data

2. **"File too large" error or server crashes with OOM**
   - Terratunnel automatically streams files larger than 10MB to prevent memory issues
   - Files between 10-50MB are streamed in 512KB chunks with integrity checking
   - Files larger than 50MB will return an HTTP 413 error (configurable)
   - Adjust limits with TERRATUNNEL_MAX_REQUEST_SIZE and TERRATUNNEL_MAX_RESPONSE_SIZE
   - WebSocket message size limit is 512MB to accommodate chunked transfers

3. **Client authentication failures**
   - Ensure API key is in Authorization header: `Authorization: Bearer your_key`
   - Check that API key is active in database

4. **Subdomain routing returns 404**
   - Verify user has tunnel_subdomain assigned in database
   - Check that tunnel is active in connected_tunnels dict
   - Ensure hostname normalization (lowercase) is consistent

5. **Admin access issues**
   - Verify username is in TERRATUNNEL_{PROVIDER}_ADMIN_USERS
   - Check JWT token expiration

6. **Fly.io deployment issues**
   - Ensure volume is mounted at /data for persistent storage
   - Check that TERRATUNNEL_DB_PATH points to /data/terratunnel.db
   - Verify secrets are set: fly secrets list

### Development Tips

1. **Testing Authentication Flow**:
   ```bash
   # Set REQUIRE_AUTH_FOR_TUNNELS=false for testing without auth
   # Use ngrok or similar to test OAuth callbacks locally
   ```

2. **Database Debugging**:
   ```bash
   # Enable debug logging to see SQL queries
   python -m terratunnel --log-level DEBUG server
   ```

3. **Client Testing**:
   ```bash
   # Test with a simple HTTP server
   python -m http.server 3000
   # Then connect client to http://localhost:3000
   ```

4. **Subdomain Testing**:
   ```bash
   # Add to /etc/hosts for local testing
   127.0.0.1 happy-panda-dancing.localhost
   ```

5. **API Key Rotation**:
   - Generate new key through dashboard
   - Update client with new key
   - Old key is automatically deactivated

### Streaming Large Files

Terratunnel automatically handles large file transfers using a chunked streaming protocol:

1. **Automatic Streaming**: 
   - Binary files larger than 10MB are automatically streamed in chunks
   - Binary responses without Content-Length headers are always streamed (prevents loading unknown-size responses into memory)
2. **Memory Efficient**: Only 512KB is held in memory at a time during transfers
3. **Integrity Checking**: Each chunk includes SHA256 checksums for verification
4. **Transparent**: The streaming is transparent to HTTP clients - they see normal responses
5. **Progress Tracking**: Debug logs show chunk transfer progress

**How it works**:
- Client detects large responses and sends initial response with streaming metadata
- File data is sent in separate chunk messages with checksums
- Server reassembles chunks and verifies integrity before sending to HTTP client
- Errors during streaming are handled gracefully with proper error messages

**When streaming is used**:
- Binary files larger than STREAMING_THRESHOLD (default: 10MB)
- Binary responses without Content-Length header (to avoid memory issues with unknown-size responses)
- Text files are never streamed (they use the regular text response path)

**Configuration**:
- `STREAMING_THRESHOLD`: Files larger than this use streaming (default: 10MB)
- `DEFAULT_CHUNK_SIZE`: Size of each chunk (default: 512KB)
- `TERRATUNNEL_MAX_RESPONSE_SIZE`: Maximum file size before returning 413 error (default: 50MB)

### Recent Changes (2024-2025)

1. **Static Tunnel URLs**: Each user now gets a permanent subdomain (e.g., `gentle-tiger-swimming.yourdomain.com`) that doesn't change when API keys are rotated.

2. **Tunnel-Based API Keys**: Each tunnel has its own API key that can be rotated independently.

3. **Human-Readable Subdomains**: Using the `coolname` library to generate memorable subdomains instead of random strings.

4. **Middleware-Based Routing**: Subdomain detection moved to middleware for cleaner request handling.

5. **Database Schema Updates**: Added `tunnel_subdomain` column to users table with automatic migration support.

6. **Request Logging System**: 
   - Access logs always shown in console: `[timestamp] METHOD /path -> status_code duration_ms`
   - Optional detailed logging to file (disabled by default)
   - When enabled via `TERRATUNNEL_REQUEST_LOG` environment variable:
     - Adds request UUID to console logs
     - Saves full request/response details to specified log file
     - Human-readable format with clear section separators

7. **Streaming for Large Files**: 
   - Automatic chunked streaming for files larger than 10MB
   - Prevents OOM errors by processing files in 512KB chunks
   - Includes integrity checking with SHA256 checksums per chunk
   - Transparent to HTTP clients - no changes needed
   - WebSocket message size increased to 512MB to support large transfers

8. **Admin Features Update**:
   - Admin dashboard accessible at root path for admin users
   - Admin dashboard shows all active tunnels and recent connections
   - Database browser at `/admin/database` for direct SQL access

9. **Tunnel-Based Architecture** (December 2024):
   - Introduced proper `tunnels` table in database
   - Each tunnel consists of:
     - Unique subdomain (e.g., `happy-panda-123`)
     - API key (can be rotated without changing subdomain)
   - Non-admin users: Limited to 1 tunnel
   - Admin users: Can create multiple tunnels
   - API keys now belong to tunnels (not directly to users)
   - UI changes:
     - Dashboard shows all user's tunnels
     - Each tunnel has "Generate/Rotate API Key" button
     - Admin users see "Create New Tunnel" button
   - Database migration automatically moves existing data to new structure
   - New users get automatic tunnel creation on first login

10. **GitLab OAuth Support** (July 2025):
    - Added GitLab as an OAuth provider alongside GitHub
    - Configure with `GITLAB_CLIENT_ID` and `GITLAB_CLIENT_SECRET`
    - Admin users configured via `TERRATUNNEL_GITLAB_ADMIN_USERS`
    - Redirect URI: `https://yourdomain.com/auth/gitlab/callback`
    - Required scope: `read_user`

# important-instruction-reminders
Do what has been asked; nothing more, nothing less.
NEVER create files unless they're absolutely necessary for achieving your goal.
ALWAYS prefer editing an existing file to creating a new one.
NEVER proactively create documentation files (*.md) or README files. Only create documentation files if explicitly requested by the User.

IMPORTANT: this context may or may not be relevant to your tasks. You should not respond to this context unless it is highly relevant to your task.