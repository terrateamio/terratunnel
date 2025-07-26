from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

from fastapi import APIRouter, HTTPException, Depends, Header, Query, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse
import httpx
import sqlite3
import json
import io

from .database import Database
from .config import Config
from .auth import get_current_user_from_cookie

# Create API router
api_router = APIRouter(prefix="/api", tags=["api"])

# Security scheme for API endpoints
security = HTTPBearer()

# Global database instance (set by app.py)
_db: Optional[Database] = None
_domain: Optional[str] = None

def set_database(db: Database):
    """Set the global database instance"""
    global _db
    _db = db

def set_domain(domain: str):
    """Set the global domain"""
    global _domain
    _domain = domain

def get_database() -> Database:
    """Get the global database instance"""
    if _db is None:
        raise RuntimeError("Database not initialized")
    return _db

def get_domain() -> str:
    """Get the global domain"""
    if _domain is None:
        return "tunnel.terrateam.dev"  # Default fallback
    return _domain


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
    tunnel_url: str = Field(..., description="Full tunnel URL for this API key")


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
    db = get_database()
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
    db = get_database()
    
    try:
        user_id = db.create_or_update_user(
            auth_provider="github",
            provider_user_id=str(github_user["id"]),
            provider_username=github_user["login"],
            email=github_user.get("email"),
            name=github_user.get("name"),
            avatar_url=github_user.get("avatar_url")
        )
        
        # Get user info to check if admin
        user = db.get_user_by_id(user_id)
        from .config import Config
        is_admin = Config.is_admin_user(user["auth_provider"], user["provider_username"]) if user else False
        
        # Create API key for the user
        api_key_name = request.api_key_name or f"API key created {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        
        # API keys don't expire by default, but you can add expiration logic here if needed
        api_key = db.create_api_key(
            user_id=user_id,
            name=api_key_name,
            expires_at=None,  # No expiration
            scopes="tunnel:create,tunnel:read,tunnel:delete",
            is_admin=is_admin
        )
        
        # Get the key prefix (first 8 characters)
        api_key_prefix = api_key[:8]
        
        # Get the tunnel subdomain for this user
        # The create_api_key method creates a tunnel if one doesn't exist
        tunnels = db.list_user_tunnels(user_id)
        if not tunnels:
            raise HTTPException(status_code=500, detail="Failed to create tunnel")
        
        tunnel_subdomain = tunnels[0]["subdomain"]
        domain = get_domain()
        tunnel_url = f"{tunnel_subdomain}.{domain}"
        
        return TokenExchangeResponse(
            api_key=api_key,
            api_key_prefix=api_key_prefix,
            user_id=user_id,
            username=github_user["login"],
            expires_at=None,
            tunnel_url=tunnel_url
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
    
    db = get_database()
    keys = db.list_user_api_keys(api_key_info["user_id"])
    
    return [APIKeyInfo(**key) for key in keys]


@api_router.delete("/auth/keys/{key_id}")
async def revoke_api_key(key_id: int, api_key_info: dict = Depends(get_current_user_from_api_key)):
    """Revoke an API key"""
    
    # Don't allow revoking the current API key
    if api_key_info["id"] == key_id:
        raise HTTPException(status_code=400, detail="Cannot revoke the API key currently in use")
    
    db = get_database()
    if db.revoke_api_key(api_key_info["user_id"], key_id):
        return {"message": "API key revoked successfully"}
    else:
        raise HTTPException(status_code=404, detail="API key not found or not owned by user")


# Admin Database Browser Endpoints
async def require_admin_api(api_key_info: dict = Depends(get_current_user_from_api_key)) -> dict:
    """Dependency to require admin user via API key"""
    if not Config.is_admin_user(api_key_info["auth_provider"], api_key_info["provider_username"]):
        raise HTTPException(status_code=403, detail="Admin access required")
    return api_key_info


async def require_admin_cookie(request: Request, user: dict = Depends(get_current_user_from_cookie)) -> dict:
    """Dependency to require admin user via cookie/JWT"""
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not Config.is_admin_user(user["provider"], user["username"]):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


@api_router.get("/admin/db/tables")
async def list_database_tables(request: Request, admin: dict = Depends(require_admin_cookie)):
    """List all tables in the database"""
    db = get_database()
    conn = sqlite3.connect(db.db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            SELECT name, sql FROM sqlite_master 
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
        """)
        tables = []
        for row in cursor.fetchall():
            # Get table info
            cursor.execute(f"PRAGMA table_info({row['name']})")
            columns = [{"name": col[1], "type": col[2], "nullable": not col[3], "pk": col[5]} 
                      for col in cursor.fetchall()]
            
            # Get row count
            cursor.execute(f"SELECT COUNT(*) as count FROM {row['name']}")
            row_count = cursor.fetchone()["count"]
            
            tables.append({
                "name": row["name"],
                "columns": columns,
                "row_count": row_count,
                "create_sql": row["sql"]
            })
        
        return {"tables": tables}
    finally:
        conn.close()


@api_router.get("/admin/db/tables/{table_name}")
async def get_table_data(
    table_name: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=1000),
    request: Request = None,
    admin: dict = Depends(require_admin_cookie)
):
    """Get paginated data from a specific table"""
    db = get_database()
    conn = sqlite3.connect(db.db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    try:
        # Validate table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Table not found")
        
        # Get total count
        cursor.execute(f"SELECT COUNT(*) as count FROM {table_name}")
        total_count = cursor.fetchone()["count"]
        
        # Get paginated data
        offset = (page - 1) * page_size
        cursor.execute(f"SELECT * FROM {table_name} LIMIT ? OFFSET ?", (page_size, offset))
        
        rows = []
        columns = []
        for row in cursor.fetchall():
            if not columns:
                columns = list(row.keys())
            rows.append(dict(row))
        
        # Convert datetime objects to ISO format strings
        for row in rows:
            for key, value in row.items():
                if isinstance(value, datetime):
                    row[key] = value.isoformat()
        
        return {
            "table_name": table_name,
            "columns": columns,
            "rows": rows,
            "total_count": total_count,
            "page": page,
            "page_size": page_size,
            "total_pages": (total_count + page_size - 1) // page_size
        }
    finally:
        conn.close()


@api_router.get("/admin/db/query")
async def execute_query(
    query: str = Query(..., description="SQL query to execute"),
    request: Request = None,
    admin: dict = Depends(require_admin_cookie)
):
    """Execute a custom SQL query (SELECT only for safety)"""
    # Only allow SELECT queries for safety
    if not query.strip().upper().startswith("SELECT"):
        raise HTTPException(status_code=400, detail="Only SELECT queries are allowed")
    
    db = get_database()
    conn = sqlite3.connect(db.db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    try:
        cursor.execute(query)
        rows = []
        columns = []
        for row in cursor.fetchall():
            if not columns:
                columns = list(row.keys())
            row_dict = dict(row)
            # Convert datetime objects to ISO format strings
            for key, value in row_dict.items():
                if isinstance(value, datetime):
                    row_dict[key] = value.isoformat()
            rows.append(row_dict)
        
        return {
            "query": query,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows)
        }
    except sqlite3.Error as e:
        raise HTTPException(status_code=400, detail=f"Query error: {str(e)}")
    finally:
        conn.close()


@api_router.delete("/admin/db/users/{user_id}")
async def delete_user(
    user_id: int,
    request: Request,
    admin: dict = Depends(require_admin_cookie)
):
    """Delete a user and all associated data"""
    db = get_database()
    conn = sqlite3.connect(db.db_path)
    cursor = conn.cursor()
    
    try:
        # Begin transaction
        cursor.execute("BEGIN TRANSACTION")
        
        # Check if user exists
        cursor.execute("SELECT provider_username, auth_provider FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        username, provider = user
        
        # Delete in correct order due to foreign key constraints
        # 1. Delete API keys
        cursor.execute("DELETE FROM api_keys WHERE user_id = ?", (user_id,))
        api_key_count = cursor.rowcount
        
        # 2. Delete tunnels
        cursor.execute("DELETE FROM tunnels WHERE user_id = ?", (user_id,))
        tunnel_count = cursor.rowcount
        
        # 3. Delete audit logs
        cursor.execute("DELETE FROM tunnel_audit_log WHERE user_id = ?", (user_id,))
        audit_count = cursor.rowcount
        
        # 4. Finally, delete the user
        cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
        
        # Commit transaction
        conn.commit()
        
        return {
            "success": True,
            "message": f"Deleted user {username} ({provider}) and all associated data",
            "deleted": {
                "api_keys": api_key_count,
                "tunnels": tunnel_count,
                "audit_logs": audit_count
            }
        }
        
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to delete user: {str(e)}")
    finally:
        conn.close()


@api_router.get("/admin/db/download")
async def download_database(request: Request, admin: dict = Depends(require_admin_cookie)):
    """Download the entire database file"""
    db = get_database()
    
    # Read the database file
    try:
        with open(db.db_path, 'rb') as f:
            db_content = f.read()
        
        # Return as downloadable file
        return StreamingResponse(
            io.BytesIO(db_content),
            media_type="application/x-sqlite3",
            headers={
                "Content-Disposition": f"attachment; filename=terratunnel_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read database: {str(e)}")


# Removed duplicate JWT-based admin endpoints - using cookie auth in the main endpoints above

