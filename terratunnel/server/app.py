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
from fastapi.responses import JSONResponse, Response, HTMLResponse, RedirectResponse, StreamingResponse
import uvicorn
from coolname import generate_slug
from .database import Database
from .config import Config
from .auth import auth_router, require_admin_user, set_database as set_auth_database
from .api import api_router, set_database as set_api_database
from .middleware import AuthMiddleware
from ..utils import is_binary_content
from ..streaming import StreamReceiver, StreamMetadata


logger = logging.getLogger("terratunnel-server")


class StreamingRequest:
    """Handles streaming response data."""
    def __init__(self, request_id: str):
        self.request_id = request_id
        self.metadata_future = asyncio.Future()
        self.chunk_queue = asyncio.Queue()
        self.is_complete = False
        self.error = None


class StreamHandler:
    """Handles a single streaming response."""
    def __init__(self, request_id: str):
        self.request_id = request_id
        self.metadata_event = asyncio.Event()
        self.metadata = None
        self.chunk_queue = asyncio.Queue()
        self.is_complete = False
        self.error = None
        self.stream_id = None
        
    async def wait_for_metadata(self, timeout: float = 30.0):
        """Wait for response metadata."""
        try:
            await asyncio.wait_for(self.metadata_event.wait(), timeout=timeout)
            return self.metadata
        except asyncio.TimeoutError:
            return None
    
    def set_metadata(self, metadata: dict, stream_id: str = None):
        """Set response metadata."""
        self.metadata = metadata
        self.stream_id = stream_id
        self.metadata_event.set()
    
    async def add_chunk(self, data: bytes):
        """Add a chunk to the stream."""
        await self.chunk_queue.put(data)
    
    async def complete(self):
        """Mark stream as complete."""
        self.is_complete = True
        await self.chunk_queue.put(None)
    
    async def error(self, error_msg: str):
        """Mark stream as errored."""
        self.error = error_msg
        self.is_complete = True
        await self.chunk_queue.put(None)
    
    async def stream_chunks(self):
        """Yield chunks as they arrive."""
        while True:
            try:
                chunk = await asyncio.wait_for(self.chunk_queue.get(), timeout=60.0)
                if chunk is None:
                    break
                yield chunk
            except asyncio.TimeoutError:
                logger.error(f"Timeout waiting for chunk in stream {self.request_id}")
                break
            except Exception as e:
                logger.error(f"Error streaming chunk: {e}")
                break


class ConnectionManager:
    def __init__(self, domain: str = "tunnel.terrateam.dev"):
        self.active_connections: Dict[str, WebSocket] = {}
        self.hostname_to_subdomain: Dict[str, str] = {}
        self.subdomain_to_endpoint: Dict[str, str] = {}
        self.pending_requests: Dict[str, asyncio.Future] = {}
        self.streaming_requests: Dict[str, StreamingRequest] = {}  # For streaming responses
        self.domain = domain
        self.lock = threading.Lock()
        self.stream_receivers: Dict[str, StreamReceiver] = {}  # Track active stream receivers
        self.stream_handlers: Dict[str, StreamHandler] = {}  # Stream handlers for new architecture

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
        # Generate unique request ID
        request_id = str(uuid.uuid4())
        request_data["request_id"] = request_id
        
        with self.lock:
            websocket = self.active_connections.get(subdomain)
        
        if not websocket:
            logger.error(f"[{request_id}] No active WebSocket connection for subdomain {subdomain}")
            return None
        
        method = request_data.get("method", "GET")
        path = request_data.get("path", "/")
        logger.debug(f"[{request_id}] Sending {method} {path} to {subdomain}")
        
        # Create future for response
        future = asyncio.Future()
        
        with self.lock:
            self.pending_requests[request_id] = future
            pending_count = len(self.pending_requests)
        
        logger.debug(f"[{request_id}] Added to pending requests (total pending: {pending_count})")
        
        try:
            await websocket.send_text(json.dumps(request_data))
            logger.debug(f"[{request_id}] Request sent via WebSocket, waiting for response...")
            # Wait for response with timeout
            response = await asyncio.wait_for(future, timeout=600.0)  # 10 minutes for large files
            logger.debug(f"[{request_id}] Response received")
            return response
        except asyncio.TimeoutError:
            logger.error(f"[{request_id}] Timeout waiting for response from {subdomain} (waited 10 minutes)")
            with self.lock:
                self.pending_requests.pop(request_id, None)
            self.disconnect(subdomain)
            return None
        except Exception as e:
            logger.error(f"[{request_id}] Error sending request to {subdomain}: {e}")
            with self.lock:
                self.pending_requests.pop(request_id, None)
            self.disconnect(subdomain)
            return None

    async def create_streaming_response(self, subdomain: str, request_data: dict):
        """Create a streaming response using the new architecture."""
        request_id = str(uuid.uuid4())
        request_data["request_id"] = request_id
        
        # Create stream handler
        handler = StreamHandler(request_id)
        with self.lock:
            self.stream_handlers[request_id] = handler
        
        try:
            # Send request
            with self.lock:
                websocket = self.active_connections.get(subdomain)
            
            if not websocket:
                raise Exception("No active WebSocket connection")
            
            await websocket.send_text(json.dumps(request_data))
            
            # Wait for metadata
            metadata = await handler.wait_for_metadata()
            if not metadata:
                raise Exception("Timeout waiting for response metadata")
            
            # Return metadata and handler
            return metadata, handler
            
        except Exception as e:
            # Cleanup on error
            with self.lock:
                self.stream_handlers.pop(request_id, None)
            raise

    async def handle_stream_chunk(self, subdomain: str, message: dict):
        """Handle incoming stream chunk."""
        stream_id = message.get("stream_id")
        
        logger.debug(f"[SERVER-STREAM] handle_stream_chunk called for stream {stream_id} from subdomain {subdomain}")
        logger.debug(f"[SERVER-STREAM] Message keys: {list(message.keys())}")
        logger.debug(f"[SERVER-STREAM] Chunk data keys: {list(message.get('chunk', {}).keys())}")
        
        # Find the stream receiver
        receiver = None
        receiver_key = None
        with self.lock:
            logger.debug(f"[SERVER-STREAM] Looking for receiver for stream {stream_id} from subdomain {subdomain}")
            logger.debug(f"[SERVER-STREAM] Active receivers: {list(self.stream_receivers.keys())}")
            for recv_id, recv in self.stream_receivers.items():
                logger.debug(f"[SERVER-STREAM] Checking receiver {recv_id}, active streams: {list(recv.active_streams.keys())}")
                if recv_id.startswith(f"{subdomain}:") and stream_id in recv.active_streams:
                    receiver = recv
                    receiver_key = recv_id
                    logger.debug(f"[SERVER-STREAM] Found matching receiver!")
                    break
        
        if receiver:
            logger.debug(f"[SERVER-STREAM] Found receiver {receiver_key} for stream {stream_id}")
            
            # Let receiver handle the chunk
            receiver.handle_chunk(message)
            
            # Check if we have a stream handler
            stream_handler = getattr(receiver, 'stream_handler', None)
            if stream_handler:
                # Forward decoded chunk to handler
                chunk_data = message.get("chunk", {})
                if chunk_data.get("data"):
                    try:
                        decoded_data = base64.b64decode(chunk_data["data"])
                        await stream_handler.add_chunk(decoded_data)
                        logger.debug(f"[SERVER-STREAM] Added chunk to stream handler")
                    except Exception as e:
                        logger.error(f"Error decoding chunk: {e}")
                        await stream_handler.error(str(e))
        else:
            logger.warning(f"[SERVER-STREAM] No receiver found for stream {stream_id} from subdomain {subdomain}")
    
    async def handle_stream_complete(self, subdomain: str, message: dict):
        """Handle stream completion."""
        stream_id = message.get("stream_id")
        
        logger.debug(f"[SERVER-STREAM] handle_stream_complete called for stream {stream_id} from subdomain {subdomain}")
        logger.debug(f"[SERVER-STREAM] Completion message: {message}")
        
        # Find the stream receiver
        receiver = None
        with self.lock:
            logger.debug(f"[SERVER-STREAM] Looking for receiver for completed stream {stream_id}")
            logger.debug(f"[SERVER-STREAM] Available receivers: {list(self.stream_receivers.keys())}")
            for recv_id, recv in self.stream_receivers.items():
                logger.debug(f"[SERVER-STREAM] Checking receiver {recv_id}, active streams: {list(recv.active_streams.keys())}")
                if recv_id.startswith(f"{subdomain}:") and stream_id in recv.active_streams:
                    receiver = recv
                    logger.debug(f"[SERVER-STREAM] Found receiver {recv_id} for completed stream")
                    break
        
        if receiver:
            # Check if we have a stream handler
            stream_handler = getattr(receiver, 'stream_handler', None)
            if stream_handler:
                # New streaming architecture - just mark complete
                logger.debug(f"[SERVER-STREAM] Using new streaming architecture for completion")
                receiver.handle_complete(message)
                await stream_handler.complete()
                
                # Cleanup
                with self.lock:
                    # Find and remove receiver
                    for recv_id, recv in list(self.stream_receivers.items()):
                        if recv == receiver:
                            del self.stream_receivers[recv_id]
                            break
                    # Remove stream handler
                    self.stream_handlers.pop(stream_handler.request_id, None)
                logger.info(f"Stream {stream_id} completed successfully")
            elif receiver.handle_complete(message):
                logger.debug(f"[SERVER-STREAM] handle_complete returned True, getting assembled data")
                # Get the complete data
                data = receiver.get_assembled_data(stream_id)
                logger.debug(f"[SERVER-STREAM] get_assembled_data returned: {type(data)}, size={len(data) if data else 'None'}")
                stream = receiver.active_streams.get(stream_id)
                
                if data is not None and stream and "request_future" in stream:
                    future = stream["request_future"]
                    if not future.done():
                        try:
                            # Check if stream took too long (safety check)
                            start_time = stream.get("start_time", 0)
                            current_time = asyncio.get_event_loop().time()
                            duration = current_time - start_time
                            
                            # Validate assembled data
                            if len(data) == 0:
                                logger.error(f"Stream {stream_id} assembled data is empty")
                                if not future.done():
                                    future.set_exception(Exception("Stream assembly failed: empty data"))
                                receiver.cleanup_stream(stream_id)
                                return
                            
                            # Check if data size matches expected size
                            metadata = stream.get("metadata")
                            if metadata and hasattr(metadata, "total_size"):
                                expected_size = metadata.total_size
                            else:
                                expected_size = 0
                            if expected_size > 0 and len(data) != expected_size:
                                logger.warning(f"Stream {stream_id} size mismatch: expected {expected_size}, got {len(data)}")
                                # Don't fail on size mismatch, just log it
                            
                            # Create response with assembled data
                            response_data = {
                                "status_code": stream.get("status_code", 200),
                                "headers": stream.get("headers", {}),
                                "body": base64.b64encode(data).decode('ascii'),
                                "is_binary": True
                            }
                            future.set_result(response_data)
                            logger.info(f"Stream {stream_id} completed, response delivered ({len(data)} bytes, took {duration:.2f}s)")
                            
                            # Schedule cleanup after a short delay to allow response processing
                            def delayed_cleanup():
                                try:
                                    receiver.cleanup_stream(stream_id)
                                    logger.debug(f"Stream {stream_id} cleaned up after response delivery")
                                except Exception as cleanup_error:
                                    logger.error(f"Error during delayed cleanup of stream {stream_id}: {cleanup_error}")
                            
                            # Schedule cleanup with a small delay
                            asyncio.get_event_loop().call_later(0.1, delayed_cleanup)
                            
                        except Exception as e:
                            logger.error(f"Error creating response for stream {stream_id}: {e}")
                            if not future.done():
                                future.set_exception(Exception(f"Stream assembly failed: {e}"))
                            # Clean up immediately on error
                            receiver.cleanup_stream(stream_id)
                    else:
                        logger.warning(f"Stream {stream_id} completed but future already done")
                        # Clean up if future is already done
                        receiver.cleanup_stream(stream_id)
                elif data is None:
                    logger.error(f"Stream {stream_id} completed but assembled data is None")
                    if stream and "request_future" in stream:
                        future = stream["request_future"]
                        if not future.done():
                            future.set_exception(Exception("Stream assembly failed: missing data"))
                    # Clean up on error
                    receiver.cleanup_stream(stream_id)
                else:
                    logger.error(f"Stream {stream_id} completed but stream or future not found")
                    # Clean up on error
                    receiver.cleanup_stream(stream_id)
            else:
                logger.error(f"Stream {stream_id} completion failed validation")
                # Handle failed completion
                stream = receiver.active_streams.get(stream_id)
                if stream and "request_future" in stream:
                    future = stream["request_future"]
                    if not future.done():
                        future.set_exception(Exception("Stream completion failed validation"))
                receiver.cleanup_stream(stream_id)
        else:
            logger.warning(f"No receiver found for completed stream {stream_id} from subdomain {subdomain}")
    
    def handle_stream_error(self, subdomain: str, message: dict):
        """Handle stream error."""
        stream_id = message.get("stream_id")
        
        # Find the stream receiver
        receiver = None
        with self.lock:
            for recv_id, recv in self.stream_receivers.items():
                if recv_id.startswith(f"{subdomain}:") and stream_id in recv.active_streams:
                    receiver = recv
                    break
        
        if receiver:
            receiver.handle_error(message)
            # Find the pending request and error it
            stream = receiver.active_streams.get(stream_id)
            if stream and "request_future" in stream:
                future = stream["request_future"]
                if not future.done():
                    future.set_exception(Exception(message.get("error", "Stream error")))

    def handle_response(self, response_data: dict, subdomain: str = None):
        request_id = response_data.get("request_id")
        if not request_id:
            logger.warning("Received response without request_id")
            return
        
        logger.debug(f"[{request_id}] Received response from client")
        
        # Check if we have a stream handler waiting for this
        with self.lock:
            stream_handler = self.stream_handlers.get(request_id)
        
        if stream_handler:
            # New streaming architecture
            logger.debug(f"[{request_id}] Using new streaming architecture")
            
            if response_data.get("is_streaming"):
                # Set metadata on the handler
                stream_info = response_data.get("stream", {})
                stream_id = stream_info.get("id")
                stream_handler.set_metadata(response_data, stream_id)
                
                # Still need to set up receiver for chunk handling
                if stream_id:
                    receiver_key = f"{subdomain}:{request_id}"
                    with self.lock:
                        if receiver_key not in self.stream_receivers:
                            self.stream_receivers[receiver_key] = StreamReceiver()
                        receiver = self.stream_receivers[receiver_key]
                        # Link handler to receiver
                        self.stream_receivers[receiver_key].stream_handler = stream_handler
                    
                    # Initialize the stream
                    metadata = StreamMetadata(
                        stream_id=stream_id,
                        request_id=request_id,
                        total_size=stream_info.get("total_size", 0),
                        total_chunks=0,
                        content_type=stream_info.get("content_type", "")
                    )
                    receiver.start_stream(metadata, None)
                    receiver.active_streams[stream_id]["receiver_key"] = receiver_key
                    logger.info(f"[{request_id}] Started streaming response for stream {stream_id}")
            else:
                # Non-streaming response - set it directly
                stream_handler.set_metadata(response_data)
        else:
            # Old architecture - check for future
            with self.lock:
                future = self.pending_requests.pop(request_id, None)
                pending_count = len(self.pending_requests)
            
            logger.debug(f"[{request_id}] Removed from pending requests (remaining: {pending_count})")
            
            if future and not future.done():
                # Check if this is a streaming response
                if response_data.get("is_streaming"):
                    # Create a stream receiver for this stream
                    stream_info = response_data.get("stream", {})
                    stream_id = stream_info.get("id")
                    
                    if stream_id:
                        # Create receiver key with subdomain prefix
                        receiver_key = f"{subdomain}:{request_id}"
                        
                        with self.lock:
                            if receiver_key not in self.stream_receivers:
                                self.stream_receivers[receiver_key] = StreamReceiver()
                            
                            receiver = self.stream_receivers[receiver_key]
                        
                        # Initialize the stream
                        metadata = StreamMetadata(
                            stream_id=stream_id,
                            request_id=request_id,
                            total_size=stream_info.get("total_size", 0),
                            total_chunks=0,  # Will be updated by chunks
                            content_type=stream_info.get("content_type", "")
                        )
                        
                        logger.debug(f"[SERVER-STREAM-INIT] Creating stream metadata: stream_id={stream_id}, request_id={request_id}, total_size={metadata.total_size}")
                        
                        receiver.start_stream(metadata, None)
                        
                        # Store the future in the stream for later completion
                        receiver.active_streams[stream_id]["request_future"] = future
                        receiver.active_streams[stream_id]["status_code"] = response_data.get("status_code", 200)
                        receiver.active_streams[stream_id]["headers"] = response_data.get("headers", {})
                        receiver.active_streams[stream_id]["subdomain"] = subdomain
                        receiver.active_streams[stream_id]["start_time"] = asyncio.get_event_loop().time()
                        
                        logger.debug(f"[SERVER-STREAM-INIT] Stream {stream_id} initialized with future and metadata")
                        logger.info(f"[{request_id}] Started receiving stream {stream_id} with receiver key {receiver_key}")
                    else:
                        # No stream ID, complete with error
                        future.set_exception(Exception("Streaming response missing stream ID"))
                else:
                    # Regular response
                    future.set_result(response_data)
                    logger.debug(f"[{request_id}] Response delivered to waiting request")
            else:
                logger.warning(f"[{request_id}] No waiting future found or future already done")


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
                # Get user info for logging
                user = db.get_user_by_id(user_id)
                if user:
                    username = user['provider_username']
                    logger.info(f"     Authenticated tunnel creation for user: {username} (ID: {user_id})")
                    
                if tunnel_subdomain:
                    # Use the user's static subdomain
                    subdomain = tunnel_subdomain
                    # Strip port from domain for hostname lookup
                    domain_without_port = manager.domain.split(':')[0]
                    hostname = f"{subdomain}.{domain_without_port}"
                else:
                    # Generate a new subdomain
                    subdomain = manager.generate_subdomain()
                    domain_without_port = manager.domain.split(':')[0]
                    hostname = f"{subdomain}.{domain_without_port}"
                    logger.info(f"User {username} generated new subdomain: {subdomain}")
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
                
                message_type = message.get("type")
                logger.debug(f"[SERVER-WS] Received WebSocket message type: {message_type}")
                logger.debug(f"[SERVER-WS] Full message keys: {list(message.keys())}")
                
                if message_type == "keepalive_ack":
                    continue
                elif message_type == "stream_chunk":
                    logger.debug(f"[SERVER-WS] Handling stream_chunk message")
                    # Handle streaming chunk
                    await manager.handle_stream_chunk(subdomain, message)
                    continue
                elif message_type == "stream_complete":
                    # Handle stream completion
                    logger.debug(f"Received stream_complete message: {message}")
                    await manager.handle_stream_complete(subdomain, message)
                    continue
                elif message_type == "stream_error":
                    # Handle stream error
                    manager.handle_stream_error(subdomain, message)
                    continue
                elif message.get("request_id"):
                    # Handle response from client
                    manager.handle_response(message, subdomain)
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


def is_admin_user(user: dict) -> bool:
    """Check if a user is an admin"""
    if not user:
        return False
    return Config.is_admin_user(user.get("provider"), user.get("username"))


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
        is_admin = is_admin_user(user)
        
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
                    <h1>API Keys</h1>
                    <p>Use your API key to connect the tunnel client. """ + ("As an admin, you can have multiple active API keys." if is_admin else "You can have one active API key at a time.") + """</p>
                    
                    <div class="api-keys">
        """
        
        # Find active API keys
        if api_keys:
            active_keys = [key for key in api_keys if key.get('is_active', 1)]
            
            if is_admin and active_keys:
                # Show all active keys for admin users
                for key in active_keys:
                    created_date = key['created_at'].split('T')[0] if 'T' in key['created_at'] else key['created_at'].split()[0]
                    html += f"""
                        <div class="api-key">
                            <div class="api-key-info">
                                <div class="api-key-prefix">{key['key_prefix']}...</div>
                                <div class="api-key-name">{key['name'] or 'API Key'}</div>
                                <div class="api-key-created">Created on {created_date}</div>
                            </div>
                        </div>
                    """
                html += f"""
                    <div style="margin-top: 20px;">
                        <a href="/api/keys/new" class="button">Generate New API Key</a>
                    </div>
                """
            elif active_keys:
                # Show single key for regular users
                active_key = active_keys[0]
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
        """
        
        # Add multiple API keys section for admin users
        if is_admin:
            html += """
                <div class="card">
                    <h1>🚀 Multiple Tunnels (Admin Feature)</h1>
                    <p>As an administrator, you can create multiple API keys to run multiple tunnels simultaneously.</p>
                    
                    <h3>Create Additional Tunnels</h3>
                    <p>Unlike regular users, admin users can have multiple active API keys. Each API key can be used to create a separate tunnel:</p>
                    <div class="code-block"># Generate a new API key from the dashboard above
# Then use it to create a new tunnel:
python -m terratunnel client \\
    --api-key YOUR_NEW_API_KEY \\
    --local-endpoint http://localhost:3000

# Run another tunnel with a different API key:
python -m terratunnel client \\
    --api-key ANOTHER_API_KEY \\
    --local-endpoint http://localhost:4000</div>
                    
                    <p style="margin-top: 20px;"><strong>Note:</strong> Each API key will create a tunnel with its own unique subdomain.</p>
                </div>
        """
        
        # Add admin section if user is admin
        if is_admin:
            # Gather tunnel data for admin view
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
            
            # Get recent audit logs
            recent_logs = []
            if db:
                try:
                    recent_logs = db.get_audit_logs(limit=10)
                except:
                    pass
            
            html += f"""
                <div class="card">
                    <h1>🛡️ Admin Dashboard</h1>
                    <p>You have administrator privileges. Here's an overview of all active tunnels.</p>
                    
                    <h2>Active Tunnels ({len([t for t in tunnels if t['connected']])})</h2>
                    <div style="overflow-x: auto;">
                        <table style="width: 100%; border-collapse: collapse; margin-top: 20px;">
                            <thead>
                                <tr>
                                    <th style="text-align: left; padding: 12px; background: #f6f8fa; border-bottom: 1px solid #e1e4e8;">Hostname</th>
                                    <th style="text-align: left; padding: 12px; background: #f6f8fa; border-bottom: 1px solid #e1e4e8;">Endpoint</th>
                                    <th style="text-align: left; padding: 12px; background: #f6f8fa; border-bottom: 1px solid #e1e4e8;">Status</th>
                                </tr>
                            </thead>
                            <tbody>
            """
            
            for tunnel in tunnels:
                if tunnel['connected']:
                    status_badge = '<span style="color: #28a745;">● Connected</span>'
                    html += f"""
                                <tr>
                                    <td style="padding: 12px; border-bottom: 1px solid #e1e4e8;">
                                        <a href="{tunnel['url']}" target="_blank" style="color: #0066cc; text-decoration: none;">
                                            {tunnel['hostname']}
                                        </a>
                                    </td>
                                    <td style="padding: 12px; border-bottom: 1px solid #e1e4e8; font-family: monospace; font-size: 13px;">
                                        {tunnel['endpoint'] or '-'}
                                    </td>
                                    <td style="padding: 12px; border-bottom: 1px solid #e1e4e8;">
                                        {status_badge}
                                    </td>
                                </tr>
                    """
            
            if not any(t['connected'] for t in tunnels):
                html += """
                                <tr>
                                    <td colspan="3" style="padding: 20px; text-align: center; color: #666;">
                                        No active tunnels
                                    </td>
                                </tr>
                """
            
            html += """
                            </tbody>
                        </table>
                    </div>
                    
                    <h2 style="margin-top: 30px;">Recent Connections</h2>
                    <div style="overflow-x: auto;">
                        <table style="width: 100%; border-collapse: collapse; margin-top: 20px;">
                            <thead>
                                <tr>
                                    <th style="text-align: left; padding: 12px; background: #f6f8fa; border-bottom: 1px solid #e1e4e8;">User</th>
                                    <th style="text-align: left; padding: 12px; background: #f6f8fa; border-bottom: 1px solid #e1e4e8;">Subdomain</th>
                                    <th style="text-align: left; padding: 12px; background: #f6f8fa; border-bottom: 1px solid #e1e4e8;">Connected</th>
                                    <th style="text-align: left; padding: 12px; background: #f6f8fa; border-bottom: 1px solid #e1e4e8;">Duration</th>
                                </tr>
                            </thead>
                            <tbody>
            """
            
            for log in recent_logs[:10]:
                connected_at = log.get('connected_at', '')
                disconnected_at = log.get('disconnected_at', '')
                duration = '-'
                if connected_at and disconnected_at:
                    try:
                        from datetime import datetime
                        start = datetime.fromisoformat(connected_at.replace(' ', 'T'))
                        end = datetime.fromisoformat(disconnected_at.replace(' ', 'T'))
                        delta = end - start
                        duration = str(delta).split('.')[0]  # Remove microseconds
                    except:
                        pass
                
                html += f"""
                                <tr>
                                    <td style="padding: 12px; border-bottom: 1px solid #e1e4e8;">
                                        {log.get('username', 'Unknown')}
                                    </td>
                                    <td style="padding: 12px; border-bottom: 1px solid #e1e4e8; font-family: monospace; font-size: 13px;">
                                        {log.get('subdomain', '-')}
                                    </td>
                                    <td style="padding: 12px; border-bottom: 1px solid #e1e4e8; color: #666; font-size: 13px;">
                                        {connected_at}
                                    </td>
                                    <td style="padding: 12px; border-bottom: 1px solid #e1e4e8;">
                                        {duration}
                                    </td>
                                </tr>
                """
            
            if not recent_logs:
                html += """
                                <tr>
                                    <td colspan="4" style="padding: 20px; text-align: center; color: #666;">
                                        No recent connections
                                    </td>
                                </tr>
                """
            
            html += """
                            </tbody>
                        </table>
                    </div>
                </div>
            """
        
        html += """
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
        # Check if user is admin
        user_details = db.get_user_by_id(user["id"])
        is_admin = is_admin_user(user_details) if user_details else False
        
        api_key = db.create_api_key(user["id"], api_key_name, is_admin=is_admin)
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






async def proxy_request_streaming(request: Request, path: str):
    """Handle proxy requests with proper streaming support."""
    if not manager:
        raise HTTPException(status_code=503, detail="Service starting up")
    
    host_header = request.headers.get("host", "")
    if not host_header:
        raise HTTPException(status_code=400, detail="Host header required")
    
    hostname = host_header.split(':')[0].lower()
    subdomain = manager.get_subdomain_from_hostname(hostname)
    if not subdomain:
        raise HTTPException(status_code=404, detail=f"Tunnel {hostname} not found")
    
    # Prepare request data
    body = await request.body()
    MAX_REQUEST_SIZE = int(os.environ.get('TERRATUNNEL_MAX_REQUEST_SIZE', str(50 * 1024 * 1024)))
    if len(body) > MAX_REQUEST_SIZE:
        raise HTTPException(status_code=413, detail=f"Request body too large")
    
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
            request_data["body"] = base64.b64encode(body).decode('ascii')
            request_data["is_binary"] = True
        else:
            try:
                request_data["body"] = body.decode('utf-8')
                request_data["is_binary"] = False
            except UnicodeDecodeError:
                request_data["body"] = base64.b64encode(body).decode('ascii')
                request_data["is_binary"] = True
    else:
        request_data["body"] = ""
        request_data["is_binary"] = False
    
    # Try streaming approach first
    try:
        metadata, handler = await manager.create_streaming_response(subdomain, request_data)
        
        if metadata.get("is_streaming"):
            # Return streaming response
            status_code = metadata.get("status_code", 200)
            headers = metadata.get("headers", {})
            
            # Filter headers
            filtered_headers = {}
            skip_headers = {"content-length", "transfer-encoding", "connection"}
            for key, value in headers.items():
                if key.lower() not in skip_headers:
                    filtered_headers[key] = value
            
            return StreamingResponse(
                handler.stream_chunks(),
                status_code=status_code,
                headers=filtered_headers,
                media_type=headers.get("content-type", "application/octet-stream")
            )
        else:
            # Non-streaming response
            response_data = metadata
            
            # Filter headers
            response_headers = response_data.get("headers", {})
            filtered_headers = {}
            skip_headers = {"content-length", "transfer-encoding", "connection"}
            for key, value in response_headers.items():
                if key.lower() not in skip_headers:
                    filtered_headers[key] = value
            
            # Handle body
            response_body = response_data.get("body", "")
            status_code = response_data.get("status_code", 200)
            
            if response_data.get("is_binary") and response_body:
                content = base64.b64decode(response_body)
            else:
                content = response_body
            
            return Response(
                content=content,
                status_code=status_code,
                headers=filtered_headers
            )
    except Exception as e:
        logger.error(f"Error in streaming proxy: {e}")
        # Fall back to old method
        response_data = await manager.send_request(subdomain, request_data)
        
        if response_data is None:
            raise HTTPException(status_code=502, detail="Service unavailable")
        
        # [Rest of original proxy_request code for fallback]
        response_headers = response_data.get("headers", {})
        filtered_headers = {}
        skip_headers = {"content-length", "transfer-encoding", "connection"}
        for key, value in response_headers.items():
            if key.lower() not in skip_headers:
                filtered_headers[key] = value
        
        response_body = response_data.get("body", "")
        status_code = response_data.get("status_code", 200)
        
        if response_data.get("is_binary") and response_body:
            content = base64.b64decode(response_body)
        else:
            content = response_body
        
        return Response(
            content=content,
            status_code=status_code,
            headers=filtered_headers
        )


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy_request(request: Request, path: str):
    # Use the new streaming proxy
    return await proxy_request_streaming(request, path)


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
        ws_max_size=512 * 1024 * 1024,  # 512MB max WebSocket message size
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
