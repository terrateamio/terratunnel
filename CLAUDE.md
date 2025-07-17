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

The Docker publish workflow (`.github/workflows/docker-publish.yml`) runs on:
- Pushes to main branch
- Git tags
- Pull requests (build only, no push)
- Manual workflow dispatch

## High-Level Architecture

### Core Components

**Terratunnel** is a secure HTTP tunneling solution that uses WebSocket connections to forward webhooks from public URLs to local development environments. The architecture consists of:

1. **Server Component** (`terratunnel/server/app.py`)
   - FastAPI-based server handling WebSocket connections
   - OAuth2 authentication (GitHub, GitLab, Google)
   - SQLite database for user management and audit logging
   - Static tunnel URLs per user (e.g., `happy-panda-dancing.yourdomain.com`)
   - One active API key per user with rotation capability
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
   - Tables: users, api_keys, tunnel_audit_log
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

1. **OAuth Login**: User visits `/auth/login` → GitHub OAuth → Callback → JWT session
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
- **Single API Key**: Users limited to one active API key at a time with rotation

### Important Routes and Endpoints

**Server Routes** (in order of registration - important for route matching):
1. `/ws` - WebSocket endpoint for tunnel clients
2. `/_health` - Health check endpoint (no auth required)
3. `/auth/login` - OAuth login page
4. `/auth/{provider}` - OAuth provider redirect (github, gitlab, google)
5. `/auth/{provider}/callback` - OAuth callback handlers
6. `/api/auth/exchange` - Exchange OAuth token for API key
7. `/api/auth/me` - Get current user info
8. `/api/auth/keys` - List user's API keys
9. `/api/auth/keys/{key_id}` - Revoke API key
10. `/_admin` - Admin dashboard (OAuth protected, admin users only)
11. `/_admin/api/tunnels` - Admin API for active tunnels
12. `/_admin/api/audit` - Admin API for audit logs
13. `/` - Home page with login
14. `/dashboard` - User dashboard for API key management
15. `/{path:path}` - Catch-all proxy route (handles subdomain requests)

**Client Routes** (when dashboard enabled):
- Dashboard on port 8080: `/`, `/api/webhooks`, `/api/status`
- API on port 8081: `/status`, `/webhooks`, `/health`

### Security Features

- **OAuth2 Authentication**: Support for GitHub, GitLab, and Google
- **JWT Sessions**: Secure session management with configurable expiration
- **API Key Authentication**: Bearer token authentication for programmatic access
- **Admin Access Control**: Restricted to users in TERRATUNNEL_{PROVIDER}_ADMIN_USERS
- **Static Subdomain Assignment**: Predictable URLs per user for security auditing
- **One Key Per User**: Prevents key proliferation and simplifies management
- **Audit Logging**: All tunnel connections logged with timestamps and user info

### Database Schema

```sql
-- Users table with static tunnel subdomain
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    provider TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    tunnel_subdomain TEXT UNIQUE,
    UNIQUE(username, provider)
);

-- API keys (one active per user)
CREATE TABLE api_keys (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    api_key_hash TEXT NOT NULL UNIQUE,
    api_key_prefix TEXT NOT NULL,
    name TEXT,
    is_active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP,
    expires_at TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Audit log for connections
CREATE TABLE tunnel_audit_log (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    username TEXT,
    subdomain TEXT NOT NULL,
    connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    disconnected_at TIMESTAMP,
    client_ip TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
```

### Environment Variables

Required for production:
```bash
# OAuth credentials
GITHUB_CLIENT_ID=your_github_client_id
GITHUB_CLIENT_SECRET=your_github_client_secret

# Security
JWT_SECRET=your_secure_secret_key  # Generate with: openssl rand -hex 32

# Admin users (comma-separated)
TERRATUNNEL_GITHUB_ADMIN_USERS=username1,username2

# Database (for Fly.io deployment)
TERRATUNNEL_DB_PATH=/data/terratunnel.db
```

### Common Issues and Solutions

1. **"Cannot add a UNIQUE column" during migration**
   - Solution: Start fresh with new database or manually migrate data

2. **Client authentication failures**
   - Ensure API key is in Authorization header: `Authorization: Bearer your_key`
   - Check that API key is active in database

3. **Subdomain routing returns 404**
   - Verify user has tunnel_subdomain assigned in database
   - Check that tunnel is active in connected_tunnels dict
   - Ensure hostname normalization (lowercase) is consistent

4. **Admin access issues**
   - Verify username is in TERRATUNNEL_{PROVIDER}_ADMIN_USERS
   - Check JWT token expiration

5. **Fly.io deployment issues**
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

### Recent Changes (July 2025)

1. **Static Tunnel URLs**: Each user now gets a permanent subdomain (e.g., `gentle-tiger-swimming.yourdomain.com`) that doesn't change when API keys are rotated.

2. **One API Key Per User**: Users are limited to one active API key at a time. Generating a new key automatically deactivates the old one.

3. **Human-Readable Subdomains**: Using the `coolname` library to generate memorable subdomains instead of random strings.

4. **Middleware-Based Routing**: Subdomain detection moved to middleware for cleaner request handling.

5. **Database Schema Updates**: Added `tunnel_subdomain` column to users table with automatic migration support.