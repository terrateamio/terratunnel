"""Request logging system for detailed request/response logging to a single file."""

import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional
import threading

logger = logging.getLogger("terratunnel-client")


class RequestLogger:
    """Handles detailed request/response logging to a single flat file with UUID tracking."""
    
    def __init__(self, log_file: Optional[str] = None):
        """Initialize the request logger.
        
        Args:
            log_file: Path to the request log file. If not provided, checks TERRATUNNEL_REQUEST_LOG env var.
        """
        log_path = log_file or os.getenv("TERRATUNNEL_REQUEST_LOG")
        if not log_path:
            raise ValueError("Request logger requires a log file path")
            
        self.log_file = Path(log_path)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        
    def log_request(self, request_data: Dict[str, Any], response_data: Dict[str, Any]) -> str:
        """Log a complete request/response to the log file and return the request ID.
        
        Args:
            request_data: Dictionary containing request details
            response_data: Dictionary containing response details
            
        Returns:
            The UUID of the logged request
        """
        # Generate unique request ID
        request_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc)
        
        # Format the log entry
        log_lines = []
        log_lines.append("=" * 80)
        log_lines.append(f"REQUEST ID: {request_id}")
        log_lines.append(f"TIMESTAMP: {timestamp.isoformat()}")
        log_lines.append("")
        
        # Request details
        method = request_data.get("method", "GET")
        path = request_data.get("path", "/")

        # Add query string if present - use raw_query_string if available for proper encoding display
        raw_query_string = request_data.get("raw_query_string", "")
        if raw_query_string:
            path += "?" + raw_query_string
        else:
            # Fallback to parsed params (backward compatibility)
            query_params = request_data.get("query_params", {})
            if query_params:
                query_str = "?" + "&".join([f"{k}={v}" for k, v in query_params.items()])
                path += query_str
            
        log_lines.append(f"REQUEST: {method} {path}")
        
        # Request headers
        request_headers = request_data.get("headers", {})
        if request_headers:
            log_lines.append("REQUEST HEADERS:")
            for key, value in request_headers.items():
                log_lines.append(f"  {key}: {value}")
        
        # Request body
        request_body = request_data.get("body", "")
        if request_body:
            log_lines.append("REQUEST BODY:")
            if request_data.get("is_binary"):
                log_lines.append(f"  [Binary data: {len(request_body)} bytes base64-encoded]")
            else:
                # Split body into lines for better formatting
                for line in request_body.split('\n'):
                    log_lines.append(f"  {line}")
        
        log_lines.append("")
        
        # Response details
        status_code = response_data.get("status_code", 0)
        log_lines.append(f"RESPONSE: {status_code}")
        
        # Response headers
        response_headers = response_data.get("headers", {})
        if response_headers:
            log_lines.append("RESPONSE HEADERS:")
            for key, value in response_headers.items():
                log_lines.append(f"  {key}: {value}")
        
        # Response body
        response_body = response_data.get("body", "")
        if response_body:
            log_lines.append("RESPONSE BODY:")
            if response_data.get("is_binary"):
                log_lines.append(f"  [Binary data: {len(response_body)} bytes base64-encoded]")
            else:
                # Split body into lines for better formatting
                for line in response_body.split('\n'):
                    log_lines.append(f"  {line}")
        
        log_lines.append("")
        
        # Write to file
        with self.lock:
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write('\n'.join(log_lines) + '\n')
        
        return request_id
    
    def format_access_log(self, request_data: Dict[str, Any], response_data: Dict[str, Any], 
                         request_id: str, duration_ms: Optional[float] = None) -> str:
        """Format a concise access log entry for console output.
        
        Args:
            request_data: Dictionary containing request details
            response_data: Dictionary containing response details
            request_id: The UUID of the request
            duration_ms: Request duration in milliseconds (optional)
            
        Returns:
            Formatted access log string
        """
        method = request_data.get("method", "GET")
        path = request_data.get("path", "/")
        status_code = response_data.get("status_code", 0)

        # Add query string if present - use raw_query_string if available for proper encoding display
        raw_query_string = request_data.get("raw_query_string", "")
        if raw_query_string:
            path += "?" + raw_query_string
        else:
            # Fallback to parsed params (backward compatibility)
            query_params = request_data.get("query_params", {})
            if query_params:
                query_str = "?" + "&".join([f"{k}={v}" for k, v in query_params.items()])
                path += query_str
        
        # Format timestamp
        timestamp = datetime.now().strftime("%H:%M:%S")
        
        # Format duration if provided
        duration_str = f" {duration_ms:.0f}ms" if duration_ms is not None else ""
        
        # Create access log entry
        # Format: [timestamp] request_id METHOD /path -> status_code (duration)
        return f"[{timestamp}] {request_id} {method} {path} -> {status_code}{duration_str}"