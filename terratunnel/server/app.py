import asyncio
import logging
import threading
import secrets
import string
import json
import uuid
import time
import os
from typing import Dict, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, Response, HTMLResponse, RedirectResponse
import uvicorn
from .database import Database
from .config import Config
from .auth import auth_router, require_admin_user
from .api import api_router
from .middleware import AuthMiddleware


logger = logging.getLogger("terratunnel-server")


class ConnectionManager:
    def __init__(self, domain: str = "tunnel.terrateam.dev"):
        self.active_connections: Dict[str, WebSocket] = {}
        self.hostname_to_subdomain: Dict[str, str] = {}
        self.subdomain_to_endpoint: Dict[str, str] = {}
        self.pending_requests: Dict[str, asyncio.Future] = {}
        self.domain = domain
        self.lock = threading.Lock()

    def generate_subdomain(self) -> str:
        chars = string.ascii_lowercase + string.digits
        return ''.join(secrets.choice(chars) for _ in range(8))


    def disconnect(self, subdomain: str):
        with self.lock:
            if subdomain in self.active_connections:
                del self.active_connections[subdomain]
            
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
db = None
auth_middleware = None


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    subdomain = None
    hostname = None
    connection_id = str(uuid.uuid4())
    user_id = None
    api_key_id = None
    username = None
    
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
        
        # Authenticate using middleware
        if auth_middleware:
            auth_result = await auth_middleware.authenticate_websocket(websocket, client_info)
            if auth_result is None:
                # WebSocket was closed by middleware
                return
            user_id, api_key_id = auth_result
            
            if user_id:
                # Check user tunnel limit
                if not auth_middleware.check_user_tunnel_limit(user_id, manager.active_connections):
                    await websocket.close(code=1008, reason="Tunnel limit reached")
                    return
                
                # Get user info for logging
                user = db.get_user_by_id(user_id)
                if user:
                    username = user['provider_username']
                    logger.info(f"     Authenticated tunnel creation for user: {username} (ID: {user_id})")
        
        # Generate subdomain and complete connection FIRST
        subdomain = manager.generate_subdomain()
        hostname = f"{subdomain}.{manager.domain}"
        
        with manager.lock:
            manager.active_connections[subdomain] = websocket
            manager.hostname_to_subdomain[hostname] = subdomain
            manager.subdomain_to_endpoint[subdomain] = local_endpoint
        
        logger.info(f"     WebSocket client connected: {hostname} -> {local_endpoint}")
        
        # Log connection to database
        if db:
            client_headers = dict(websocket.headers)
            client_ip = websocket.client.host if websocket.client else None
            user_agent = client_headers.get("user-agent")
            db.log_connection(
                subdomain=subdomain,
                hostname=hostname,
                local_endpoint=local_endpoint,
                client_ip=client_ip,
                user_agent=user_agent,
                connection_id=connection_id,
                user_id=user_id,
                username=username,
                api_key_id=api_key_id
            )
        
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
            # Log disconnection to database
            if db:
                db.log_disconnection(subdomain, connection_id)


@app.get("/_health")
async def health_check():
    return {"status": "healthy", "active_connections": len(manager.active_connections)}


@app.get("/_admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, current_user = Depends(require_admin_user)):
    """Admin dashboard - protected by OAuth authentication"""
    # If require_admin_user returns a RedirectResponse, return it
    if isinstance(current_user, RedirectResponse):
        return current_user
    
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
            code {{
                font-family: monospace;
                font-size: 0.9em;
                background: #f0f0f0;
                padding: 2px 4px;
                border-radius: 3px;
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
                    User: {current_user['username']} ({current_user['provider']})
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
    
    # Add recent connections section
    if db:
        recent_connections = db.get_recent_connections(limit=10)
        if recent_connections:
            html += """
            
            <h2 style="margin-top: 40px;">Recent Connections</h2>
            <table>
                <thead>
                    <tr>
                        <th>Timestamp</th>
                        <th>Tunnel ID</th>
                        <th>Hostname</th>
                        <th>User</th>
                        <th>Local Endpoint</th>
                        <th>Client IP</th>
                    </tr>
                </thead>
                <tbody>
            """
            for conn in recent_connections:
                timestamp = conn['timestamp']
                subdomain = conn['subdomain']
                hostname = conn['hostname']
                username = conn.get('username') or 'anonymous'
                endpoint = conn['local_endpoint']
                client_ip = conn['client_ip'] or 'N/A'
                
                html += f"""
                    <tr>
                        <td>{timestamp}</td>
                        <td><code>{subdomain}</code></td>
                        <td><span class="endpoint">{hostname}</span></td>
                        <td>{username}</td>
                        <td><span class="endpoint">{endpoint}</span></td>
                        <td>{client_ip}</td>
                    </tr>
                """
            html += """
                </tbody>
            </table>
            """
    
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
async def admin_api_tunnels(request: Request, current_user = Depends(require_admin_user)):
    """Admin API endpoint - returns JSON data"""
    # If require_admin_user returns a RedirectResponse, convert to 401
    if isinstance(current_user, RedirectResponse):
        raise HTTPException(status_code=401, detail="Authentication required")
    
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
            "domain": manager.domain
        }
    }


@app.get("/_admin/api/audit")
async def admin_api_audit(request: Request, current_user = Depends(require_admin_user), limit: int = 100):
    """Admin API endpoint - returns audit log data"""
    # If require_admin_user returns a RedirectResponse, convert to 401
    if isinstance(current_user, RedirectResponse):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    if not db:
        return {"error": "Database not initialized"}
    
    recent_connections = db.get_recent_connections(limit=limit)
    
    return {
        "connections": recent_connections,
        "total": len(recent_connections)
    }


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy_request(request: Request, path: str):
    # Skip internal routes
    if path.startswith("_admin") or path.startswith("_health") or path.startswith("auth/") or path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found")
    
    host_header = request.headers.get("host", "")
    
    if not host_header:
        raise HTTPException(status_code=400, detail="Host header required")
    
    # Strip port from host header if present (e.g., "example.com:8000" -> "example.com")
    hostname = host_header.split(':')[0]
    
    subdomain = manager.get_subdomain_from_hostname(hostname)
    if not subdomain:
        raise HTTPException(status_code=404, detail=f"Hostname {hostname} not found")
    
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


def run_server(host: str = "0.0.0.0", port: int = 8000, domain: str = "tunnel.terrateam.dev", vcs_only: bool = False, validate_endpoint: bool = False, db_path: Optional[str] = None):
    global manager, db, auth_middleware
    manager = ConnectionManager(domain)
    
    # Validate configuration
    Config.validate()
    
    # Initialize database if path provided
    if db_path:
        db = Database(db_path)
    else:
        db = Database()  # Uses default terratunnel.db
    
    # Initialize auth middleware
    auth_middleware = AuthMiddleware(db)
    
    # Include routers BEFORE starting the server
    # This ensures admin routes are registered before the catch-all proxy route
    if Config.has_github_oauth():
        app.include_router(auth_router)
    # Always include API routes
    app.include_router(api_router)
    
    # Log OAuth configuration status
    if Config.has_github_oauth():
        logger.info(f"     GitHub OAuth enabled (redirect URI: {Config.get_github_oauth_redirect_uri(domain)})") 
    else:
        logger.info("     GitHub OAuth not configured (set GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET to enable)")
    
    # Log authentication requirement status
    if Config.REQUIRE_AUTH_FOR_TUNNELS:
        logger.info("     Authentication required for tunnel creation (default)")
    else:
        logger.info("     Authentication not required for tunnel creation (WARNING: Not recommended for production)")
    
    logger.info(f"     Starting tunnel server on {host}:{port} with domain {domain}")
    
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
