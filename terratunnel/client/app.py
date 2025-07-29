import asyncio
import logging
import json
import threading
import signal
import sys
import os
import time
import base64
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Optional
from urllib.parse import urlparse
import websockets
import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn
import jwt

from .request_logger import RequestLogger
from ..utils import is_binary_content
from ..streaming import (
    ChunkedStreamer, StreamMessage, StreamMetadata, 
    STREAMING_THRESHOLD, DEFAULT_CHUNK_SIZE
)

# Suppress uvicorn's shutdown errors
logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)


logger = logging.getLogger("terratunnel-client")


class TunnelClient:
    def __init__(self, server_url: str, local_endpoint: str, dashboard: bool = False, dashboard_port: int = 8080, api_port: int = 8081, update_github_webhook: bool = False, api_key: Optional[str] = None):
        self.server_url = server_url
        self.local_endpoint = local_endpoint
        self.api_key = api_key
        self.assigned_hostname = None
        self.subdomain = None
        self.websocket = None
        self.running = True
        self.lock = threading.Lock()
        self.websocket_send_lock = asyncio.Lock()  # Lock for WebSocket sends
        self.dashboard = dashboard
        self.dashboard_port = dashboard_port
        self.api_port = api_port
        self.update_github_webhook = update_github_webhook
        self.webhook_history: List[Dict] = []
        self.max_history = 100  # Keep last 100 webhooks
        self.connection_start_time = None
        self.last_keepalive = None
        self.keepalive_timeout = 35  # Server sends keepalive every 30s, timeout after 35s
        self.reconnect_delay = 1  # Initial reconnect delay in seconds
        self.max_reconnect_delay = 60  # Maximum reconnect delay
        self.reconnect_attempts = 0
        
        # Initialize request logger (only if enabled)
        log_file = os.getenv("TERRATUNNEL_REQUEST_LOG")
        self.request_logger = RequestLogger() if log_file else None
        
        parsed = urlparse(server_url)
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        self.ws_url = f"{ws_scheme}://{parsed.netloc}/ws"
        
        # Ensure local endpoint has a protocol
        if not local_endpoint.startswith(('http://', 'https://')):
            self.local_url = f"http://{local_endpoint}"
        else:
            self.local_url = local_endpoint

    async def connect(self):
        try:
            logger.info(f"Connecting to tunnel server at {self.ws_url}...")
            
            # Prepare headers with API key if provided
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            
            # Add connection timeout and ping interval for better connection health monitoring
            self.websocket = await websockets.connect(
                self.ws_url,
                extra_headers=headers,
                ping_interval=10,  # Send ping every 10 seconds
                ping_timeout=20,   # Wait 20 seconds for pong
                close_timeout=10,  # Wait 10 seconds for close handshake
                max_size=512 * 1024 * 1024  # 512MB max message size
            )
            
            # Send local endpoint information to server
            endpoint_info = {
                "type": "client_info",
                "local_endpoint": self.local_endpoint
            }
            
            # Add subdomain if specified (for admin users)
            
            async with self.websocket_send_lock:
                await self.websocket.send(json.dumps(endpoint_info))
            
            # Wait for hostname assignment with timeout
            try:
                hostname_message = await asyncio.wait_for(self.websocket.recv(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.error("Timeout waiting for hostname assignment from server")
                await self.websocket.close()
                return False
                
            hostname_data = json.loads(hostname_message)
            
            if hostname_data.get("type") == "hostname_assigned":
                self.assigned_hostname = hostname_data.get("hostname")
                self.subdomain = hostname_data.get("subdomain")
                
                with self.lock:
                    self.running = True
                    self.connection_start_time = datetime.now(timezone.utc)
                    self.last_keepalive = time.time()
                    self.reconnect_delay = 1  # Reset reconnect delay on successful connection
                    self.reconnect_attempts = 0
                
                logger.info(f"Tunnel ready! Access your local server at: https://{self.assigned_hostname}")
                logger.info(f"Connected to tunnel server with hostname: {self.assigned_hostname}")
                logger.info(f"Forwarding to local endpoint: {self.local_url}")
                
                # Automatically update GitHub webhook URL if flag is enabled and environment variables are set
                if self.update_github_webhook and os.getenv("GITHUB_APP_ID") and os.getenv("GITHUB_APP_PEM"):
                    webhook_path = os.getenv("WEBHOOK_PATH", "/webhook")
                    await self.update_github_webhook_url(webhook_path)
                
                return True
            else:
                logger.error("Did not receive hostname assignment from server")
                if self.websocket:
                    await self.websocket.close()
                return False
                
        except websockets.exceptions.InvalidURI:
            logger.error(f"Invalid WebSocket URL: {self.ws_url}")
            return False
        except websockets.exceptions.InvalidStatusCode as e:
            if e.status_code == 401:
                logger.error("Invalid API key - authentication failed")
            else:
                logger.error(f"WebSocket connection failed with status {e.status_code}")
            return False
        except websockets.exceptions.ConnectionClosedError as e:
            # Handle policy violation (1008) for authentication errors
            if e.code == 1008 and "Authentication required" in str(e):
                logger.error("Invalid API key - authentication failed")
            else:
                logger.error(f"WebSocket connection closed: {e}")
            return False
        except websockets.exceptions.WebSocketException as e:
            logger.error(f"WebSocket error during connection: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to connect to tunnel server: {e}")
            if self.websocket:
                try:
                    await self.websocket.close()
                except:
                    pass
            return False

    def _record_webhook(self, request_data: dict, response_data: dict):
        """Record webhook request and response for dashboard"""
        if not self.dashboard:
            return
            
        webhook_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "method": request_data.get("method", "GET"),
            "path": request_data.get("path", "/"),
            "headers": request_data.get("headers", {}),
            "query_params": request_data.get("query_params", {}),
            "body": request_data.get("body", ""),
            "response_status": response_data.get("status_code", 0),
            "response_headers": response_data.get("headers", {}),
            "response_body": response_data.get("body", "")
        }
        
        with self.lock:
            self.webhook_history.insert(0, webhook_record)  # Insert at beginning
            if len(self.webhook_history) > self.max_history:
                self.webhook_history.pop()  # Remove oldest

    async def _process_request(self, request_data: dict):
        """Process a request asynchronously to avoid blocking other requests."""
        request_id = request_data.get("request_id", "unknown")
        method = request_data.get("method", "GET")
        path = request_data.get("path", "/")
        
        logger.debug(f"[{request_id}] Starting to process {method} {path}")
        
        try:
            # Handle HTTP request and track duration
            start_time = time.time()
            logger.debug(f"[{request_id}] Calling handle_request")
            response_data = await self.handle_request(request_data)
            duration_ms = (time.time() - start_time) * 1000
            
            logger.debug(f"[{request_id}] Request completed in {duration_ms:.0f}ms, status: {response_data.get('status_code')}")
            
            # Log request with duration
            self._log_request(request_data, response_data, duration_ms)
            
            # Include request_id in response if present
            if request_data.get("request_id"):
                response_data["request_id"] = request_data["request_id"]
            
            # Check if response needs streaming
            if response_data.get("is_streaming"):
                logger.debug(f"[CLIENT-PROCESS] Response marked for streaming")
                logger.debug(f"[CLIENT-PROCESS] Stream info: {response_data.get('stream')}")
                
                # Remove the response object before sending
                streaming_response = response_data.pop("_streaming_response", None)
                logger.debug(f"[CLIENT-PROCESS] Removed streaming response object: {type(streaming_response)}")
                
                # First send the initial response with streaming info
                if self.websocket:
                    logger.debug(f"[{request_id}] Sending initial streaming response via WebSocket")
                    logger.debug(f"[CLIENT-PROCESS] Initial response keys: {list(response_data.keys())}")
                    logger.debug(f"[CLIENT-PROCESS] Initial response is_streaming: {response_data.get('is_streaming')}")
                    async with self.websocket_send_lock:
                        # Check again inside the lock
                        if self.websocket:
                            await self.websocket.send(json.dumps(response_data))
                            logger.debug(f"[{request_id}] Initial streaming response sent")
                        else:
                            logger.warning(f"[{request_id}] WebSocket closed before sending initial streaming response")
                            return
                
                # Put the response back for streaming
                if streaming_response:
                    response_data["_streaming_response"] = streaming_response
                    logger.debug(f"[CLIENT-PROCESS] Restored streaming response for content streaming")
                
                # Now stream the actual content
                await self._stream_response_content(request_id, response_data)
            else:
                # Send response back through WebSocket
                if self.websocket:
                    logger.debug(f"[{request_id}] Sending response back via WebSocket")
                    async with self.websocket_send_lock:
                        # Check again inside the lock
                        if self.websocket:
                            await self.websocket.send(json.dumps(response_data))
                            logger.debug(f"[{request_id}] Response sent successfully")
                        else:
                            logger.warning(f"[{request_id}] WebSocket closed before sending response")
                
        except Exception as e:
            logger.error(f"Error processing request: {e}")
            # Send error response if possible
            if self.websocket and request_data.get("request_id"):
                error_response = {
                    "request_id": request_data["request_id"],
                    "status_code": 500,
                    "headers": {"content-type": "text/plain"},
                    "body": f"Internal error: {str(e)}"
                }
                try:
                    async with self.websocket_send_lock:
                        # Check again inside the lock
                        if self.websocket:
                            await self.websocket.send(json.dumps(error_response))
                except Exception:
                    pass

    async def _stream_response_content(self, request_id: str, response_data: dict):
        """Stream large response content in chunks."""
        stream_info = response_data.get("stream", {})
        stream_id = stream_info.get("id")
        response = response_data.get("_streaming_response")
        
        logger.debug(f"[CLIENT-STREAM] Starting _stream_response_content for request {request_id}")
        logger.debug(f"[CLIENT-STREAM] Stream info: {stream_info}")
        
        if not response:
            logger.error(f"[CLIENT-STREAM] No response object found for streaming")
            return
        
        content_size = stream_info.get("total_size", 0)
        
        try:
            logger.info(f"[CLIENT-STREAM] Starting to stream {content_size} bytes for stream {stream_id}")
            
            # Read content once to avoid multiple accesses
            response_content = response.content
            logger.info(f"[CLIENT-STREAM] Response content type: {type(response_content)}, size: {len(response_content)}")
            logger.info(f"[CLIENT-STREAM] First 100 bytes of content: {response_content[:100]!r}")
            
            # Verify we actually have content
            if not response_content:
                logger.error(f"[CLIENT-STREAM] ERROR: Response content is empty! Cannot stream empty content.")
                logger.error(f"[CLIENT-STREAM] Response object: {response}")
                logger.error(f"[CLIENT-STREAM] Response headers: {getattr(response, 'headers', 'N/A')}")
                return
            
            # Create streamer
            streamer = ChunkedStreamer(chunk_size=DEFAULT_CHUNK_SIZE)
            
            # Stream the response content
            chunk_count = 0
            logger.info(f"[CLIENT-STREAM] Starting to iterate through chunks from streamer.stream_bytes")
            
            # Process any pending keepalives before starting
            await self._process_pending_keepalives()
            
            async for chunk_message in streamer.stream_bytes(response_content, stream_id):
                if self.websocket:
                    message_type = chunk_message.get("type")
                    logger.info(f"[CLIENT-STREAM] Processing {message_type} message for stream {stream_id}")
                    
                    # Log the actual message being sent
                    if message_type == "stream_chunk":
                        chunk_info = chunk_message.get("chunk", {})
                        logger.info(f"[CLIENT-STREAM] Chunk details: index={chunk_info.get('index')}/{chunk_info.get('total')-1}, size={chunk_info.get('size')}, data_len={len(chunk_info.get('data', ''))}, checksum={chunk_info.get('checksum', '')[:8]}...")
                    
                    message_json = json.dumps(chunk_message)
                    logger.info(f"[CLIENT-STREAM] Sending WebSocket message type={message_type}, size={len(message_json)} bytes")
                    
                    async with self.websocket_send_lock:
                        # Check again inside the lock to prevent race condition
                        if self.websocket:
                            await self.websocket.send(message_json)
                            logger.info(f"[CLIENT-STREAM] Successfully sent {message_type} message via WebSocket")
                        else:
                            logger.error(f"[CLIENT-STREAM] WebSocket closed during streaming, aborting stream {stream_id}")
                            break
                    
                    if message_type == "stream_chunk":
                        chunk_count += 1
                        chunk_info = chunk_message.get("chunk", {})
                        logger.info(f"[CLIENT-STREAM] Sent chunk {chunk_info.get('index') + 1}/{chunk_info.get('total')} successfully (total sent: {chunk_count})")
                        
                        # Process keepalives every 10 chunks to prevent timeout
                        if chunk_count % 10 == 0:
                            await self._process_pending_keepalives()
                            
                    elif message_type == "stream_complete":
                        logger.info(f"[CLIENT-STREAM] Sent stream completion message with checksum: {chunk_message.get('checksum')}")
                        logger.info(f"[CLIENT-STREAM] Stream {stream_id} completed: sent {chunk_count} chunks")
            
            logger.info(f"[CLIENT-STREAM] Streaming loop completed - total chunks sent: {chunk_count}")
            if chunk_count == 0:
                logger.error(f"[CLIENT-STREAM] WARNING: No chunks were sent! This indicates a problem.")
            
        except Exception as e:
            logger.error(f"[{request_id}] Error during streaming: {e}")
            # Send error message
            if self.websocket:
                error_msg = StreamMessage.error(stream_id, str(e))
                async with self.websocket_send_lock:
                    # Check again inside the lock
                    if self.websocket:
                        await self.websocket.send(json.dumps(error_msg))
                    else:
                        logger.warning(f"[CLIENT-STREAM] Cannot send error message - WebSocket closed")
        
        finally:
            # Clean up the response object from response_data
            if "_streaming_response" in response_data:
                del response_data["_streaming_response"]

    def _log_request(self, request_data: dict, response_data: dict, duration_ms: Optional[float] = None):
        """Log request to file (if enabled) and display access log to console.
        
        Args:
            request_data: Request details
            response_data: Response details  
            duration_ms: Request duration in milliseconds (optional)
        """
        method = request_data.get("method", "GET")
        path = request_data.get("path", "/")
        status_code = response_data.get("status_code", 0)
        
        # Add query string if present
        query_params = request_data.get("query_params", {})
        if query_params:
            query_str = "?" + "&".join([f"{k}={v}" for k, v in query_params.items()])
            path += query_str
        
        if self.request_logger:
            # Log full details to file and get request ID
            request_id = self.request_logger.log_request(request_data, response_data)
            
            # Display concise access log to console with request ID
            access_log = self.request_logger.format_access_log(
                request_data, response_data, request_id, duration_ms
            )
            logger.info(access_log)
        else:
            # Simple access log without request ID when logging is disabled
            timestamp = datetime.now().strftime("%H:%M:%S")
            duration_str = f" {duration_ms:.0f}ms" if duration_ms is not None else ""
            logger.info(f"[{timestamp}] {method} {path} -> {status_code}{duration_str}")


    async def handle_request(self, request_data: dict) -> dict:
        request_id = request_data.get("request_id", "unknown")
        try:
            method = request_data.get("method", "GET")
            path = request_data.get("path", "/")
            headers = request_data.get("headers", {})
            query_params = request_data.get("query_params", {})
            body = request_data.get("body", "")
            
            logger.debug(f"[{request_id}] Processing {method} {path}")
            
            # Handle binary request body if present
            if request_data.get("is_binary") and body:
                logger.debug(f"[{request_id}] Decoding binary request body")
                body = base64.b64decode(body)
            
            url = f"{self.local_url}{path}"
            logger.debug(f"[{request_id}] Making request to {url}")
            
            async with httpx.AsyncClient() as client:
                logger.debug(f"[{request_id}] Sending HTTP request...")
                response = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=query_params,
                    content=body,
                    timeout=600.0  # 10 minutes for large files
                )
                logger.debug(f"[{request_id}] HTTP request completed, status: {response.status_code}")
                
                # Check if response is binary based on content-type
                content_type = response.headers.get("content-type", "").lower()
                is_binary = is_binary_content(content_type)
                
                response_data = {
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                }
                
                # Check response size before encoding
                # Base64 encoding increases size by ~33%, so we need to account for that
                # Plus JSON overhead and WebSocket framing
                # Default: 50MB raw data (will be ~67MB encoded)
                # This conservative limit prevents server OOM issues
                MAX_RESPONSE_SIZE = int(os.environ.get('TERRATUNNEL_MAX_RESPONSE_SIZE', str(50 * 1024 * 1024)))
                
                if is_binary:
                    # Check content length from headers first
                    content_length = response.headers.get("content-length")
                    if content_length:
                        content_size = int(content_length)
                        use_streaming = content_size > STREAMING_THRESHOLD
                    else:
                        # No Content-Length header - always stream to avoid loading entire response
                        logger.debug(f"[{request_id}] No Content-Length header, will use streaming")
                        content_size = 0  # Unknown size
                        use_streaming = True
                    
                    logger.debug(f"[{request_id}] Response is binary, size: {content_size if content_size > 0 else 'unknown'} bytes")
                    
                    # Use streaming for large files or when size is unknown
                    if use_streaming:
                        if content_size > 0:
                            logger.info(f"[{request_id}] Large response ({content_size} bytes > {STREAMING_THRESHOLD} threshold), using streaming")
                        else:
                            logger.info(f"[{request_id}] Response size unknown (no Content-Length header), using streaming")
                        
                        stream_id = str(uuid.uuid4())
                        logger.debug(f"[CLIENT-STREAM-INIT] Created stream ID: {stream_id}")
                        logger.debug(f"[CLIENT-STREAM-INIT] Response object type: {type(response)}")
                        logger.debug(f"[CLIENT-STREAM-INIT] Response content available: {hasattr(response, 'content')}")
                        
                        # Calculate actual content size once
                        actual_content_size = content_size
                        if content_size <= 0 and hasattr(response, 'content'):
                            actual_content_size = len(response.content)
                        logger.debug(f"[CLIENT-STREAM-INIT] Content size: {actual_content_size}")
                        
                        # Send initial response with streaming info
                        response_data["body"] = ""
                        response_data["is_binary"] = True
                        response_data["is_streaming"] = True
                        response_data["stream"] = {
                            "id": stream_id,
                            "total_size": actual_content_size,
                            "content_type": content_type
                        }
                        
                        logger.debug(f"[CLIENT-STREAM-INIT] Set response_data streaming fields: is_streaming={response_data.get('is_streaming')}, stream={response_data.get('stream')}")
                        
                        # Return response data with streaming flag
                        # The initial response will be sent by _process_request
                        response_data["_streaming_response"] = response  # Store for later streaming
                        logger.debug(f"[CLIENT-STREAM-INIT] Stored response object for later streaming")
                        return response_data
                    
                    # For smaller files, use regular encoding
                    logger.debug(f"[{request_id}] Encoding to base64...")
                    response_data["body"] = base64.b64encode(response.content).decode('ascii')
                    response_data["is_binary"] = True
                    logger.debug(f"[{request_id}] Base64 encoding complete, size: {len(response_data['body'])} chars")
                else:
                    text_size = len(response.text.encode('utf-8'))
                    logger.debug(f"[{request_id}] Response is text, size: {text_size} bytes")
                    
                    if text_size > MAX_RESPONSE_SIZE:
                        logger.error(f"[{request_id}] Response too large: {text_size} bytes exceeds {MAX_RESPONSE_SIZE} bytes limit")
                        size_mb = text_size / (1024 * 1024)
                        max_mb = MAX_RESPONSE_SIZE / (1024 * 1024)
                        return {
                            "status_code": 413,
                            "headers": {"content-type": "text/plain"},
                            "body": f"Response too large: {size_mb:.1f}MB exceeds maximum allowed size of {max_mb:.1f}MB. Terratunnel currently does not support streaming large files.",
                            "is_binary": False
                        }
                    
                    response_data["body"] = response.text
                    response_data["is_binary"] = False
                
                # Record webhook for dashboard
                self._record_webhook(request_data, response_data)
                
                return response_data
                
        except Exception as e:
            logger.error(f"Error handling local request: {e}")
            error_response = {
                "status_code": 500,
                "headers": {},
                "body": f"Internal server error: {str(e)}",
                "is_binary": False
            }
            
            # Record error for dashboard
            self._record_webhook(request_data, error_response)
            
            return error_response
    
    async def _process_pending_keepalives(self):
        """Process any pending keepalive messages without blocking"""
        if not self.websocket:
            return
            
        processed_count = 0
        max_messages = 10  # Process up to 10 messages to avoid blocking too long
        
        while processed_count < max_messages:
            try:
                # Try to receive a message with zero timeout
                message = await asyncio.wait_for(self.websocket.recv(), timeout=0.001)
                request_data = json.loads(message)
                
                if request_data.get("type") == "keepalive":
                    logger.debug(f"[CLIENT-STREAM] Processing keepalive during streaming")
                    async with self.websocket_send_lock:
                        if self.websocket:
                            await self.websocket.send(json.dumps({"type": "keepalive_ack"}))
                    with self.lock:
                        self.last_keepalive = time.time()
                else:
                    # Put the message back by storing it for later processing
                    logger.warning(f"[CLIENT-STREAM] Received non-keepalive message during streaming: {request_data.get('type')}")
                    # For now, we'll just log it. In a production system, you'd want to queue this.
                    
                processed_count += 1
                
            except asyncio.TimeoutError:
                # No messages pending, that's fine
                break
            except Exception as e:
                # Websocket might be closed or other error
                logger.debug(f"[CLIENT-STREAM] Error processing keepalives: {e}")
                break

    async def monitor_keepalive(self):
        """Monitor keepalive messages and detect connection failures"""
        while self.running and self.websocket:
            try:
                await asyncio.sleep(5)  # Check every 5 seconds
                
                with self.lock:
                    if self.last_keepalive and time.time() - self.last_keepalive > self.keepalive_timeout:
                        logger.warning(f"No keepalive received for {self.keepalive_timeout} seconds, connection may be dead")
                        # Force close the websocket to trigger reconnection
                        if self.websocket:
                            await self.websocket.close()
                        break
            except Exception as e:
                logger.error(f"Error in keepalive monitor: {e}")
                break

    async def listen(self):
        # Start keepalive monitor in background
        monitor_task = asyncio.create_task(self.monitor_keepalive())
        
        try:
            while self.running:
                try:
                    if not self.websocket:
                        break
                    
                    # Use timeout to make recv() interruptible
                    try:
                        message = await asyncio.wait_for(self.websocket.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                        
                    request_data = json.loads(message)
                    
                    if request_data.get("type") == "keepalive":
                        async with self.websocket_send_lock:
                            # Check if websocket is still available
                            if self.websocket:
                                await self.websocket.send(json.dumps({"type": "keepalive_ack"}))
                        with self.lock:
                            self.last_keepalive = time.time()
                        continue
                    
                    # Handle HTTP request concurrently
                    request_id = request_data.get("request_id", "unknown")
                    logger.debug(f"Received request {request_id} - {request_data.get('method')} {request_data.get('path')}")
                    task = asyncio.create_task(self._process_request(request_data))
                    logger.debug(f"Created async task for request {request_id}")
                    
                except websockets.exceptions.ConnectionClosed:
                    logger.info("WebSocket connection closed")
                    break
                except Exception as e:
                    logger.error(f"Error in message loop: {e}")
                    break
        finally:
            # Cancel the monitor task
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
            
            with self.lock:
                self.websocket = None
                # Clear connection-related state
                self.connection_start_time = None
                self.last_keepalive = None

    async def run(self):
        while self.running:
            try:
                if await self.connect():
                    # Connection successful
                    try:
                        await self.listen()
                    except Exception as e:
                        logger.error(f"Error in client loop: {e}")
                else:
                    # Connection failed
                    with self.lock:
                        self.reconnect_attempts += 1
                
                if not self.running:
                    break
                
                # Calculate reconnect delay with exponential backoff
                with self.lock:
                    delay = min(self.reconnect_delay * (2 ** min(self.reconnect_attempts - 1, 5)), self.max_reconnect_delay)
                
                logger.info(f"Reconnecting in {delay} seconds... (attempt {self.reconnect_attempts})")
                
                # Use shorter sleep intervals to be more responsive to shutdown
                sleep_intervals = int(delay * 10)  # 0.1 second intervals
                for _ in range(sleep_intervals):
                    if not self.running:
                        break
                    await asyncio.sleep(0.1)
                    
            except Exception as e:
                logger.error(f"Unexpected error in run loop: {e}")
                # Continue with reconnection logic
                if not self.running:
                    break
                await asyncio.sleep(1)

    def stop(self):
        with self.lock:
            self.running = False
    
    async def stop_async(self):
        self.stop()
        if self.websocket:
            try:
                await self.websocket.close()
            except Exception:
                pass

    async def update_github_webhook_url(self, webhook_path: str = "/webhook"):
        """Update GitHub application webhook URL with the tunnel endpoint"""
        try:
            logger.info("Updating the GitHub application webhook url")
            
            # Get required environment variables
            github_app_id = os.getenv("GITHUB_APP_ID")
            github_app_pem = os.getenv("GITHUB_APP_PEM")
            github_api_endpoint = os.getenv("GITHUB_API_ENDPOINT", "https://api.github.com")
            
            # Handle escaped newlines in PEM key
            if github_app_pem and "\\n" in github_app_pem:
                github_app_pem = github_app_pem.replace("\\n", "\n")

            if not github_app_id:
                logger.error("GITHUB_APP_ID environment variable not set")
                return False
                
            if not github_app_pem:
                logger.error("GITHUB_APP_PEM environment variable not set")
                return False
                
            if not self.assigned_hostname:
                logger.error("No tunnel hostname assigned yet")
                return False
            
            # Construct the webhook URL using tunnel address
            webhook_url = f"https://{self.assigned_hostname}{webhook_path}"
            logger.info(f"Setting webhook URL to: {webhook_url}")
            
            # Create JWT payload with issue and expiry time
            current_time = int(time.time())
            jwt_payload = {
                "iat": current_time,
                "exp": current_time + 300,  # 5 minutes
                "iss": github_app_id
            }
            
            # Encode the JWT using the private key
            jwt_token = jwt.encode(jwt_payload, github_app_pem, algorithm="RS256")
            
            # Set request headers with JWT token
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "Terratunnel/1.0"
            }
            
            # Define payload with new webhook configuration
            webhook_payload = {
                "content_type": "json",
                "url": webhook_url
            }
            
            # Send PATCH request to GitHub API to update webhook
            async with httpx.AsyncClient() as client:
                response = await client.patch(
                    f"{github_api_endpoint}/app/hook/config",
                    json=webhook_payload,
                    headers=headers,
                    timeout=30.0
                )
                
                if response.status_code == 200:
                    logger.info("GitHub webhook URL updated successfully")
                    logger.info(f"Response: {response.text}")
                    return True
                else:
                    logger.error(f"Failed to update GitHub webhook URL: {response.status_code}")
                    logger.error(f"Response: {response.text}")
                    return False
                    
        except jwt.InvalidKeyError:
            logger.error("Invalid GitHub App private key format")
            return False
        except jwt.PyJWTError as e:
            logger.error(f"JWT error: {e}")
            return False
        except httpx.RequestError as e:
            logger.error(f"HTTP request error: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error updating GitHub webhook URL: {e}")
            return False

    def create_dashboard_app(self) -> FastAPI:
        """Create FastAPI dashboard application"""
        dashboard_app = FastAPI(title="Terratunnel Dashboard")
        
        @dashboard_app.get("/", response_class=HTMLResponse)
        async def dashboard():
            return self._generate_dashboard_html()
        
        @dashboard_app.get("/api/webhooks")
        async def get_webhooks():
            with self.lock:
                return {
                    "webhooks": self.webhook_history,
                    "total": len(self.webhook_history),
                    "tunnel_hostname": self.assigned_hostname
                }
        
        @dashboard_app.get("/api/status")
        async def get_status():
            connected = self.websocket is not None and not self.websocket.closed
            return {
                "running": self.running,
                "connected": connected,
                "tunnel_hostname": self.assigned_hostname,
                "local_endpoint": self.local_endpoint,
                "webhook_count": len(self.webhook_history)
            }
        
        @dashboard_app.post("/api/retry/{webhook_index}")
        async def retry_webhook(webhook_index: int):
            try:
                with self.lock:
                    if webhook_index < 0 or webhook_index >= len(self.webhook_history):
                        return {"success": False, "error": "Invalid webhook index"}
                    
                    webhook = self.webhook_history[webhook_index]
                
                # Reconstruct the original request data
                request_data = {
                    "method": webhook["method"],
                    "path": webhook["path"],
                    "headers": webhook["headers"],
                    "query_params": webhook["query_params"],
                    "body": webhook["body"]
                }
                
                # Retry the request
                response_data = await self.handle_request(request_data)
                
                return {
                    "success": True,
                    "original_status": webhook["response_status"],
                    "new_status": response_data["status_code"],
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
                
            except Exception as e:
                logger.error(f"Error retrying webhook {webhook_index}: {e}")
                return {"success": False, "error": str(e)}
        
        return dashboard_app
    
    def create_api_app(self) -> FastAPI:
        """Create simple JSON API application"""
        api_app = FastAPI(title="Terratunnel Client API", version="1.0.0")
        
        @api_app.get("/status")
        async def get_status():
            """Get current client status"""
            with self.lock:
                uptime_seconds = None
                if self.connection_start_time:
                    uptime_seconds = (datetime.now(timezone.utc) - self.connection_start_time).total_seconds()
                
                connected = self.websocket is not None and not self.websocket.closed
                
                return {
                    "running": self.running,
                    "connected": connected,
                    "tunnel_hostname": self.assigned_hostname,
                    "local_endpoint": self.local_endpoint,
                    "server_url": self.server_url,
                    "webhook_count": len(self.webhook_history),
                    "uptime_seconds": uptime_seconds,
                    "connection_start_time": self.connection_start_time.isoformat() if self.connection_start_time else None,
                    "reconnect_attempts": self.reconnect_attempts,
                    "last_keepalive_ago": time.time() - self.last_keepalive if self.last_keepalive else None
                }
        
        @api_app.get("/webhooks")
        async def get_webhooks(limit: int = 10):
            """Get recent webhooks"""
            with self.lock:
                limited_webhooks = self.webhook_history[:limit] if limit > 0 else self.webhook_history
                return {
                    "webhooks": limited_webhooks,
                    "total_count": len(self.webhook_history),
                    "limit": limit
                }
        
        @api_app.get("/webhooks/stats")
        async def get_webhook_stats():
            """Get webhook statistics"""
            with self.lock:
                if not self.webhook_history:
                    return {
                        "total_count": 0,
                        "status_codes": {},
                        "methods": {},
                        "latest_webhook": None
                    }
                
                # Count status codes and methods
                status_codes = {}
                methods = {}
                
                for webhook in self.webhook_history:
                    status = webhook.get("response_status", 0)
                    method = webhook.get("method", "UNKNOWN")
                    
                    status_codes[str(status)] = status_codes.get(str(status), 0) + 1
                    methods[method] = methods.get(method, 0) + 1
                
                return {
                    "total_count": len(self.webhook_history),
                    "status_codes": status_codes,
                    "methods": methods,
                    "latest_webhook": self.webhook_history[0] if self.webhook_history else None
                }
        
        @api_app.get("/health")
        async def health_check():
            """Simple health check"""
            return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}
        
        return api_app
    
    def _generate_dashboard_html(self) -> str:
        """Generate HTML dashboard"""
        return '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Terratunnel Dashboard</title>
    <style>
        * { box-sizing: border-box; }
        
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif; 
            margin: 0; 
            padding: 20px; 
            background: #f8f9fa;
            min-height: 100vh;
            color: #212529;
            line-height: 1.5;
        }
        
        .container { 
            max-width: 1200px; 
            margin: 0 auto; 
        }
        
        .header { 
            background: white; 
            padding: 2rem; 
            border-radius: 8px; 
            margin-bottom: 1.5rem; 
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            border: 1px solid #dee2e6;
        }
        
        .header h1 { 
            margin: 0 0 1.5rem 0; 
            font-size: 2rem; 
            font-weight: 600; 
            color: #212529;
        }
        
        .status-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
            margin-bottom: 1.5rem;
        }
        
        .status-item {
            background: #f8f9fa;
            padding: 1rem;
            border-radius: 6px;
            border: 1px solid #e9ecef;
        }
        
        .status-item strong {
            display: block;
            font-size: 0.875rem;
            color: #6c757d;
            margin-bottom: 0.25rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            font-weight: 500;
        }
        
        .status-value {
            font-size: 1rem;
            font-weight: 600;
            color: #212529;
        }
        
        .status-connected { color: #198754; }
        .status-disconnected { color: #dc3545; }
        
        .controls {
            display: flex;
            gap: 0.75rem;
            align-items: center;
        }
        
        .refresh-btn { 
            background: #0d6efd; 
            color: white; 
            border: none; 
            padding: 0.5rem 1rem; 
            border-radius: 6px; 
            cursor: pointer; 
            font-weight: 500;
            font-size: 0.875rem;
            transition: background-color 0.15s ease;
        }
        
        .refresh-btn:hover { 
            background: #0b5ed7;
        }
        
        .webhook-list { 
            background: white; 
            padding: 2rem; 
            border-radius: 8px; 
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            border: 1px solid #dee2e6;
        }
        
        .webhook-list h2 {
            margin: 0 0 1.5rem 0;
            font-size: 1.5rem;
            font-weight: 600;
            color: #212529;
        }
        
        .webhook-item { 
            background: white;
            border: 1px solid #dee2e6; 
            margin-bottom: 1rem; 
            border-radius: 6px; 
            overflow: hidden;
            transition: box-shadow 0.15s ease;
        }
        
        .webhook-item:hover {
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        
        .webhook-header { 
            background: #f8f9fa; 
            padding: 1rem; 
            cursor: pointer;
            transition: background-color 0.15s ease;
            display: flex;
            align-items: center;
            gap: 1rem;
            flex-wrap: wrap;
        }
        
        .webhook-header:hover {
            background: #e9ecef;
        }
        
        .webhook-main-info {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            flex: 1;
            min-width: 0;
        }
        
        .webhook-actions {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }
        
        .webhook-details { 
            padding: 1.5rem; 
            display: none; 
            background: #fafbfc;
            border-top: 1px solid #dee2e6;
        }
        
        .method { 
            padding: 0.25rem 0.75rem; 
            border-radius: 4px; 
            color: white; 
            font-weight: 600; 
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            min-width: 60px;
            text-align: center;
        }
        
        .method.GET { background: #198754; }
        .method.POST { background: #0d6efd; }
        .method.PUT { background: #fd7e14; }
        .method.DELETE { background: #dc3545; }
        .method.PATCH { background: #6f42c1; }
        
        .webhook-path {
            font-family: 'SF Mono', Monaco, 'Cascadia Code', 'Roboto Mono', Consolas, 'Courier New', monospace;
            font-weight: 500;
            font-size: 0.875rem;
            color: #495057;
            word-break: break-all;
        }
        
        .status-badge {
            padding: 0.25rem 0.5rem;
            border-radius: 4px;
            font-weight: 600;
            font-size: 0.75rem;
            min-width: 50px;
            text-align: center;
        }
        
        .status-200 { background: #d1e7dd; color: #0f5132; }
        .status-300 { background: #fff3cd; color: #664d03; }
        .status-400 { background: #f8d7da; color: #58151c; }
        .status-500 { background: #e2e3f0; color: #41464b; }
        
        .timestamp { 
            color: #6c757d; 
            font-size: 0.75rem; 
            font-weight: 400;
            width: 100%;
            margin-top: 0.5rem;
        }
        
        .retry-btn { 
            background: #198754; 
            color: white; 
            border: none; 
            padding: 0.375rem 0.75rem; 
            border-radius: 4px; 
            cursor: pointer; 
            font-size: 0.75rem; 
            font-weight: 500;
            transition: background-color 0.15s ease;
        }
        
        .retry-btn:hover { 
            background: #157347;
        }
        
        .retry-btn:disabled { 
            background: #6c757d; 
            cursor: not-allowed; 
        }
        
        .retry-status { 
            margin: 1rem 0; 
            padding: 0.75rem; 
            border-radius: 6px; 
            font-size: 0.875rem; 
        }
        
        .retry-success { 
            background: #d1e7dd; 
            color: #0f5132; 
            border: 1px solid #a3cfbb; 
        }
        
        .retry-error { 
            background: #f8d7da; 
            color: #58151c; 
            border: 1px solid #f1aeb5; 
        }
        
        .no-webhooks { 
            text-align: center; 
            color: #6c757d; 
            padding: 3rem 1rem;
            font-size: 1rem;
        }
        
        .section-title {
            font-size: 0.875rem;
            font-weight: 600;
            color: #495057;
            margin: 1.5rem 0 0.75rem 0;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        
        pre { 
            background: #f1f3f4; 
            padding: 1rem; 
            border-radius: 6px; 
            overflow-x: auto; 
            white-space: pre-wrap; 
            font-family: 'SF Mono', Monaco, 'Cascadia Code', 'Roboto Mono', Consolas, 'Courier New', monospace;
            font-size: 0.8125rem;
            line-height: 1.4;
            border: 1px solid #dee2e6;
            margin: 0.5rem 0;
        }
        
        .expand-indicator {
            transition: transform 0.15s ease;
            font-size: 0.75rem;
            color: #6c757d;
        }
        
        .webhook-header.expanded .expand-indicator {
            transform: rotate(90deg);
        }
        
        @media (max-width: 768px) {
            body { padding: 1rem; }
            .header { padding: 1.5rem; }
            .webhook-list { padding: 1.5rem; }
            .webhook-header { padding: 1rem; flex-direction: column; align-items: flex-start; }
            .webhook-main-info { width: 100%; }
            .webhook-actions { width: 100%; justify-content: flex-start; }
            .status-grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Terratunnel Dashboard</h1>
            <div class="status-grid" id="status">Loading...</div>
            <div class="controls">
                <button class="refresh-btn" onclick="loadWebhooks()">Refresh</button>
            </div>
        </div>
        
        <div class="webhook-list">
            <h2>Webhook History</h2>
            <div id="webhooks">Loading webhooks...</div>
        </div>
    </div>

    <script>
        let expandedItems = new Set();
        let lastWebhookCount = 0;

        async function loadStatus() {
            console.log('Loading status...');
            try {
                const response = await fetch('/api/status');
                console.log('Status response:', response.status);
                const status = await response.json();
                console.log('Status data:', status);
                document.getElementById('status').innerHTML = `
                    <div class="status-item">
                        <strong>Connection Status</strong>
                        <div class="status-value ${status.connected ? 'status-connected' : 'status-disconnected'}">
                            ${status.connected ? 'Connected' : 'Disconnected'}
                        </div>
                    </div>
                    <div class="status-item">
                        <strong>Tunnel URL</strong>
                        <div class="status-value">
                            ${status.tunnel_hostname ? 'https://' + status.tunnel_hostname : 'Not assigned'}
                        </div>
                    </div>
                    <div class="status-item">
                        <strong>Local Endpoint</strong>
                        <div class="status-value">${status.local_endpoint}</div>
                    </div>
                    <div class="status-item">
                        <strong>Total Webhooks</strong>
                        <div class="status-value">${status.webhook_count}</div>
                    </div>
                `;
            } catch (error) {
                console.error('Error loading status:', error);
                document.getElementById('status').innerHTML = '<div class="status-item"><strong>Error loading status</strong></div>';
            }
        }

        async function loadWebhooks() {
            console.log('Loading webhooks...');
            try {
                const response = await fetch('/api/webhooks');
                console.log('Webhooks response:', response.status);
                const data = await response.json();
                console.log('Webhooks data:', data);
                const webhooksDiv = document.getElementById('webhooks');
                
                if (data.webhooks.length === 0) {
                    webhooksDiv.innerHTML = '<div class="no-webhooks">No webhooks received yet<br><small>Webhook requests will appear here as they come in</small></div>';
                    expandedItems.clear();
                    lastWebhookCount = 0;
                    return;
                }
                
                // Only re-render if webhook count changed
                if (data.webhooks.length !== lastWebhookCount) {
                    webhooksDiv.innerHTML = data.webhooks.map((webhook, index) => `
                        <div class="webhook-item">
                            <div class="webhook-header ${expandedItems.has(index) ? 'expanded' : ''}" onclick="toggleDetails(${index})">
                                <div class="webhook-main-info">
                                    <span class="method ${webhook.method}">${webhook.method}</span>
                                    <div class="webhook-path">${webhook.path}</div>
                                    <span class="expand-indicator">▶</span>
                                </div>
                                <div class="webhook-actions">
                                    <span class="status-badge status-${Math.floor(webhook.response_status / 100)}00">
                                        ${webhook.response_status}
                                    </span>
                                    <button class="retry-btn" onclick="retryWebhook(${index}, event)" id="retry-btn-${index}">
                                        Retry
                                    </button>
                                </div>
                                <div class="timestamp">${new Date(webhook.timestamp).toLocaleString()}</div>
                            </div>
                            <div class="webhook-details" id="details-${index}" style="display: ${expandedItems.has(index) ? 'block' : 'none'}">
                                <div id="retry-status-${index}"></div>
                                
                                <div class="section-title">Request Headers</div>
                                <pre>${JSON.stringify(webhook.headers, null, 2)}</pre>
                                
                                ${webhook.query_params && Object.keys(webhook.query_params).length > 0 ? `
                                <div class="section-title">Query Parameters</div>
                                <pre>${JSON.stringify(webhook.query_params, null, 2)}</pre>
                                ` : ''}
                                
                                ${webhook.body ? `
                                <div class="section-title">Request Body</div>
                                <pre>${webhook.body}</pre>
                                ` : ''}
                                
                                <div class="section-title">Response Headers</div>
                                <pre>${JSON.stringify(webhook.response_headers, null, 2)}</pre>
                                
                                ${webhook.response_body ? `
                                <div class="section-title">Response Body</div>
                                <pre>${webhook.response_body.substring(0, 1000)}${webhook.response_body.length > 1000 ? '\\n\\n... (truncated)' : ''}</pre>
                                ` : ''}
                            </div>
                        </div>
                    `).join('');
                    
                    lastWebhookCount = data.webhooks.length;
                }
                
                await loadStatus();
            } catch (error) {
                console.error('Error loading webhooks:', error);
                document.getElementById('webhooks').innerHTML = '<div class="no-webhooks">Error loading webhooks</div>';
            }
        }
        
        function toggleDetails(index) {
            const details = document.getElementById(`details-${index}`);
            const header = details.previousElementSibling;
            
            if (expandedItems.has(index)) {
                details.style.display = 'none';
                header.classList.remove('expanded');
                expandedItems.delete(index);
            } else {
                details.style.display = 'block';
                header.classList.add('expanded');
                expandedItems.add(index);
            }
        }
        
        async function retryWebhook(index, event) {
            // Prevent the click from triggering toggleDetails
            event.stopPropagation();
            
            const retryBtn = document.getElementById(`retry-btn-${index}`);
            const retryStatus = document.getElementById(`retry-status-${index}`);
            
            // Disable button and show loading state
            retryBtn.disabled = true;
            retryBtn.textContent = 'Retrying...';
            retryStatus.innerHTML = '';
            
            try {
                const response = await fetch(`/api/retry/${index}`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    }
                });
                
                const result = await response.json();
                
                if (result.success) {
                    const statusChange = result.original_status !== result.new_status ? 
                        ` (${result.original_status} → ${result.new_status})` : 
                        ` (${result.new_status})`;
                    
                    retryStatus.innerHTML = `
                        <div class="retry-status retry-success">
                            ✓ Webhook retried successfully${statusChange}
                            <br><small>Retried at: ${new Date(result.timestamp).toLocaleString()}</small>
                        </div>
                    `;
                    
                    // Auto-expand details to show retry status
                    if (!expandedItems.has(index)) {
                        expandedItems.add(index);
                        document.getElementById(`details-${index}`).style.display = 'block';
                    }
                    
                } else {
                    retryStatus.innerHTML = `
                        <div class="retry-status retry-error">
                            ✗ Retry failed: ${result.error}
                        </div>
                    `;
                }
                
            } catch (error) {
                console.error('Error retrying webhook:', error);
                retryStatus.innerHTML = `
                    <div class="retry-status retry-error">
                        ✗ Retry failed: ${error.message}
                    </div>
                `;
            } finally {
                // Re-enable button
                retryBtn.disabled = false;
                retryBtn.textContent = 'Retry';
            }
        }
        
        // Add global error handler for better debugging
        window.addEventListener('error', function(e) {
            console.error('Global error:', e.error);
            console.error('File:', e.filename, 'Line:', e.lineno, 'Column:', e.colno);
        });
        
        // Add unhandled promise rejection handler
        window.addEventListener('unhandledrejection', function(e) {
            console.error('Unhandled promise rejection:', e.reason);
        });
        
        // Load data on page load
        console.log('Dashboard initializing...');
        loadWebhooks();
        
        // Auto-refresh every 5 seconds
        setInterval(function() {
            console.log('Auto-refreshing webhooks...');
            loadWebhooks();
        }, 5000);
    </script>
</body>
</html>
        '''


async def main_client(server_url: str, local_endpoint: str, dashboard: bool = False, dashboard_port: int = 8080, api_port: int = 8081, update_github_webhook: bool = False, api_key: Optional[str] = None):
    logger.info(f"Starting tunnel client - local endpoint: {local_endpoint}")
    
    client = TunnelClient(server_url, local_endpoint, dashboard, dashboard_port, api_port, update_github_webhook, api_key)
    servers = []
    server_tasks = []
    
    # Set up signal handlers
    shutdown_event = asyncio.Event()
    force_exit = False
    
    def signal_handler(signum, frame):
        nonlocal force_exit
        if force_exit:
            # Second Ctrl+C, force immediate exit
            logger.info("Force exit!")
            os._exit(1)
        
        force_exit = True
        logger.info("Shutting down... (Press Ctrl+C again to force quit)")
        
        # Stop the client
        client.stop()
        
        # Stop all servers
        for server in servers:
            server.should_exit = True
        
        # Set shutdown event
        shutdown_event.set()
        
        # Force exit after 1 second
        def force_shutdown():
            logger.warning("Shutdown timeout, forcing exit...")
            os._exit(1)
        
        import threading
        timer = threading.Timer(1.0, force_shutdown)
        timer.daemon = True
        timer.start()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        # Start dashboard and API servers if enabled
        if dashboard or True:  # Always start API server
            api_app = client.create_api_app()
            api_config = uvicorn.Config(
                app=api_app,
                host="0.0.0.0",
                port=api_port,
                log_level="warning",
                loop="asyncio",
                access_log=False
            )
            api_server = uvicorn.Server(api_config)
            servers.append(api_server)
            
            if dashboard:
                dashboard_app = client.create_dashboard_app()
                dashboard_config = uvicorn.Config(
                    app=dashboard_app,
                    host="0.0.0.0",
                    port=dashboard_port,
                    log_level="warning",
                    loop="asyncio",
                    access_log=False
                )
                dashboard_server = uvicorn.Server(dashboard_config)
                servers.append(dashboard_server)
        
        # Start servers in background tasks
        server_tasks = []
        for server in servers:
            # Disable uvicorn's signal handlers
            server.install_signal_handlers = lambda: None
            task = asyncio.create_task(server.serve())
            server_tasks.append(task)
        
        if dashboard:
            logger.info(f"Dashboard available at: http://localhost:{dashboard_port}")
        logger.info(f"API available at: http://localhost:{api_port}")
        
        # Run the main client with shutdown monitoring
        client_task = asyncio.create_task(client.run())
        shutdown_task = asyncio.create_task(shutdown_event.wait())
        
        # Wait for either client to finish or shutdown signal
        done, pending = await asyncio.wait(
            {client_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # Cancel the other task
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
    finally:
        logger.info("Cleaning up...")
        client.stop()
        
        # Stop servers gracefully
        for server in servers:
            server.should_exit = True
        
        # Give servers a moment to shutdown cleanly
        await asyncio.sleep(0.1)
        
        # Cancel any remaining server tasks
        for task in server_tasks:
            if not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=0.5)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
        
        if client.websocket:
            try:
                await client.websocket.close()
            except:
                pass


def run_client(server_url: str, local_endpoint: str, dashboard: bool = False, dashboard_port: int = 8080, api_port: int = 8081, update_github_webhook: bool = False, api_key: Optional[str] = None, request_log: Optional[str] = None):
    # Set request log path if provided
    if request_log:
        import os
        os.environ['TERRATUNNEL_REQUEST_LOG'] = request_log
    
    try:
        asyncio.run(main_client(server_url, local_endpoint, dashboard, dashboard_port, api_port, update_github_webhook, api_key))
    except KeyboardInterrupt:
        logger.info("Stopping tunnel client...")
    except Exception as e:
        logger.error(f"Client error: {e}")
        sys.exit(1)
