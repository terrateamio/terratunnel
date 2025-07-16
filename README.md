# Terratunnel

Expose your local development server to the internet through secure WebSocket tunnels.

## Quick Start

### Using Docker

**Server** (on your public server):
```bash
# For production with authentication
docker run -p 8000:8000 \
  -e GITHUB_CLIENT_ID=your_client_id \
  -e GITHUB_CLIENT_SECRET=your_client_secret \
  -e JWT_SECRET=your_jwt_secret \
  ghcr.io/terrateamio/terratunnel:latest server --domain yourdomain.com

# For development/testing without authentication
docker run -p 8000:8000 \
  -e REQUIRE_AUTH_FOR_TUNNELS=false \
  ghcr.io/terrateamio/terratunnel:latest server --domain yourdomain.com
```

**Client** (on your development machine):
```bash
# With authentication (default)
docker run \
  -e TERRATUNNEL_API_KEY=your_api_key \
  ghcr.io/terrateamio/terratunnel:latest client --local-endpoint http://host.docker.internal:3000

# Without authentication (if server allows)
docker run ghcr.io/terrateamio/terratunnel:latest client --local-endpoint http://host.docker.internal:3000
```

Your local server is now accessible at `https://random-subdomain.yourdomain.com`

### From Source

```bash
git clone https://github.com/terrateamio/terratunnel.git
cd terratunnel
pip install -r requirements.txt

# Server
python -m terratunnel server --domain yourdomain.com

# Client
python -m terratunnel client --local-endpoint http://localhost:3000
```

## Options

**Server:**
- `--domain` - Your domain for tunnel URLs (required)
- `--port` - Server port (default: 8000)

**Client:**
- `--local-endpoint` - Local URL to forward to (required)
- `--server-url` - Tunnel server URL (default: ws://tunnel.terrateam.dev)
- `--dashboard` - Enable web dashboard on port 8080

All options can be set via environment variables with `TERRATUNNEL_` prefix.

## Environment Variables

For OAuth authentication and API key support, set these environment variables:

```bash
# Required for GitHub OAuth
GITHUB_CLIENT_ID=your_github_client_id
GITHUB_CLIENT_SECRET=your_github_client_secret

# Required for JWT tokens
JWT_SECRET=your_secure_secret_key
```

See `.env.example` for all available environment variables.

### Admin Access

To grant admin access to specific users:

```bash
# Set admin users by OAuth provider
TERRATUNNEL_GITHUB_ADMIN_USERS=username1,username2
TERRATUNNEL_GITLAB_ADMIN_USERS=username3
TERRATUNNEL_GOOGLE_ADMIN_USERS=user@example.com
```

Admin users can access:
- `/_admin` - Web dashboard showing active tunnels and recent connections
- `/_admin/api/tunnels` - JSON API for tunnel information
- `/_admin/api/audit` - JSON API for audit logs

### OAuth Authentication

When GitHub OAuth is configured, the following endpoints are available:

- `/auth/login` - Login page with GitHub OAuth
- `/auth/github` - Redirect to GitHub for authentication
- `/auth/github/callback` - OAuth callback endpoint (configure this in your GitHub OAuth App)
- `/auth/me` - Get current user info (requires Bearer token)

### API Authentication

Terratunnel supports API key authentication for programmatic access:

- `POST /api/auth/exchange` - Exchange a GitHub token for a Terratunnel API key
- `GET /api/auth/me` - Get current user info (requires API key)
- `GET /api/auth/keys` - List all API keys for the current user
- `DELETE /api/auth/keys/{key_id}` - Revoke an API key

#### Example: Exchange GitHub token for API key

```bash
# Using a GitHub personal access token
curl -X POST https://your-server.com/api/auth/exchange \
  -H "Content-Type: application/json" \
  -d '{"github_token": "ghp_your_github_token", "api_key_name": "My CLI Key"}'

# Response:
{
  "api_key": "your_terratunnel_api_key",
  "api_key_prefix": "tt_1234",
  "user_id": 1,
  "username": "your-github-username",
  "expires_at": null
}
```

#### Using API keys

```bash
# Include the API key in the Authorization header
curl https://your-server.com/api/auth/me \
  -H "Authorization: Bearer your_terratunnel_api_key"
```

### Authentication for Tunnels

By default, authentication is required for tunnel creation. To disable authentication (not recommended for production):

1. Set `REQUIRE_AUTH_FOR_TUNNELS=false` on the server

Clients must provide an API key when authentication is enabled:

```bash
# Using environment variable
export TERRATUNNEL_API_KEY=your_api_key
python -m terratunnel client --local-endpoint http://localhost:3000

# Or using command line option
python -m terratunnel client --local-endpoint http://localhost:3000 --api-key your_api_key
```

## Features

- **WebSocket-based tunneling** - Fast, real-time connection
- **Secure by default** - HTTPS support with custom domains
- **Optional dashboard** - Monitor webhooks and requests
- **GitHub integration** - Auto-update webhook URLs
- **Simple CLI** - Easy to use command-line interface
- **Docker ready** - Pre-built images available

## License

MPL-2.0
