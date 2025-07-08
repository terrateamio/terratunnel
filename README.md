# Terratunnel

Expose your local development server to the internet through secure WebSocket tunnels.

## Quick Start

### Using Docker

**Server** (on your public server):
```bash
docker run -p 8000:8000 ghcr.io/terrateamio/terratunnel:latest server --domain yourdomain.com
```

**Client** (on your development machine):
```bash
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
- `--github-only` - Only allow GitHub webhook IPs

**Client:**
- `--local-endpoint` - Local URL to forward to (required)
- `--server-url` - Tunnel server URL (default: ws://tunnel.terrateam.dev)
- `--dashboard` - Enable web dashboard on port 8080

All options can be set via environment variables with `TERRATUNNEL_` prefix.

## Features

- **WebSocket-based tunneling** - Fast, real-time connection
- **Secure by default** - HTTPS support with custom domains
- **Optional dashboard** - Monitor webhooks and requests
- **GitHub integration** - Auto-update webhook URLs, IP filtering
- **Simple CLI** - Easy to use command-line interface
- **Docker ready** - Pre-built images available

## License

MPL-2.0
