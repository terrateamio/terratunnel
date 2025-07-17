# Terratunnel

Expose your local development server to the internet through secure WebSocket tunnels. Perfect for webhook testing, sharing local development, and integrating with third-party services.

## What is Terratunnel?

Terratunnel creates a secure tunnel between your local development environment and a public URL. When someone makes a request to your tunnel URL, it gets forwarded to your local server through a WebSocket connection.

**Key Features:**
- Permanent tunnel URLs that don't change
- Secure WebSocket connections
- Built-in webhook dashboard
- Support for any local HTTP service
- No port forwarding or firewall configuration needed

## Quick Start

### 1. Get Your Tunnel URL

Visit https://tunnel.terrateam.dev and get your permanent tunnel URL.

### 2. Get Your API Key

From the dashboard, generate an API key for authentication.

### 3. Connect Your Local App

**Using Docker:**
```bash
docker run \
  -e TERRATUNNEL_API_KEY=your_api_key \
  ghcr.io/terrateamio/terratunnel:latest client \
  --local-endpoint http://host.docker.internal:3000
```

**From source:**
```bash
pip install -r requirements.txt
export TERRATUNNEL_API_KEY=your_api_key
python -m terratunnel client \
  --local-endpoint http://localhost:3000
```

Your local app is now accessible at your permanent tunnel URL.

## License

MPL-2.0
