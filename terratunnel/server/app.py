import asyncio
import logging
import threading
import secrets
import string
import json
import uuid
import time
import os
import base64
from typing import Dict, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Depends, Cookie
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse, Response, HTMLResponse, RedirectResponse
import uvicorn
from coolname import generate_slug
from .database import Database
from .config import Config
from .auth import auth_router, require_admin_user, set_database as set_auth_database
from .api import api_router, set_database as set_api_database
from .middleware import AuthMiddleware
from ..utils import is_binary_content


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
        """Generate a human-readable subdomain for anonymous users"""
        max_attempts = 50
        
        for _ in range(max_attempts):
            # Generate readable subdomain with 3 words
            subdomain = generate_slug(3)
            
            # Check if it's already in use
            with self.lock:
                if subdomain not in self.active_connections:
                    return subdomain
        
        # Fallback to random string if we can't find a unique readable name
        # This is very unlikely with 3-word combinations
        chars = string.ascii_lowercase + string.digits
        return ''.join(secrets.choice(chars) for _ in range(12))


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
            # Normalize hostname to lowercase for case-insensitive lookup
            return self.hostname_to_subdomain.get(hostname.lower())
    
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

@app.middleware("http")
async def subdomain_proxy_middleware(request: Request, call_next):
    """Middleware to handle subdomain proxy requests before regular routes"""
    # Skip if manager not initialized
    if not manager:
        return await call_next(request)
    
    host_header = request.headers.get("host", "")
    if not host_header:
        return await call_next(request)
    
    hostname = host_header.split(':')[0].lower()
    
    # Check if this is a subdomain request
    if hostname != manager.domain and hostname.endswith(f".{manager.domain}"):
        # This is a subdomain request - handle it as a proxy request
        subdomain = manager.get_subdomain_from_hostname(hostname)
        if subdomain:
            # Get the path
            path = request.url.path
            if path.startswith("/"):
                path = path[1:]  # Remove leading slash for consistency
            
            # Call the proxy handler directly
            try:
                return await proxy_request(request, path)
            except HTTPException as e:
                # Convert HTTPException to Response in middleware context
                return JSONResponse(
                    status_code=e.status_code,
                    content={"detail": e.detail}
                )
        else:
            # Subdomain doesn't exist - return error page
            error_html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Tunnel Not Found</title>
                <style>
                    @font-face {{
                        font-family: 'Apertura';
                        src: url('/fonts/Apertura_Regular.otf') format('opentype');
                        font-weight: 400;
                        font-style: normal;
                    }}
                    body {{
                        font-family: 'Apertura', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                        margin: 0;
                        padding: 0;
                        background: #f5f5f5;
                        display: flex;
                        justify-content: center;
                        align-items: center;
                        min-height: 100vh;
                    }}
                    .container {{
                        max-width: 500px;
                        background: white;
                        padding: 40px;
                        border-radius: 8px;
                        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                        text-align: center;
                    }}
                    h1 {{
                        margin-top: 0;
                        color: #333;
                        font-size: 48px;
                    }}
                    p {{
                        color: #666;
                        line-height: 1.6;
                        margin: 20px 0;
                        font-size: 18px;
                    }}
                    .hostname {{
                        font-family: monospace;
                        background: #f6f8fa;
                        padding: 4px 8px;
                        border-radius: 4px;
                        color: #0066cc;
                    }}
                </style>
            </head>
            <body>
                <div class="container">
                    <h1>404</h1>
                    <p>Tunnel <span class="hostname">{hostname}</span> doesn't exist.</p>
                    <p>This tunnel may have been disconnected or never existed.</p>
                </div>
            </body>
            </html>
            """
            return HTMLResponse(content=error_html, status_code=404)
    
    # Not a subdomain request, continue with normal routes
    return await call_next(request)

# Include routers immediately to ensure proper route registration order
# The routers will check for configuration internally
app.include_router(auth_router)
app.include_router(api_router)


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
            user_id, api_key_id, tunnel_subdomain = auth_result
            
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
                    
                # Use the user's static subdomain
                if tunnel_subdomain:
                    subdomain = tunnel_subdomain
                    # Strip port from domain for hostname lookup
                    domain_without_port = manager.domain.split(':')[0]
                    hostname = f"{subdomain}.{domain_without_port}"
                else:
                    # This shouldn't happen if the database migration worked correctly
                    logger.error(f"User {user_id} has no tunnel_subdomain assigned")
                    await websocket.close(code=1008, reason="No tunnel subdomain assigned")
                    return
            else:
                # No authentication - generate random subdomain
                subdomain = manager.generate_subdomain()
                # Strip port from domain for hostname lookup
                domain_without_port = manager.domain.split(':')[0]
                hostname = f"{subdomain}.{domain_without_port}"
        else:
            # No auth middleware - generate random subdomain
            subdomain = manager.generate_subdomain()
            # Strip port from domain for hostname lookup
            domain_without_port = manager.domain.split(':')[0]
            hostname = f"{subdomain}.{domain_without_port}"
        
        with manager.lock:
            manager.active_connections[subdomain] = websocket
            manager.hostname_to_subdomain[hostname.lower()] = subdomain
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
        # Send full hostname with port to client for display
        hostname_with_port = f"{subdomain}.{manager.domain}"
        await websocket.send_text(json.dumps({
            "type": "hostname_assigned",
            "hostname": hostname_with_port,
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


@app.get("/", response_class=HTMLResponse)
async def home_page(request: Request, auth_token: Optional[str] = Cookie(None)):
    """Home page with login/dashboard"""
    from .auth import get_current_user_from_cookie
    
    user = None
    if auth_token:
        try:
            user = await get_current_user_from_cookie(request, auth_token)
        except:
            pass
    
    if user:
        # User is logged in, show dashboard
        # Get user's API keys and tunnel subdomain
        api_keys = []
        user_details = None
        if db:
            api_keys = db.list_user_api_keys(user["id"])
            user_details = db.get_user_by_id(user["id"])
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Terratunnel Dashboard</title>
            <style>
                @font-face {{
                    font-family: 'Apertura';
                    src: url('/fonts/Apertura_Regular.otf') format('opentype');
                    font-weight: 400;
                    font-style: normal;
                }}
                @font-face {{
                    font-family: 'Apertura';
                    src: url('/fonts/Apertura_Medium.otf') format('opentype');
                    font-weight: 500;
                    font-style: normal;
                }}
                @font-face {{
                    font-family: 'Apertura';
                    src: url('/fonts/Apertura_Bold.otf') format('opentype');
                    font-weight: 700;
                    font-style: normal;
                }}
                body {{
                    font-family: 'Apertura', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    margin: 0;
                    padding: 0;
                    background: #f5f5f5;
                }}
                .header {{
                    background: white;
                    border-bottom: 1px solid #e1e4e8;
                    padding: 20px 0;
                }}
                .header-content {{
                    max-width: 1200px;
                    margin: 0 auto;
                    padding: 0 20px;
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                }}
                .logo {{
                    font-size: 24px;
                    font-weight: bold;
                    color: #333;
                }}
                .user-info {{
                    display: flex;
                    align-items: center;
                    gap: 20px;
                }}
                .avatar {{
                    width: 32px;
                    height: 32px;
                    border-radius: 50%;
                }}
                .container {{
                    max-width: 1200px;
                    margin: 40px auto;
                    padding: 0 20px;
                }}
                .card {{
                    background: white;
                    border-radius: 8px;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                    padding: 30px;
                    margin-bottom: 20px;
                }}
                h1, h2 {{
                    margin-top: 0;
                    color: #333;
                }}
                .api-keys {{
                    margin-top: 30px;
                }}
                .api-key {{
                    background: #f8f9fa;
                    border: 1px solid #e1e4e8;
                    border-radius: 6px;
                    padding: 15px;
                    margin-bottom: 15px;
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                }}
                .api-key-info {{
                    flex: 1;
                }}
                .api-key-prefix {{
                    font-family: monospace;
                    font-weight: bold;
                    color: #0066cc;
                }}
                .api-key-name {{
                    color: #666;
                    margin-top: 5px;
                }}
                .api-key-created {{
                    color: #999;
                    font-size: 0.9em;
                    margin-top: 5px;
                }}
                .tunnel-url {{
                    background: #f6f8fa;
                    border: 1px solid #e1e4e8;
                    border-radius: 6px;
                    padding: 15px 20px;
                    margin: 15px 0;
                    font-family: monospace;
                    font-size: 16px;
                    color: #0066cc;
                    text-align: center;
                }}
                .button {{
                    display: inline-block;
                    padding: 10px 20px;
                    background: #0066cc;
                    color: white;
                    border: none;
                    border-radius: 6px;
                    text-decoration: none;
                    cursor: pointer;
                    font-size: 14px;
                }}
                .button:hover {{
                    background: #0052a3;
                }}
                .button-danger {{
                    background: #d73a49;
                }}
                .button-danger:hover {{
                    background: #cb2431;
                }}
                .empty {{
                    text-align: center;
                    padding: 40px;
                    color: #666;
                }}
                .code-block {{
                    background: #f6f8fa;
                    border: 1px solid #e1e4e8;
                    border-radius: 6px;
                    padding: 20px;
                    margin-top: 20px;
                    font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
                    font-size: 14px;
                    line-height: 1.5;
                    overflow-x: auto;
                    white-space: pre-wrap;
                }}
                .logout-link {{
                    color: #666;
                    text-decoration: none;
                }}
                .logout-link:hover {{
                    text-decoration: underline;
                }}
            </style>
        </head>
        <body>
            <div class="header">
                <div class="header-content">
                    <div class="logo"><img src="/logo-symbol.svg" alt="Terratunnel" style="height: 48px; vertical-align: middle; margin-right: 8px;">Terratunnel</div>
                    <div class="user-info">
                        <span>Signed in as <strong>{user['username']}</strong> ({user['provider']})</span>
                        <a href="/auth/logout?redirect_uri=/" class="logout-link">Sign out</a>
                    </div>
                </div>
            </div>
            
            <div class="container">
                <div class="card">
                    <h1>Your Tunnel</h1>
                    <p>Your permanent tunnel URL:</p>
                    <div class="tunnel-url">
                        <code>https://{user_details.get('tunnel_subdomain', 'unknown')}.{manager.domain}</code>
                    </div>
                </div>
                
                <div class="card">
                    <h1>API Key</h1>
                    <p>Use your API key to connect the tunnel client. You can have one active API key at a time.</p>
                    
                    <div class="api-keys">
        """
        
        # Find the active API key (should only be one)
        active_key = None
        if api_keys:
            active_keys = [key for key in api_keys if key.get('is_active', 1)]
            if active_keys:
                active_key = active_keys[0]
        
        if active_key:
            created_date = active_key['created_at'].split('T')[0] if 'T' in active_key['created_at'] else active_key['created_at'].split()[0]
            html += f"""
                        <div class="api-key">
                            <div class="api-key-info">
                                <div class="api-key-prefix">{active_key['key_prefix']}...</div>
                                <div class="api-key-name">{active_key['name'] or 'API Key'}</div>
                                <div class="api-key-created">Created on {created_date}</div>
                            </div>
                            <a href="/api/keys/new" class="button">Rotate API Key</a>
                        </div>
            """
        else:
            html += """
                        <div class="empty">
                            <p>You don't have an API key yet.</p>
                            <a href="/api/keys/new" class="button">Generate API Key</a>
                        </div>
            """
        
        html += """
                    </div>
                </div>
            </div>
        </body>
        </html>
        """
    else:
        # User is not logged in, show login page
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Terratunnel - Secure HTTP Tunneling</title>
            <style>
                @font-face {{
                    font-family: 'Apertura';
                    src: url('/fonts/Apertura_Regular.otf') format('opentype');
                    font-weight: 400;
                    font-style: normal;
                }}
                @font-face {{
                    font-family: 'Apertura';
                    src: url('/fonts/Apertura_Medium.otf') format('opentype');
                    font-weight: 500;
                    font-style: normal;
                }}
                @font-face {{
                    font-family: 'Apertura';
                    src: url('/fonts/Apertura_Bold.otf') format('opentype');
                    font-weight: 700;
                    font-style: normal;
                }}
                body {{
                    font-family: 'Apertura', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    margin: 0;
                    padding: 0;
                    background: #f5f5f5;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    min-height: 100vh;
                }}
                .container {{
                    max-width: 500px;
                    background: white;
                    padding: 40px;
                    border-radius: 8px;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                    text-align: center;
                }}
                h1 {{
                    margin-top: 0;
                    color: #333;
                }}
                .logo {{
                    font-size: 48px;
                    margin-bottom: 20px;
                }}
                p {{
                    color: #666;
                    line-height: 1.6;
                    margin: 20px 0;
                }}
                .github-button {{
                    display: inline-flex;
                    align-items: center;
                    gap: 10px;
                    padding: 12px 24px;
                    background: #24292e;
                    color: white;
                    border: none;
                    border-radius: 6px;
                    text-decoration: none;
                    font-size: 16px;
                    margin-top: 20px;
                    cursor: pointer;
                }}
                .github-button:hover {{
                    background: #1a1e22;
                }}
                .features {{
                    text-align: left;
                    margin: 30px 0;
                    padding: 20px;
                    background: #f8f9fa;
                    border-radius: 6px;
                }}
                .features ul {{
                    margin: 10px 0;
                    padding-left: 20px;
                }}
                .features li {{
                    color: #666;
                    margin: 5px 0;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="logo"><img src="/logo-wordmark.svg" alt="Terratunnel" style="height: 64px; margin-bottom: 20px;"></div>
                <h1>Welcome to Terratunnel</h1>
                <p>Secure HTTP tunneling for webhook development and testing.</p>
                
                <div class="features">
                    <strong>Features:</strong>
                    <ul>
                        <li>Expose local services to the internet</li>
                        <li>Perfect for webhook development</li>
                        <li>Secure WebSocket connections</li>
                        <li>API key authentication</li>
                        <li>Real-time request forwarding</li>
                    </ul>
                </div>
                
                <p>Sign in to get started with your API key.</p>
                
                <a href="/auth/github?redirect_uri=/" class="github-button">
                    <svg height="20" width="20" viewBox="0 0 16 16" fill="white">
                        <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/>
                    </svg>
                    Sign in with GitHub
                </a>
            </div>
        </body>
        </html>
        """
    
    return HTMLResponse(content=html)


@app.get("/api/keys/new", response_class=HTMLResponse)
async def new_api_key_page(request: Request, auth_token: Optional[str] = Cookie(None)):
    """Page to generate a new API key"""
    
    from .auth import get_current_user_from_cookie
    
    user = await get_current_user_from_cookie(request, auth_token)
    if not user:
        return RedirectResponse(url="/auth/github?redirect_uri=/api/keys/new", status_code=302)
    
    # Check if user already has an API key
    has_api_key = False
    if db:
        api_keys = db.list_user_api_keys(user["id"])
        active_keys = [key for key in api_keys if key.get('is_active', 1)]
        has_api_key = len(active_keys) > 0
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Generate API Key - Terratunnel</title>
        <style>
            @font-face {{
                font-family: 'Apertura';
                src: url('/fonts/Apertura_Regular.otf') format('opentype');
                font-weight: 400;
                font-style: normal;
            }}
            @font-face {{
                font-family: 'Apertura';
                src: url('/fonts/Apertura_Medium.otf') format('opentype');
                font-weight: 500;
                font-style: normal;
            }}
            @font-face {{
                font-family: 'Apertura';
                src: url('/fonts/Apertura_Bold.otf') format('opentype');
                font-weight: 700;
                font-style: normal;
            }}
            body {{
                font-family: 'Apertura', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                margin: 0;
                padding: 0;
                background: #f5f5f5;
            }}
            .header {{
                background: white;
                border-bottom: 1px solid #e1e4e8;
                padding: 20px 0;
            }}
            .header-content {{
                max-width: 800px;
                margin: 0 auto;
                padding: 0 20px;
            }}
            .logo {{
                font-size: 24px;
                font-weight: bold;
                color: #333;
            }}
            .container {{
                max-width: 800px;
                margin: 40px auto;
                padding: 0 20px;
            }}
            .card {{
                background: white;
                border-radius: 8px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                padding: 30px;
            }}
            h1 {{
                margin-top: 0;
                color: #333;
            }}
            .form-group {{
                margin-bottom: 20px;
            }}
            label {{
                display: block;
                margin-bottom: 5px;
                color: #333;
                font-weight: 500;
            }}
            input[type="text"] {{
                width: 100%;
                padding: 10px;
                border: 1px solid #e1e4e8;
                border-radius: 6px;
                font-size: 14px;
                box-sizing: border-box;
            }}
            input[type="text"]:focus {{
                outline: none;
                border-color: #0066cc;
            }}
            .help-text {{
                font-size: 0.9em;
                color: #666;
                margin-top: 5px;
            }}
            .button {{
                display: inline-block;
                padding: 10px 20px;
                background: #0066cc;
                color: white;
                border: none;
                border-radius: 6px;
                text-decoration: none;
                cursor: pointer;
                font-size: 14px;
            }}
            .button:hover {{
                background: #0052a3;
            }}
            .button-secondary {{
                background: #6c757d;
            }}
            .button-secondary:hover {{
                background: #5a6268;
            }}
            .buttons {{
                display: flex;
                gap: 10px;
                margin-top: 20px;
            }}
            .warning {{
                background: #fff3cd;
                border: 1px solid #ffeaa7;
                color: #856404;
                padding: 12px 16px;
                border-radius: 6px;
                margin: 20px 0;
                font-size: 14px;
            }}
        </style>
    </head>
    <body>
        <div class="header">
            <div class="header-content">
                <div class="logo">🚇 Terratunnel</div>
            </div>
        </div>
        
        <div class="container">
            <div class="card">
                <h1>{("Generate API Key" if not has_api_key else "Rotate API Key")}</h1>
                <form method="POST" action="/api/keys/generate">
                    <div class="form-group">
                        <label for="name">API Key Name (optional)</label>
                        <input type="text" id="name" name="name" placeholder="e.g., Production Server, CI/CD Pipeline">
                        <div class="help-text">Give your API key a name to help you remember what it's used for.</div>
                    </div>
                    
                    {('<div class="warning">⚠️ Generating a new API key will immediately revoke your existing key.</div>' if has_api_key else '')}
                    
                    <div class="buttons">
                        <button type="submit" class="button">{("Generate API Key" if not has_api_key else "Rotate API Key")}</button>
                        <a href="/" class="button button-secondary">Cancel</a>
                    </div>
                </form>
            </div>
        </div>
    </body>
    </html>
    """
    
    return HTMLResponse(content=html)


@app.post("/api/keys/generate")
async def generate_api_key(request: Request, auth_token: Optional[str] = Cookie(None)):
    """Generate a new API key for the user"""
    
    from .auth import get_current_user_from_cookie
    
    user = await get_current_user_from_cookie(request, auth_token)
    if not user:
        return RedirectResponse(url="/auth/github?redirect_uri=/api/keys/new", status_code=302)
    
    # Get form data
    form_data = await request.form()
    api_key_name = form_data.get("name", "").strip() or None
    
    # Generate API key
    if db:
        api_key = db.create_api_key(user["id"], api_key_name)
        key_prefix = api_key[:8]  # Extract prefix for display
        
        # Show the API key (only time it will be shown in full)
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>API Key Generated - Terratunnel</title>
            <style>
                @font-face {{
                    font-family: 'Apertura';
                    src: url('/fonts/Apertura_Regular.otf') format('opentype');
                    font-weight: 400;
                    font-style: normal;
                }}
                @font-face {{
                    font-family: 'Apertura';
                    src: url('/fonts/Apertura_Medium.otf') format('opentype');
                    font-weight: 500;
                    font-style: normal;
                }}
                @font-face {{
                    font-family: 'Apertura';
                    src: url('/fonts/Apertura_Bold.otf') format('opentype');
                    font-weight: 700;
                    font-style: normal;
                }}
                body {{
                    font-family: 'Apertura', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    margin: 0;
                    padding: 0;
                    background: #f5f5f5;
                }}
                .header {{
                    background: white;
                    border-bottom: 1px solid #e1e4e8;
                    padding: 20px 0;
                }}
                .header-content {{
                    max-width: 800px;
                    margin: 0 auto;
                    padding: 0 20px;
                }}
                .logo {{
                    font-size: 24px;
                    font-weight: bold;
                    color: #333;
                }}
                .container {{
                    max-width: 800px;
                    margin: 40px auto;
                    padding: 0 20px;
                }}
                .card {{
                    background: white;
                    border-radius: 8px;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                    padding: 30px;
                }}
                h1 {{
                    margin-top: 0;
                    color: #333;
                }}
                .success-icon {{
                    color: #28a745;
                    font-size: 48px;
                    margin-bottom: 20px;
                }}
                .api-key-display {{
                    background: #f6f8fa;
                    border: 1px solid #e1e4e8;
                    border-radius: 6px;
                    padding: 20px;
                    margin: 20px 0;
                    font-family: monospace;
                    font-size: 16px;
                    word-break: break-all;
                    user-select: all;
                }}
                .warning {{
                    background: #fff5b1;
                    border: 1px solid #f0e68c;
                    border-radius: 6px;
                    padding: 15px;
                    margin: 20px 0;
                    color: #856404;
                }}
                .button {{
                    display: inline-block;
                    padding: 10px 20px;
                    background: #0066cc;
                    color: white;
                    border: none;
                    border-radius: 6px;
                    text-decoration: none;
                    cursor: pointer;
                    font-size: 14px;
                    margin-top: 20px;
                }}
                .button:hover {{
                    background: #0052a3;
                }}
                .copy-button {{
                    background: #6c757d;
                    margin-left: 10px;
                }}
                .copy-button:hover {{
                    background: #5a6268;
                }}
            </style>
            <script>
                function copyApiKey() {{
                    const apiKey = document.getElementById('apiKey');
                    apiKey.select();
                    document.execCommand('copy');
                    
                    const button = document.getElementById('copyButton');
                    button.textContent = 'Copied!';
                    setTimeout(() => {{
                        button.textContent = 'Copy';
                    }}, 2000);
                }}
            </script>
        </head>
        <body>
            <div class="header">
                <div class="header-content">
                    <div class="logo"><img src="/logo-symbol.svg" alt="Terratunnel" style="height: 48px; vertical-align: middle; margin-right: 8px;">Terratunnel</div>
                </div>
            </div>
            
            <div class="container">
                <div class="card">
                    <div class="success-icon">✅</div>
                    <h1>API Key Generated Successfully!</h1>
                    
                    <p>Your new API key has been created{' with name "' + api_key_name + '"' if api_key_name else ''}.</p>
                    
                    <div class="api-key-display">
                        <input type="text" id="apiKey" value="{api_key}" readonly style="width: 100%; border: none; background: none; font-family: monospace; font-size: 16px;">
                    </div>
                    
                    <button class="button copy-button" id="copyButton" onclick="copyApiKey()">Copy</button>
                    
                    <div class="warning">
                        <strong>⚠️ Important:</strong> This is the only time you'll see this API key. Make sure to copy it and store it securely. You won't be able to see it again.
                    </div>
                    
                    <a href="/" class="button">Back to Dashboard</a>
                </div>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=html)
    else:
        raise HTTPException(status_code=500, detail="Database not initialized")


@app.post("/api/keys/{key_id}/revoke")
async def revoke_api_key(key_id: int, request: Request, auth_token: Optional[str] = Cookie(None)):
    """Revoke an API key"""
    
    from .auth import get_current_user_from_cookie
    
    user = await get_current_user_from_cookie(request, auth_token)
    if not user:
        return RedirectResponse(url="/auth/github?redirect_uri=/", status_code=302)
    
    if db:
        # Verify the key belongs to the user and revoke it
        success = db.revoke_api_key(user["id"], key_id)
        if not success:
            raise HTTPException(status_code=404, detail="API key not found")
    
    return RedirectResponse(url="/?revoked=true", status_code=302)


@app.get("/logo-wordmark.svg")
async def serve_logo_wordmark():
    """Serve the logo-wordmark.svg file"""
    import os
    logo_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logo-wordmark.svg")
    if os.path.exists(logo_path):
        with open(logo_path, "r") as f:
            svg_content = f.read()
        return Response(content=svg_content, media_type="image/svg+xml")
    else:
        # Return a placeholder if logo doesn't exist
        placeholder = '''<svg width="200" height="48" viewBox="0 0 200 48" xmlns="http://www.w3.org/2000/svg">
            <rect x="0" y="12" width="200" height="24" fill="#0066cc" rx="4"/>
            <text x="100" y="30" text-anchor="middle" fill="white" font-size="18" font-weight="bold">TERRATUNNEL</text>
        </svg>'''
        return Response(content=placeholder, media_type="image/svg+xml")


@app.get("/logo-symbol.svg")
async def serve_logo_symbol():
    """Serve the logo-symbol.svg file"""
    import os
    logo_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logo-symbol.svg")
    if os.path.exists(logo_path):
        with open(logo_path, "r") as f:
            svg_content = f.read()
        return Response(content=svg_content, media_type="image/svg+xml")
    else:
        # Return a placeholder if logo doesn't exist
        placeholder = '''<svg width="48" height="48" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg">
            <circle cx="24" cy="24" r="20" fill="#0066cc"/>
            <text x="24" y="32" text-anchor="middle" fill="white" font-size="24" font-weight="bold">T</text>
        </svg>'''
        return Response(content=placeholder, media_type="image/svg+xml")


@app.get("/fonts/{font_name}")
async def serve_font(font_name: str):
    """Serve font files"""
    import os
    fonts_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "fonts")
    font_path = os.path.join(fonts_dir, font_name)
    
    # Security check - ensure the path is within fonts directory
    if not os.path.abspath(font_path).startswith(os.path.abspath(fonts_dir)):
        raise HTTPException(status_code=404, detail="Font not found")
    
    if os.path.exists(font_path) and font_name.endswith('.otf'):
        with open(font_path, "rb") as f:
            font_content = f.read()
        return Response(content=font_content, media_type="font/otf")
    else:
        raise HTTPException(status_code=404, detail="Font not found")


@app.get("/_health")
async def health_check():
    return {"status": "healthy", "active_connections": len(manager.active_connections)}


@app.get("/_admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, current_user = Depends(require_admin_user)):
    """Admin dashboard - protected by OAuth authentication"""
    # If require_admin_user returns a RedirectResponse or HTMLResponse, return it
    if isinstance(current_user, (RedirectResponse, HTMLResponse)):
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
            @font-face {{
                font-family: 'Apertura';
                src: url('/fonts/Apertura_Regular.otf') format('opentype');
                font-weight: 400;
                font-style: normal;
            }}
            @font-face {{
                font-family: 'Apertura';
                src: url('/fonts/Apertura_Medium.otf') format('opentype');
                font-weight: 500;
                font-style: normal;
            }}
            @font-face {{
                font-family: 'Apertura';
                src: url('/fonts/Apertura_Bold.otf') format('opentype');
                font-weight: 700;
                font-style: normal;
            }}
            body {{
                font-family: 'Apertura', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
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
                <h1><img src="/logo-symbol.svg" alt="Terratunnel" style="height: 64px; vertical-align: middle; margin-right: 8px;">Terratunnel Admin</h1>
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
    # If require_admin_user returns a RedirectResponse or HTMLResponse, convert to appropriate error
    if isinstance(current_user, RedirectResponse):
        raise HTTPException(status_code=401, detail="Authentication required")
    if isinstance(current_user, HTMLResponse):
        raise HTTPException(status_code=403, detail="Access denied. Admin privileges required.")
    
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
    # If require_admin_user returns a RedirectResponse or HTMLResponse, convert to appropriate error
    if isinstance(current_user, RedirectResponse):
        raise HTTPException(status_code=401, detail="Authentication required")
    if isinstance(current_user, HTMLResponse):
        raise HTTPException(status_code=403, detail="Access denied. Admin privileges required.")
    
    if not db:
        return {"error": "Database not initialized"}
    
    recent_connections = db.get_recent_connections(limit=limit)
    
    return {
        "connections": recent_connections,
        "total": len(recent_connections)
    }




@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy_request(request: Request, path: str):
    # Skip if manager not initialized (during startup)
    if not manager:
        raise HTTPException(status_code=503, detail="Service starting up")
    
    host_header = request.headers.get("host", "")
    logger.debug(f"Proxy request - Host: {host_header}, Path: {path}")
    
    if not host_header:
        raise HTTPException(status_code=400, detail="Host header required")
    
    # Strip port from host header if present (e.g., "example.com:8000" -> "example.com")
    # Normalize to lowercase for consistent lookup
    hostname = host_header.split(':')[0].lower()
    
    
    subdomain = manager.get_subdomain_from_hostname(hostname)
    if not subdomain:
        logger.debug(f"Subdomain lookup failed for hostname: {hostname}")
        logger.debug(f"Available hostnames: {list(manager.hostname_to_subdomain.keys())}")
        raise HTTPException(status_code=404, detail=f"Tunnel {hostname} not found")
    
    body = await request.body()
    
    # Check if request contains binary data
    content_type = request.headers.get("content-type", "").lower()
    is_binary_request = is_binary_content(content_type)
    
    request_data = {
        "method": request.method,
        "path": f"/{path}" if path else "/",
        "headers": dict(request.headers),
        "query_params": dict(request.query_params),
    }
    
    if body:
        if is_binary_request:
            # Encode binary data as base64
            request_data["body"] = base64.b64encode(body).decode('ascii')
            request_data["is_binary"] = True
        else:
            try:
                request_data["body"] = body.decode('utf-8')
                request_data["is_binary"] = False
            except UnicodeDecodeError:
                # Fallback to base64 if decode fails
                request_data["body"] = base64.b64encode(body).decode('ascii')
                request_data["is_binary"] = True
    else:
        request_data["body"] = ""
        request_data["is_binary"] = False
    
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
    
    # Handle binary response data
    response_body = response_data.get("body", "")
    if response_data.get("is_binary") and response_body:
        # Decode base64 back to binary
        content = base64.b64decode(response_body)
    else:
        content = response_body
    
    return Response(
        content=content,
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
    
    # Set database in auth and api modules
    set_auth_database(db)
    set_api_database(db)
    
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
