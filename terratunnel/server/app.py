import asyncio
import logging
import threading
import secrets
import string
import json
import uuid
import time
import ipaddress
import os
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, List, Union
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import JSONResponse, Response, HTMLResponse
import uvicorn
import httpx


logger = logging.getLogger("terratunnel-server")


class DomainValidator:
    def __init__(self):
        self.validation_cache: Dict[str, Dict] = {}
        self.cache_duration = 3600  # 1 hour in seconds
        self.validation_freshness = 86400  # 24 hours in seconds
        self.lock = threading.Lock()
    
    async def validate_domain(self, endpoint: str) -> bool:
        """Validate endpoint ownership by checking .well-known/terratunnel endpoint"""
        # Skip validation for local/private endpoints
        host_part = endpoint.split(':')[0] if ':' in endpoint else endpoint
        is_local = (
            host_part in ['localhost', '127.0.0.1', '::1'] or 
            host_part.startswith('192.168.') or 
            host_part.startswith('10.') or 
            host_part.startswith('172.') or
            host_part.startswith('169.254.') or  # Link-local
            host_part.startswith('fc00:') or  # IPv6 ULA
            host_part.startswith('fe80:')  # IPv6 link-local
        )
        
        if is_local:
            logger.info(f"     Skipping validation for local/private endpoint: {endpoint}")
            return True
        
        # Check cache first
        with self.lock:
            cached = self.validation_cache.get(endpoint)
            if cached and time.time() - cached["cached_at"] < self.cache_duration:
                return cached["valid"]
        
        try:
            # Make HTTP GET request to endpoint validation endpoint
            async with httpx.AsyncClient() as client:
                protocol = "https"  # Public endpoints should use HTTPS
                validation_url = f"{protocol}://{endpoint}/.well-known/terratunnel"
                logger.info(f"     Validating endpoint ownership: {validation_url}")
                
                response = await client.get(validation_url, timeout=10.0)
                
                if response.status_code != 200:
                    logger.warning(f"     Endpoint validation failed - HTTP {response.status_code}: {endpoint}")
                    self._cache_result(endpoint, False)
                    return False
                
                # Parse JSON response
                try:
                    data = response.json()
                except json.JSONDecodeError:
                    logger.warning(f"     Endpoint validation failed - Invalid JSON: {endpoint}")
                    self._cache_result(endpoint, False)
                    return False
                
                # Validate response format
                if not self._validate_response_format(data):
                    logger.warning(f"     Endpoint validation failed - Invalid format: {endpoint}")
                    self._cache_result(endpoint, False)
                    return False
                
                # Validate timestamp freshness
                if not self._validate_timestamp_freshness(data.get("timestamp")):
                    logger.warning(f"     Endpoint validation failed - Stale timestamp: {endpoint}")
                    self._cache_result(endpoint, False)
                    return False
                
                logger.info(f"     Endpoint validation successful: {endpoint}")
                self._cache_result(endpoint, True)
                return True
                
        except Exception as e:
            logger.warning(f"     Endpoint validation error for {endpoint}: {e}")
            self._cache_result(endpoint, False)
            return False
    
    def _validate_response_format(self, data: dict) -> bool:
        """Validate the JSON response format"""
        required_fields = ["tunnel_validation", "timestamp"]
        
        for field in required_fields:
            if field not in data:
                return False
        
        if data.get("tunnel_validation") != "terratunnel-v1":
            return False
        
        return True
    
    def _validate_timestamp_freshness(self, timestamp_str: str) -> bool:
        """Validate that timestamp is within 24 hours"""
        try:
            # Parse ISO8601 timestamp
            timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            
            # Ensure timestamp is timezone-aware
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            
            now = datetime.now(timezone.utc)
            age = now - timestamp
            
            # Check if timestamp is within validation freshness window
            return age.total_seconds() <= self.validation_freshness
            
        except (ValueError, TypeError) as e:
            logger.warning(f"     Invalid timestamp format: {timestamp_str} - {e}")
            return False
    
    def _cache_result(self, endpoint: str, valid: bool):
        """Cache validation result"""
        with self.lock:
            self.validation_cache[endpoint] = {
                "valid": valid,
                "cached_at": time.time()
            }
    
    def should_revalidate(self, endpoint: str, connection_start: float) -> bool:
        """Check if endpoint should be revalidated for long-lived connections"""
        return time.time() - connection_start > self.validation_freshness


class GitHubIPValidator:
    def __init__(self):
        self.hook_ranges: List[ipaddress.IPv4Network] = []
        self.hook_ranges_v6: List[ipaddress.IPv6Network] = []
        self.last_updated = 0
        self.cache_duration = 3600  # 1 hour in seconds
        self.lock = threading.Lock()
    
    async def update_hook_ranges(self):
        """Fetch GitHub hook IP ranges from the API"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get("https://api.github.com/meta", timeout=10.0)
                response.raise_for_status()
                data = response.json()
                
                hook_cidrs = data.get("hooks", [])
                ranges_v4 = []
                ranges_v6 = []
                
                for cidr in hook_cidrs:
                    try:
                        # Try IPv4 first
                        if ':' not in cidr:
                            ranges_v4.append(ipaddress.IPv4Network(cidr, strict=False))
                        else:
                            ranges_v6.append(ipaddress.IPv6Network(cidr, strict=False))
                    except ValueError as e:
                        logger.warning(f"Invalid CIDR range from GitHub API: {cidr} - {e}")
                
                with self.lock:
                    self.hook_ranges = ranges_v4
                    self.hook_ranges_v6 = ranges_v6
                    self.last_updated = time.time()
                
                logger.info(f"     Updated GitHub hook IP ranges: {len(ranges_v4)} IPv4 + {len(ranges_v6)} IPv6 ranges loaded")
                return True
                
        except Exception as e:
            logger.error(f"     Failed to fetch GitHub hook IP ranges: {e}")
            return False
    
    async def is_github_hook_ip(self, ip_str: str) -> bool:
        """Check if an IP address is in GitHub's hook ranges"""
        # Update cache if expired
        if time.time() - self.last_updated > self.cache_duration:
            await self.update_hook_ranges()
        
        if not self.hook_ranges and not self.hook_ranges_v6:
            # If we don't have ranges loaded, allow all (fail open)
            logger.warning("     No GitHub hook IP ranges loaded, allowing request")
            return True
        
        try:
            # Try to parse as IPv4 first
            if ':' not in ip_str:
                ip = ipaddress.IPv4Address(ip_str)
                with self.lock:
                    for network in self.hook_ranges:
                        if ip in network:
                            return True
            else:
                # Parse as IPv6
                ip = ipaddress.IPv6Address(ip_str)
                with self.lock:
                    for network in self.hook_ranges_v6:
                        if ip in network:
                            return True
            
            return False
            
        except ValueError:
            logger.warning(f"     Invalid IP address format: {ip_str}")
            return False


class ConnectionManager:
    def __init__(self, domain: str = "tunnel.terrateam.dev"):
        self.active_connections: Dict[str, WebSocket] = {}
        self.hostname_to_subdomain: Dict[str, str] = {}
        self.subdomain_to_endpoint: Dict[str, str] = {}
        self.pending_requests: Dict[str, asyncio.Future] = {}
        self.connection_start_times: Dict[str, float] = {}
        self.validated_endpoints: Dict[str, bool] = {}  # Track validation state per subdomain
        self.domain = domain
        self.lock = threading.Lock()

    def generate_subdomain(self) -> str:
        chars = string.ascii_lowercase + string.digits
        return ''.join(secrets.choice(chars) for _ in range(8))


    def disconnect(self, subdomain: str):
        with self.lock:
            if subdomain in self.active_connections:
                del self.active_connections[subdomain]
            
            if subdomain in self.connection_start_times:
                del self.connection_start_times[subdomain]
            
            if subdomain in self.validated_endpoints:
                del self.validated_endpoints[subdomain]
            
            if subdomain in self.subdomain_to_endpoint:
                del self.subdomain_to_endpoint[subdomain]
            
            hostname_to_remove = None
            for hostname, sub in self.hostname_to_subdomain.items():
                if sub == subdomain:
                    hostname_to_remove = hostname
                    break
            
            if hostname_to_remove:
                del self.hostname_to_subdomain[hostname_to_remove]

    def get_subdomain_from_hostname(self, hostname: str) -> Optional[str]:
        with self.lock:
            return self.hostname_to_subdomain.get(hostname)
    
    def get_endpoint_from_subdomain(self, subdomain: str) -> Optional[str]:
        with self.lock:
            return self.subdomain_to_endpoint.get(subdomain)
    
    def set_endpoint_for_subdomain(self, subdomain: str, endpoint: str):
        with self.lock:
            self.subdomain_to_endpoint[subdomain] = endpoint

    async def send_request(self, subdomain: str, request_data: dict) -> Optional[dict]:
        with self.lock:
            websocket = self.active_connections.get(subdomain)
        
        if not websocket:
            return None
        
        # Generate unique request ID
        request_id = str(uuid.uuid4())
        request_data["request_id"] = request_id
        
        # Create future for response
        future = asyncio.Future()
        
        with self.lock:
            self.pending_requests[request_id] = future
        
        try:
            await websocket.send_text(json.dumps(request_data))
            # Wait for response with timeout
            response = await asyncio.wait_for(future, timeout=30.0)
            return response
        except asyncio.TimeoutError:
            logger.error(f"Timeout waiting for response from {subdomain}")
            with self.lock:
                self.pending_requests.pop(request_id, None)
            self.disconnect(subdomain)
            return None
        except Exception as e:
            logger.error(f"Error sending request to {subdomain}: {e}")
            with self.lock:
                self.pending_requests.pop(request_id, None)
            self.disconnect(subdomain)
            return None

    def handle_response(self, response_data: dict):
        request_id = response_data.get("request_id")
        if not request_id:
            return
        
        with self.lock:
            future = self.pending_requests.pop(request_id, None)
        
        if future and not future.done():
            future.set_result(response_data)


app = FastAPI(title="Tunnel Server")
manager = None
github_validator = None
domain_validator = None
github_only_mode = False
domain_validation_mode = False


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    subdomain = None
    hostname = None
    
    try:
        # Wait for client info first
        client_info_text = await websocket.receive_text()
        client_info = json.loads(client_info_text)
        
        if client_info.get("type") != "client_info":
            await websocket.close(code=1008, reason="Expected client_info message")
            return
        
        local_endpoint = client_info.get("local_endpoint")
        if not local_endpoint:
            await websocket.close(code=1008, reason="Missing local_endpoint")
            return
        
        # Generate subdomain and complete connection FIRST
        subdomain = manager.generate_subdomain()
        hostname = f"{subdomain}.{manager.domain}"
        
        with manager.lock:
            manager.active_connections[subdomain] = websocket
            manager.hostname_to_subdomain[hostname] = subdomain
            manager.subdomain_to_endpoint[subdomain] = local_endpoint
            manager.connection_start_times[subdomain] = time.time()
        
        logger.info(f"     WebSocket client connected: {hostname} -> {local_endpoint}")
        
        # Send hostname assignment immediately
        await websocket.send_text(json.dumps({
            "type": "hostname_assigned",
            "hostname": hostname,
            "subdomain": subdomain
        }))
        
        # NOTE: Endpoint validation removed here - will be done on first request instead
        # This allows the local endpoint to start up after the tunnel is established
        
        while True:
            try:
                # Use a timeout to avoid blocking indefinitely
                message_text = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                message = json.loads(message_text)
                
                if message.get("type") == "keepalive_ack":
                    continue
                elif message.get("request_id"):
                    # Handle response from client
                    manager.handle_response(message)
                    continue
                    
            except asyncio.TimeoutError:
                # Send periodic keepalive to detect disconnections
                try:
                    await websocket.send_text(json.dumps({"type": "keepalive"}))
                except:
                    break
            except WebSocketDisconnect:
                logger.info(f"     WebSocket client disconnected: {subdomain}")
                break
            except Exception as e:
                # Check if it's a normal WebSocket close
                if hasattr(e, 'code') and e.code == 1000:
                    logger.info(f"     WebSocket client disconnected: {subdomain}")
                else:
                    logger.error(f"     WebSocket error: {e}")
                break
    except WebSocketDisconnect:
        if subdomain:
            logger.info(f"     WebSocket client disconnected: {subdomain}")
    finally:
        if subdomain:
            manager.disconnect(subdomain)


@app.get("/_health")
async def health_check():
    return {"status": "healthy", "active_connections": len(manager.active_connections)}


@app.get("/_admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    """Admin dashboard - protected by API key"""
    # Check for admin API key
    admin_key = os.getenv("TERRATUNNEL_ADMIN_KEY")
    if admin_key:
        # Check header
        provided_key = request.headers.get("x-admin-key")
        # Also check query parameter for browser access
        if not provided_key:
            provided_key = request.query_params.get("key")
        
        if provided_key != admin_key:
            logger.warning(f"Admin access denied - invalid key")
            raise HTTPException(403, "Access denied - invalid admin key")
    
    # Gather tunnel data
    tunnels = []
    with manager.lock:
        for hostname, subdomain in manager.hostname_to_subdomain.items():
            endpoint = manager.subdomain_to_endpoint.get(subdomain)
            websocket = manager.active_connections.get(subdomain)
            tunnels.append({
                "hostname": hostname,
                "subdomain": subdomain,
                "endpoint": endpoint,
                "connected": websocket is not None,
                "url": f"https://{hostname}"
            })
    
    # Sort by hostname
    tunnels.sort(key=lambda x: x["hostname"])
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Terratunnel Admin</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                margin: 0;
                padding: 20px;
                background: #f5f5f5;
            }}
            .container {{
                max-width: 1200px;
                margin: 0 auto;
                background: white;
                padding: 20px;
                border-radius: 8px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }}
            h1 {{
                margin-top: 0;
                color: #333;
            }}
            .stats {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 20px;
                margin-bottom: 30px;
            }}
            .stat {{
                padding: 20px;
                background: #f8f9fa;
                border-radius: 8px;
                text-align: center;
            }}
            .stat-value {{
                font-size: 2em;
                font-weight: bold;
                color: #0066cc;
            }}
            .stat-label {{
                color: #666;
                margin-top: 5px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
            }}
            th, td {{
                padding: 12px;
                text-align: left;
                border-bottom: 1px solid #eee;
            }}
            th {{
                background: #f8f9fa;
                font-weight: 600;
                color: #333;
            }}
            tr:hover {{
                background: #f8f9fa;
            }}
            .status {{
                display: inline-block;
                width: 10px;
                height: 10px;
                border-radius: 50%;
                margin-right: 5px;
            }}
            .connected {{
                background: #00d084;
            }}
            .disconnected {{
                background: #ff6b6b;
            }}
            .url {{
                color: #0066cc;
                text-decoration: none;
            }}
            .url:hover {{
                text-decoration: underline;
            }}
            .endpoint {{
                font-family: monospace;
                font-size: 0.9em;
                color: #666;
            }}
            .refresh {{
                margin-top: 20px;
                text-align: center;
                color: #666;
                font-size: 0.9em;
            }}
            .empty {{
                text-align: center;
                padding: 40px;
                color: #666;
            }}
            .header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 20px;
            }}
            .settings {{
                color: #666;
                font-size: 0.9em;
            }}
        </style>
        <meta http-equiv="refresh" content="10">
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🚇 Terratunnel Admin</h1>
                <div class="settings">
                    Domain: {manager.domain} | 
                    GitHub-only: {'✓' if github_only_mode else '✗'} |
                    Validation: {'✓' if domain_validation_mode else '✗'}
                </div>
            </div>
            
            <div class="stats">
                <div class="stat">
                    <div class="stat-value">{len(tunnels)}</div>
                    <div class="stat-label">Active Tunnels</div>
                </div>
                <div class="stat">
                    <div class="stat-value">{len([t for t in tunnels if t['connected']])}</div>
                    <div class="stat-label">Connected</div>
                </div>
                <div class="stat">
                    <div class="stat-value">{len(manager.active_connections)}</div>
                    <div class="stat-label">WebSocket Connections</div>
                </div>
            </div>
            
            <h2>Active Tunnels</h2>
            """
    
    if tunnels:
        html += """
            <table>
                <thead>
                    <tr>
                        <th>Status</th>
                        <th>Hostname</th>
                        <th>Local Endpoint</th>
                        <th>Subdomain</th>
                    </tr>
                </thead>
                <tbody>
        """
        for tunnel in tunnels:
            status_class = 'connected' if tunnel['connected'] else 'disconnected'
            status_text = 'Connected' if tunnel['connected'] else 'Disconnected'
            endpoint = tunnel['endpoint'] or 'N/A'
            
            html += f"""
                    <tr>
                        <td>
                            <span class="status {status_class}"></span>
                            {status_text}
                        </td>
                        <td>
                            <a href="{tunnel['url']}" target="_blank" class="url">{tunnel['hostname']}</a>
                        </td>
                        <td>
                            <span class="endpoint">{endpoint}</span>
                        </td>
                        <td>{tunnel['subdomain']}</td>
                    </tr>
            """
        html += """
                </tbody>
            </table>
        """
    else:
        html += '<div class="empty">No active tunnels</div>'
    
    html += """
            
            <div class="refresh">
                Auto-refreshing every 10 seconds | <a href="/_admin">Refresh now</a>
            </div>
        </div>
    </body>
    </html>
    """
    
    return HTMLResponse(content=html)


@app.get("/_admin/api/tunnels")
async def admin_api_tunnels(request: Request):
    """Admin API endpoint - returns JSON data"""
    # Check for admin API key
    admin_key = os.getenv("TERRATUNNEL_ADMIN_KEY")
    if admin_key:
        # Check header
        provided_key = request.headers.get("x-admin-key")
        # Also check query parameter
        if not provided_key:
            provided_key = request.query_params.get("key")
        
        if provided_key != admin_key:
            logger.warning(f"Admin API access denied - invalid key")
            raise HTTPException(403, "Access denied - invalid admin key")
    
    tunnels = {}
    with manager.lock:
        for hostname, subdomain in manager.hostname_to_subdomain.items():
            tunnels[hostname] = {
                "subdomain": subdomain,
                "endpoint": manager.subdomain_to_endpoint.get(subdomain),
                "connected": subdomain in manager.active_connections
            }
    
    return {
        "tunnels": tunnels,
        "stats": {
            "total_tunnels": len(tunnels),
            "connected": len([t for t in tunnels.values() if t["connected"]]),
            "websocket_connections": len(manager.active_connections)
        },
        "config": {
            "domain": manager.domain,
            "github_only": github_only_mode,
            "validation_required": domain_validation_mode
        }
    }


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy_request(request: Request, path: str):
    # GitHub IP validation if enabled
    if github_only_mode and github_validator:
        client_ip = request.client.host if request.client else "unknown"
        
        # Check X-Forwarded-For header first (common in reverse proxy setups)
        forwarded_for = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded_for:
            client_ip = forwarded_for
        
        # Real IP header (some proxy configurations)
        real_ip = request.headers.get("x-real-ip", "").strip()
        if real_ip:
            client_ip = real_ip
        
        if not await github_validator.is_github_hook_ip(client_ip):
            logger.warning(f"     Blocked non-GitHub IP: {client_ip}")
            raise HTTPException(status_code=403, detail="Access denied: Not a GitHub webhook IP")
    
    host_header = request.headers.get("host", "")
    
    if not host_header:
        raise HTTPException(status_code=400, detail="Host header required")
    
    # Strip port from host header if present (e.g., "example.com:8000" -> "example.com")
    hostname = host_header.split(':')[0]
    
    subdomain = manager.get_subdomain_from_hostname(hostname)
    if not subdomain:
        raise HTTPException(status_code=404, detail=f"Hostname {hostname} not found")
    
    # Endpoint validation if enabled (for long-lived connections)
    if domain_validation_mode and domain_validator:
        local_endpoint = manager.get_endpoint_from_subdomain(subdomain)
        if local_endpoint:
            # Check if we need to revalidate for long-lived connections
            with manager.lock:
                connection_start = manager.connection_start_times.get(subdomain, time.time())
            
            # Parse endpoint to get validation format
            try:
                from urllib.parse import urlparse
                parsed_endpoint = urlparse(local_endpoint)
                validation_host = parsed_endpoint.hostname or 'localhost'
                validation_port = parsed_endpoint.port or (443 if parsed_endpoint.scheme == 'https' else 80)
                validation_endpoint = f"{validation_host}:{validation_port}"
                
                # Skip revalidation for local endpoints
                host_part = validation_endpoint.split(':')[0] if ':' in validation_endpoint else validation_endpoint
                is_local = (
                    host_part in ['localhost', '127.0.0.1', '::1'] or 
                    host_part.startswith('192.168.') or 
                    host_part.startswith('10.') or 
                    host_part.startswith('172.') or
                    host_part.startswith('169.254.') or
                    host_part.startswith('fc00:') or
                    host_part.startswith('fe80:')
                )
                
                if not is_local and domain_validator.should_revalidate(validation_endpoint, connection_start):
                    logger.info(f"     Re-validating endpoint for long-lived connection: {validation_endpoint}")
                    if not await domain_validator.validate_domain(validation_endpoint):
                        logger.warning(f"     Endpoint validation failed on revalidation: {validation_endpoint}")
                        raise HTTPException(status_code=403, detail="Endpoint validation failed")
            except Exception as e:
                logger.warning(f"     Failed to parse endpoint URL for revalidation {local_endpoint}: {e}")
    
    # Check if this subdomain has been validated (first request validation)
    if domain_validation_mode and domain_validator:
        with manager.lock:
            validated = manager.validated_endpoints.get(subdomain, False)
        
        if not validated:
            local_endpoint = manager.get_endpoint_from_subdomain(subdomain)
            if local_endpoint:
                # Parse endpoint to get validation format
                try:
                    from urllib.parse import urlparse
                    parsed_endpoint = urlparse(local_endpoint)
                    validation_host = parsed_endpoint.hostname or 'localhost'
                    validation_port = parsed_endpoint.port or (443 if parsed_endpoint.scheme == 'https' else 80)
                    validation_endpoint = f"{validation_host}:{validation_port}"
                    
                    # Skip validation for local endpoints
                    host_part = validation_endpoint.split(':')[0] if ':' in validation_endpoint else validation_endpoint
                    is_local = (
                        host_part in ['localhost', '127.0.0.1', '::1'] or 
                        host_part.startswith('192.168.') or 
                        host_part.startswith('10.') or 
                        host_part.startswith('172.') or
                        host_part.startswith('169.254.') or
                        host_part.startswith('fc00:') or
                        host_part.startswith('fe80:')
                    )
                    
                    if not is_local:
                        logger.info(f"     Performing first-request validation for: {validation_endpoint}")
                        
                        # Validation with retry logic
                        max_retries = 3
                        retry_delay = 1.0
                        validation_success = False
                        
                        for attempt in range(max_retries + 1):
                            if await domain_validator.validate_domain(validation_endpoint):
                                validation_success = True
                                logger.info(f"     First-request validation successful: {validation_endpoint}")
                                break
                            elif attempt < max_retries:
                                logger.warning(f"     Validation attempt {attempt + 1}/{max_retries + 1} failed for {validation_endpoint}")
                                logger.info(f"     Retrying validation in {retry_delay} seconds...")
                                await asyncio.sleep(retry_delay)
                                retry_delay *= 2  # Exponential backoff
                        
                        if not validation_success:
                            logger.error(f"     First-request validation failed after {max_retries + 1} attempts: {validation_endpoint}")
                            raise HTTPException(status_code=403, detail="Endpoint validation failed")
                    
                    # Mark as validated
                    with manager.lock:
                        manager.validated_endpoints[subdomain] = True
                        
                except Exception as e:
                    logger.warning(f"     Failed to parse endpoint URL for validation {local_endpoint}: {e}")
                    # Continue anyway - don't block on parsing errors
    
    body = await request.body()
    
    request_data = {
        "method": request.method,
        "path": f"/{path}" if path else "/",
        "headers": dict(request.headers),
        "query_params": dict(request.query_params),
        "body": body.decode() if body else ""
    }
    
    response_data = await manager.send_request(subdomain, request_data)
    
    if response_data is None:
        raise HTTPException(status_code=502, detail="Service unavailable")
    
    # Filter out headers that FastAPI should not set manually
    response_headers = response_data.get("headers", {})
    filtered_headers = {}
    
    # Skip headers that cause conflicts with FastAPI's response handling
    skip_headers = {"content-length", "transfer-encoding", "connection"}
    
    for key, value in response_headers.items():
        if key.lower() not in skip_headers:
            filtered_headers[key] = value
    
    return Response(
        content=response_data.get("body", ""),
        status_code=response_data.get("status_code", 200),
        headers=filtered_headers
    )


async def init_github_validator():
    """Initialize GitHub IP validator and fetch initial ranges"""
    global github_validator
    github_validator = GitHubIPValidator()
    await github_validator.update_hook_ranges()


async def init_domain_validator():
    """Initialize domain validator"""
    global domain_validator
    domain_validator = DomainValidator()


def run_server(host: str = "0.0.0.0", port: int = 8000, domain: str = "tunnel.terrateam.dev", github_only: bool = False, validate_endpoint: bool = False):
    global manager, github_only_mode, domain_validation_mode
    manager = ConnectionManager(domain)
    github_only_mode = github_only
    domain_validation_mode = validate_endpoint
    
    # Build feature flags for logging
    features = []
    if github_only:
        features.append("GitHub webhooks only")
        # Initialize GitHub validator synchronously
        asyncio.run(init_github_validator())
    
    if validate_endpoint:
        features.append("endpoint validation")
        # Initialize domain validator synchronously
        asyncio.run(init_domain_validator())
    
    if features:
        feature_str = " (" + ", ".join(features) + ")"
    else:
        feature_str = ""
    
    logger.info(f"     Starting tunnel server on {host}:{port} with domain {domain}{feature_str}")
    
    # Configure uvicorn with custom logging
    uvicorn.run(
        app, 
        host=host, 
        port=port, 
        access_log=False,  # Disable default HTTP access logs
        log_config={
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                },
            },
            "handlers": {
                "default": {
                    "formatter": "default",
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                },
            },
            "root": {
                "level": "INFO",
                "handlers": ["default"],
            },
            "loggers": {
                "terratunnel-server": {"handlers": ["default"], "level": "INFO"},
                "uvicorn": {"handlers": ["default"], "level": "WARNING"},
                "uvicorn.error": {"handlers": ["default"], "level": "WARNING"},
                "uvicorn.access": {"handlers": [], "level": "INFO"},
            },
        }
    )