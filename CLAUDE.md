# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common Development Commands

### Development Setup
```bash
# Install dependencies
pip install -r requirements.txt
```

### Running Terratunnel
```bash
# Server mode
python -m terratunnel server --domain yourdomain.com

# Client mode
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
   - Manages tunnel registration and request routing
   - Thread-safe connection management with locks
   - Optional VCS IP validation (GitHub, GitLab) and domain ownership validation
   - Graceful shutdown handling

2. **Client Component** (`terratunnel/client/app.py`)
   - WebSocket client that maintains persistent connection to server
   - Local HTTP server forwarding requests to user's endpoint
   - Optional webhook dashboard (port 8080) and JSON API (port 8081)
   - GitHub App webhook auto-update functionality
   - Thread-safe webhook history management

3. **CLI Interface** (`terratunnel/cli.py`)
   - Click-based command-line interface
   - Environment variable support for all options
   - Centralized logging configuration

### Request Flow

1. External webhook hits `https://subdomain.domain.com`
2. Server receives HTTP request and identifies target client by subdomain
3. Server forwards request over WebSocket to client
4. Client makes HTTP request to local endpoint
5. Client sends response back through WebSocket
6. Server returns response to original webhook sender

### Key Design Patterns

- **Async/Await**: Entire codebase uses Python asyncio for concurrent operations
- **Thread Safety**: All shared state protected by threading locks
- **Graceful Shutdown**: Signal handlers ensure clean connection termination
- **Environment-First Config**: All options configurable via environment variables
- **Modular Architecture**: Clear separation between server, client, and CLI layers

### Important Routes and Endpoints

**Server Routes** (in order of registration - important for route matching):
1. `/ws` - WebSocket endpoint for tunnel clients
2. `/_health` - Health check endpoint (no auth required)
3. `/_admin` - Admin dashboard (API key protected)
4. `/_admin/api/tunnels` - Admin API (API key protected)
5. `/{path:path}` - Catch-all proxy route (VCS IP validation applies here)

**Client Routes** (when dashboard enabled):
- Dashboard on port 8080: `/`, `/api/webhooks`, `/api/status`
- API on port 8081: `/status`, `/webhooks`, `/health`

### Security Features

- **VCS IP Validation**: Optional filtering to only allow VCS provider IPs (GitHub, GitLab)
- **Domain Ownership Validation**: `.well-known/terratunnel` endpoint verification
- **JWT Authentication**: For GitHub App webhook updates
- **Random Subdomain Generation**: Unpredictable tunnel URLs for security
- **Admin Interface Protection**: Admin dashboard only accessible from private IPs

