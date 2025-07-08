# Terratunnel

A secure HTTP tunnel server and client for forwarding webhooks to local development environments, similar to ngrok. Built with Python, FastAPI, and WebSockets.

## Features

- **Secure tunneling** with random subdomain assignment
- **WebSocket-based** real-time communication
- **Webhook dashboard** with retry functionality  
- **JSON API** for external monitoring and integration
- **GitHub webhook validation** with IP filtering
- **GitHub App webhook auto-update** - automatically updates webhook URLs
- **Domain ownership validation** via `.well-known/terratunnel`
- **Professional logging** with timestamps
- **Thread-safe** connection management
- **Graceful shutdown** handling

## Quick Start

### Server Mode

Start a tunnel server on your public server:

```bash
# Using Docker
docker run -p 8000:8000 ghcr.io/terrateamio/terratunnel:latest server --domain yourdomain.com

# Or from source
python -m terratunnel server --domain yourdomain.com

# Or using environment variables
export TERRATUNNEL_DOMAIN=yourdomain.com
python -m terratunnel server
```

### Client Mode

Connect your local development server:

```bash
# Using Docker
docker run ghcr.io/terrateamio/terratunnel:latest client --local-endpoint http://host.docker.internal:3000

# Or from source
python -m terratunnel client --local-endpoint http://localhost:3000

# Or using environment variables
export TERRATUNNEL_LOCAL_ENDPOINT=http://localhost:3000
python -m terratunnel client
```

Your local server will be accessible at `https://random123.yourdomain.com`

## Installation

### Option 1: Docker (Recommended)

```bash
# Pull from GitHub Container Registry
docker pull ghcr.io/terrateamio/terratunnel:latest

# Run server
docker run -p 8000:8000 ghcr.io/terrateamio/terratunnel:latest server --domain yourdomain.com

# Run client
docker run ghcr.io/terrateamio/terratunnel:latest client --local-endpoint http://host.docker.internal:3000
```

### Option 2: Install from Source

```bash
# Clone the repository
git clone https://github.com/terrateamio/terratunnel.git
cd terratunnel

# Install dependencies
pip install -r requirements.txt

# Run using Python module
python -m terratunnel --help
python -m terratunnel server --domain yourdomain.com
python -m terratunnel client --local-endpoint http://localhost:3000
```

## Docker Usage

### Server Mode with Docker

```bash
# Run server on port 8000
docker run -p 8000:8000 ghcr.io/terrateamio/terratunnel:latest server --domain yourdomain.com

# Run server with GitHub validation
docker run -p 8000:8000 ghcr.io/terrateamio/terratunnel:latest server --github-only --domain yourdomain.com

# Run with custom port
docker run -p 9000:9000 ghcr.io/terrateamio/terratunnel:latest server --port 9000 --domain yourdomain.com
```

### Client Mode with Docker

```bash
# Basic client connection
docker run --network host ghcr.io/terrateamio/terratunnel:latest client --local-endpoint http://localhost:3000

# Client with dashboard (expose dashboard port)
docker run -p 8080:8080 -p 8081:8081 --network host ghcr.io/terrateamio/terratunnel:latest client \
  --local-endpoint http://localhost:3000 \
  --dashboard

# Client with GitHub webhook integration
docker run --network host \
  -e GITHUB_APP_ID="123456" \
  -e GITHUB_APP_PEM="$(cat private-key.pem)" \
  ghcr.io/terrateamio/terratunnel:latest client \
  --local-endpoint http://localhost:3000 \
  --update-github-webhook
```

### Docker Compose Example

```yaml
version: '3.8'
services:
  terratunnel-server:
    image: ghcr.io/terrateamio/terratunnel:latest
    command: server --domain tunnel.example.com --github-only
    ports:
      - "8000:8000"
    restart: unless-stopped

  terratunnel-client:
    image: ghcr.io/terrateamio/terratunnel:latest
    command: client --local-endpoint http://host.docker.internal:3000 --dashboard
    ports:
      - "8080:8080"
      - "8081:8081"
    environment:
      - GITHUB_APP_ID=${GITHUB_APP_ID}
      - GITHUB_APP_PEM=${GITHUB_APP_PEM}
    restart: unless-stopped
    depends_on:
      - terratunnel-server
```

## Usage

### Server Options

```bash
python -m terratunnel server [OPTIONS]

Options:
  --host TEXT               Host to bind (default: 0.0.0.0, env: TERRATUNNEL_HOST)
  --port INTEGER           Port to bind (default: 8000, env: TERRATUNNEL_PORT)
  --domain TEXT            Domain name for tunnel hostnames (default: tunnel.terrateam.dev, env: TERRATUNNEL_DOMAIN)
  --github-only            Only allow requests from GitHub webhook IPs (env: TERRATUNNEL_GITHUB_ONLY)
  --validate-endpoint      Validate endpoint ownership via .well-known/terratunnel (env: TERRATUNNEL_VALIDATE_ENDPOINT)
```

### Client Options

```bash
python -m terratunnel client --local-endpoint URL [OPTIONS]

Options:
  --server-url TEXT         Server URL to connect to (default: ws://tunnel.terrateam.dev, env: TERRATUNNEL_SERVER_URL)
  --local-endpoint TEXT     Local endpoint to forward to [REQUIRED] (env: TERRATUNNEL_LOCAL_ENDPOINT)
  --dashboard              Enable webhook dashboard (env: TERRATUNNEL_DASHBOARD)
  --dashboard-port INTEGER  Dashboard port (default: 8080, env: TERRATUNNEL_DASHBOARD_PORT)
  --api-port INTEGER       JSON API port (default: 8081, env: TERRATUNNEL_API_PORT)
  --update-github-webhook  Automatically update GitHub App webhook URL when tunnel connects (env: TERRATUNNEL_UPDATE_GITHUB_WEBHOOK)
```

### Global Options

```bash
python -m terratunnel --log-level LEVEL COMMAND

Options:
  --log-level              Log level (default: INFO, env: TERRATUNNEL_LOG_LEVEL)
                          Available: DEBUG, INFO, WARNING, ERROR
```

## Examples

### Basic Tunneling

```bash
# Start server (using command line)
python -m terratunnel server --domain tunnel.example.com

# Or using environment variables
export TERRATUNNEL_DOMAIN=tunnel.example.com
python -m terratunnel server

# Connect client (using command line)
python -m terratunnel client --local-endpoint http://localhost:3000

# Or using environment variables
export TERRATUNNEL_LOCAL_ENDPOINT=http://localhost:3000
python -m terratunnel client
```

### Environment Variables Setup

```bash
# Complete environment setup
export TERRATUNNEL_LOG_LEVEL=INFO
export TERRATUNNEL_DOMAIN=tunnel.example.com
export TERRATUNNEL_LOCAL_ENDPOINT=http://localhost:3000
export TERRATUNNEL_DASHBOARD=true
export TERRATUNNEL_GITHUB_ONLY=true

# Run with minimal commands
python -m terratunnel server &
python -m terratunnel client
```

### GitHub Webhooks with Auto-Update

```bash
# Server with GitHub IP validation (using env vars)
export TERRATUNNEL_GITHUB_ONLY=true
python -m terratunnel server

# Client with GitHub App webhook auto-update
export TERRATUNNEL_LOCAL_ENDPOINT=http://localhost:3000
export TERRATUNNEL_DASHBOARD=true
export TERRATUNNEL_UPDATE_GITHUB_WEBHOOK=true
export GITHUB_APP_ID="123456"
export GITHUB_APP_PEM="-----BEGIN PRIVATE KEY-----
...your GitHub App private key...
-----END PRIVATE KEY-----"
export WEBHOOK_PATH="/webhook"

python -m terratunnel client
```

### Domain Validation

```bash
# Server with endpoint validation
export TERRATUNNEL_VALIDATE_ENDPOINT=true
python -m terratunnel server

# Client must serve validation endpoint at:
# http://localhost:3000/.well-known/terratunnel
```

## GitHub App Integration

Terratunnel can automatically update your GitHub App's webhook URL when a tunnel is established. This eliminates the need to manually update webhook URLs every time you start a new tunnel session.

### Setup

1. **Create a GitHub App** in your GitHub organization or personal account
2. **Generate a private key** for your GitHub App and download the PEM file
3. **Set environment variables**:

```bash
export GITHUB_APP_ID="your_app_id_here"
export GITHUB_APP_PEM="$(cat path/to/your/private-key.pem)"
export WEBHOOK_PATH="/webhook"  # Optional, defaults to /webhook
```

4. **Start the client** with the `--update-github-webhook` flag:

```bash
python -m terratunnel client --local-endpoint http://localhost:3000 --update-github-webhook
```

### How It Works

1. When the tunnel client connects and receives a hostname (e.g., `abc123.example.com`)
2. If the `--update-github-webhook` flag is used and GitHub App environment variables are set, the client automatically:
   - Creates a JWT token using your GitHub App's private key
   - Calls GitHub's API to update the webhook URL to `https://abc123.example.com/webhook`
   - Logs the success or failure of the operation

### Environment Variables

- `GITHUB_APP_ID` - Your GitHub App's ID (found in app settings)
- `GITHUB_APP_PEM` - Your GitHub App's private key in PEM format
- `WEBHOOK_PATH` - The path for webhooks (default: "/webhook")  
- `GITHUB_API_ENDPOINT` - GitHub API base URL (default: "https://api.github.com")

## Features in Detail

### Webhook Dashboard

When enabled with `--dashboard`, provides a web interface at `http://localhost:8080` showing:

- Real-time webhook history
- Request/response details
- Manual webhook retry functionality
- Connection status and statistics

### JSON API

Always available at `http://localhost:8081` with endpoints:

- `GET /status` - Connection status and statistics
- `GET /webhooks` - Webhook history
- `GET /webhooks/stats` - Webhook statistics  
- `GET /health` - Health check

### Security Features

**GitHub IP Validation (`--github-only`)**
- Validates requests against GitHub's webhook IP ranges
- Automatically updates IP ranges from GitHub API
- Supports both IPv4 and IPv6

**Domain Validation (`--validate-endpoint`)**
- Validates endpoint ownership before allowing tunnels
- Requires serving validation token at `/.well-known/terratunnel`
- Caches validation results with TTL

**GitHub App Integration**
- Automatically updates GitHub App webhook URLs when tunnel connects
- Uses JWT authentication with GitHub App private key
- Supports custom webhook paths and API endpoints
- Provides detailed logging of webhook update operations

### Admin Interface

The server includes a secure admin interface accessible only from private IPs:

- **Dashboard** (`/_admin`) - Visual overview of all active tunnels
- **API** (`/_admin/api/tunnels`) - JSON data for programmatic access

To access the admin interface in production:

```bash
# Using Fly.io
fly proxy 9090:8000 -a terratunnel
open http://localhost:9090/_admin

# Or locally
python -m terratunnel server
open http://localhost:8000/_admin
```

Features:
- Real-time tunnel status with connection indicators
- Clickable tunnel URLs for testing
- Server configuration display
- Auto-refreshes every 10 seconds

### Logging

Comprehensive logging with timestamps and descriptive logger names:

```
2024-01-20 10:30:45,123 - terratunnel-server - INFO - Starting tunnel server on 0.0.0.0:8000
2024-01-20 10:30:52,456 - terratunnel-client - INFO - Tunnel ready! Access at: https://abc123.example.com
```

## Development

### Running Tests

```bash
# Run all tests (fast, < 1 second)
python -m pytest

# Run with verbose output
python -m pytest -v

# Run specific test file
python -m pytest tests/test_cli.py
```

### Project Structure

```
terratunnel/
├── __init__.py         # Package initialization
├── __main__.py         # Module entry point
├── cli.py              # CLI interface
├── server/
│   └── app.py          # Server implementation
├── client/
│   └── app.py          # Client implementation
├── tests/              # Fast, simple tests
└── requirements.txt    # Dependencies
```

## Configuration

### Environment Variables

**Terratunnel Configuration:**
- `TERRATUNNEL_LOG_LEVEL` - Set logging level (DEBUG, INFO, WARNING, ERROR)
- `TERRATUNNEL_HOST` - Server host to bind (default: 0.0.0.0)
- `TERRATUNNEL_PORT` - Server port to bind (default: 8000)
- `TERRATUNNEL_DOMAIN` - Domain name for tunnel hostnames (default: tunnel.terrateam.dev)
- `TERRATUNNEL_GITHUB_ONLY` - Only allow GitHub webhook IPs (true/false)
- `TERRATUNNEL_VALIDATE_ENDPOINT` - Validate endpoint ownership (true/false)
- `TERRATUNNEL_SERVER_URL` - Server URL to connect to (default: ws://tunnel.terrateam.dev)
- `TERRATUNNEL_LOCAL_ENDPOINT` - Local endpoint to forward to (required for client)
- `TERRATUNNEL_DASHBOARD` - Enable webhook dashboard (true/false)
- `TERRATUNNEL_DASHBOARD_PORT` - Dashboard port (default: 8080)
- `TERRATUNNEL_API_PORT` - JSON API port (default: 8081)
- `TERRATUNNEL_UPDATE_GITHUB_WEBHOOK` - Auto-update GitHub webhook URL (true/false)

**GitHub App Integration:**
- `GITHUB_APP_ID` - GitHub App ID for webhook auto-update
- `GITHUB_APP_PEM` - GitHub App private key (PEM format)
- `WEBHOOK_PATH` - Webhook endpoint path (default: "/webhook")
- `GITHUB_API_ENDPOINT` - GitHub API base URL (default: "https://api.github.com")

### Server Configuration

The server validates GitHub webhook IPs and domain ownership automatically. No additional configuration files needed.

### Client Configuration

The client automatically discovers and connects to the tunnel server. Dashboard and API servers run on configurable ports.

## API Reference

### Client API Endpoints

#### GET /status
Returns client connection status and statistics.

```json
{
  "running": true,
  "tunnel_hostname": "abc123.example.com",
  "local_endpoint": "http://localhost:3000",
  "webhook_count": 42,
  "connection_start_time": "2024-01-20T10:30:45Z"
}
```

#### GET /webhooks
Returns webhook history with details.

```json
{
  "webhooks": [...],
  "total": 42,
  "tunnel_hostname": "abc123.example.com"
}
```

## Deployment

### Quick Deploy to Fly.io

The easiest way to deploy Terratunnel:

```bash
# Install Fly CLI and deploy
curl -L https://fly.io/install.sh | sh
fly auth login
fly launch
```

See [deploy/fly-setup.md](deploy/fly-setup.md) for detailed instructions.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests for new functionality
5. Ensure all tests pass: `python -m pytest`
6. Submit a pull request

## License

MPL-2.0 License - see LICENSE file for details.

## Acknowledgments

Inspired by ngrok and similar tunneling tools, built with modern Python async/await patterns and FastAPI.
