import logging
from typing import Optional, Tuple, Union

from fastapi import WebSocket, WebSocketDisconnect, status
from fastapi.websockets import WebSocketState

from .database import Database
from .config import Config

logger = logging.getLogger("terratunnel-server")


class AuthMiddleware:
    """Middleware for authenticating WebSocket connections"""
    
    def __init__(self, db: Optional[Database] = None):
        self.db = db
    
    async def authenticate_websocket(self, websocket: WebSocket, client_info: dict) -> Union[None, Tuple[Optional[int], Optional[int], Optional[str]]]:
        """
        Authenticate a WebSocket connection using API key from Authorization header or client_info.
        
        Returns:
            Tuple of (user_id, api_key_id, tunnel_subdomain) if authenticated, None otherwise
        """
        # If auth is not required, return None
        if not Config.REQUIRE_AUTH_FOR_TUNNELS:
            return None, None, None
        
        # First try to get API key from Authorization header
        api_key = None
        auth_header = websocket.headers.get("authorization")
        if auth_header and auth_header.startswith("Bearer "):
            api_key = auth_header[7:]  # Remove "Bearer " prefix
        
        # Fallback to client info for backward compatibility
        if not api_key:
            api_key = client_info.get("api_key")
        
        if not api_key:
            if Config.REQUIRE_AUTH_FOR_TUNNELS:
                await websocket.close(
                    code=status.WS_1008_POLICY_VIOLATION,
                    reason="Authentication required. Please provide an API key."
                )
                return None
            return None, None, None
        
        # Validate API key
        if not self.db:
            logger.error("Database not initialized for authentication")
            await websocket.close(
                code=status.WS_1011_INTERNAL_ERROR,
                reason="Server configuration error"
            )
            return None
        
        api_key_info = self.db.validate_api_key(api_key)
        
        if not api_key_info:
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="Invalid or expired API key"
            )
            return None
        
        # Check if the API key has tunnel creation scope
        scopes = api_key_info.get("scopes", "").split(",")
        if "tunnel:create" not in scopes:
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="API key does not have tunnel creation permission"
            )
            return None
        
        return api_key_info["user_id"], api_key_info["id"], api_key_info.get("tunnel_subdomain")
    
    def check_user_tunnel_limit(self, user_id: int, active_connections: dict) -> bool:
        """
        Check if user has reached their tunnel limit.
        
        Returns:
            True if user can create more tunnels, False otherwise
        """
        # Count active tunnels for this user
        user_tunnel_count = 0
        
        # This would need to be improved with proper tracking
        # For now, we'll allow unlimited tunnels per user
        # In production, you might want to:
        # 1. Track user_id in ConnectionManager
        # 2. Implement per-user limits based on subscription tier
        # 3. Store limits in database
        
        return True