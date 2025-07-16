from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field

from fastapi import APIRouter, HTTPException, Depends, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import httpx

from .database import Database
from .config import Config

# Create API router
api_router = APIRouter(prefix="/api", tags=["api"])

# Security scheme for API endpoints
security = HTTPBearer()


# Request/Response models
class TokenExchangeRequest(BaseModel):
    github_token: str = Field(..., description="GitHub personal access token or OAuth token")
    api_key_name: Optional[str] = Field(None, description="Optional name for the API key")


class TokenExchangeResponse(BaseModel):
    api_key: str = Field(..., description="Terratunnel API key")
    api_key_prefix: str = Field(..., description="API key prefix for identification")
    user_id: int = Field(..., description="User ID in Terratunnel")
    username: str = Field(..., description="GitHub username")
    expires_at: Optional[datetime] = Field(None, description="API key expiration time (null if no expiration)")


class APIKeyInfo(BaseModel):
    id: int
    key_prefix: str
    name: Optional[str]
    created_at: datetime
    last_used_at: Optional[datetime]
    expires_at: Optional[datetime]
    is_active: bool
    scopes: str


class UserInfo(BaseModel):
    id: int
    username: str
    email: Optional[str]
    avatar_url: Optional[str]
    provider: str


async def get_current_user_from_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """Dependency to get current user from API key"""
    db = Database()
    api_key_info = db.validate_api_key(credentials.credentials)
    
    if not api_key_info:
        raise HTTPException(status_code=401, detail="Invalid or expired API key")
    
    return api_key_info


@api_router.post("/auth/exchange", response_model=TokenExchangeResponse)
async def exchange_github_token(request: TokenExchangeRequest):
    """Exchange a GitHub access token for a Terratunnel API key"""
    
    # Validate GitHub token by fetching user info
    async with httpx.AsyncClient() as client:
        try:
            # Get user info from GitHub
            user_response = await client.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {request.github_token}",
                    "Accept": "application/json"
                }
            )
            
            if user_response.status_code == 401:
                raise HTTPException(status_code=401, detail="Invalid GitHub token")
            elif user_response.status_code != 200:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Failed to validate GitHub token: {user_response.status_code}"
                )
            
            github_user = user_response.json()
            
        except httpx.RequestError as e:
            raise HTTPException(status_code=500, detail=f"Failed to connect to GitHub: {str(e)}")
    
    # Create or update user in database
    db = Database()
    
    try:
        user_id = db.create_or_update_user(
            auth_provider="github",
            provider_user_id=str(github_user["id"]),
            provider_username=github_user["login"],
            email=github_user.get("email"),
            name=github_user.get("name"),
            avatar_url=github_user.get("avatar_url")
        )
        
        # Create API key for the user
        api_key_name = request.api_key_name or f"API key created {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        
        # API keys don't expire by default, but you can add expiration logic here if needed
        api_key = db.create_api_key(
            user_id=user_id,
            name=api_key_name,
            expires_at=None,  # No expiration
            scopes="tunnel:create,tunnel:read,tunnel:delete"
        )
        
        # Get the key prefix (first 8 characters)
        api_key_prefix = api_key[:8]
        
        return TokenExchangeResponse(
            api_key=api_key,
            api_key_prefix=api_key_prefix,
            user_id=user_id,
            username=github_user["login"],
            expires_at=None
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create API key: {str(e)}")


@api_router.get("/auth/me", response_model=UserInfo)
async def get_current_user_api(api_key_info: dict = Depends(get_current_user_from_api_key)):
    """Get current user info using API key authentication"""
    
    return UserInfo(
        id=api_key_info["user_id"],
        username=api_key_info["provider_username"],
        email=api_key_info["email"],
        avatar_url=None,  # Not included in API key validation result
        provider=api_key_info["auth_provider"]
    )


@api_router.get("/auth/keys", response_model=list[APIKeyInfo])
async def list_api_keys(api_key_info: dict = Depends(get_current_user_from_api_key)):
    """List all API keys for the current user"""
    
    db = Database()
    keys = db.list_user_api_keys(api_key_info["user_id"])
    
    return [APIKeyInfo(**key) for key in keys]


@api_router.delete("/auth/keys/{key_id}")
async def revoke_api_key(key_id: int, api_key_info: dict = Depends(get_current_user_from_api_key)):
    """Revoke an API key"""
    
    # Don't allow revoking the current API key
    if api_key_info["id"] == key_id:
        raise HTTPException(status_code=400, detail="Cannot revoke the API key currently in use")
    
    db = Database()
    if db.revoke_api_key(api_key_info["user_id"], key_id):
        return {"message": "API key revoked successfully"}
    else:
        raise HTTPException(status_code=404, detail="API key not found or not owned by user")