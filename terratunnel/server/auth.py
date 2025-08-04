import secrets
import json
import sqlite3
import logging
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends, Cookie
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
import jwt
import httpx

from .config import Config
from .database import Database

logger = logging.getLogger("terratunnel-server")

# Create auth router
auth_router = APIRouter(prefix="/auth", tags=["authentication"])

# Global database instance (set by app.py)
_db: Optional[Database] = None

def set_database(db: Database):
    """Set the global database instance"""
    global _db
    _db = db

def get_database() -> Database:
    """Get the global database instance"""
    if _db is None:
        raise RuntimeError("Database not initialized")
    return _db

# Database instance will be set by the app
_db = None

def set_database(db):
    """Set the database instance for OAuth state storage"""
    global _db
    _db = db

def store_state_data(state: str, provider: str = None, redirect_uri: str = None,
                    external_redirect_uri: str = None, external_state: str = None) -> bool:
    """Store OAuth state with additional data"""
    if _db:
        return _db.store_oauth_state(
            state, 
            provider=provider,
            redirect_uri=redirect_uri,
            external_redirect_uri=external_redirect_uri,
            external_state=external_state
        )
    return False

def get_state_data(state: str, delete: bool = True):
    """Get OAuth state data from database"""
    if _db:
        return _db.verify_oauth_state(state, delete=delete)
    return None


def generate_state(provider: str = None) -> str:
    """Generate a secure random state parameter for OAuth"""
    state = secrets.token_urlsafe(32)
    
    logger.info(f"Generated new OAuth state: {state} for provider: {provider}")
    
    # Store state in database if available, fallback to in-memory
    if _db:
        _db.store_oauth_state(state, provider=provider)
        # Also clean up old states
        _db.cleanup_oauth_states()
    else:
        logger.warning("Database not available, OAuth states will not persist across restarts")
    
    return state


def verify_state(state: str, delete: bool = True) -> bool:
    """Verify OAuth state parameter
    
    Args:
        state: The state parameter to verify
        delete: Whether to delete the state after verification (default: True)
    """
    
    # Use database if available
    if _db:
        state_data = _db.verify_oauth_state(state, delete=delete)
        return state_data is not None
    else:
        logger.warning("Database not available, OAuth state verification disabled")
        return False


@auth_router.get("/github")
async def github_oauth_redirect(request: Request, redirect_uri: Optional[str] = None):
    """Redirect to GitHub OAuth authorization page"""
    if not Config.has_github_oauth():
        raise HTTPException(status_code=503, detail="GitHub OAuth not configured")
    
    # Generate state for CSRF protection
    state = generate_state(provider="github")
    
    # Update state data with redirect URI if provided (state already stored in generate_state)
    if redirect_uri and _db:
        # Update the existing state record with redirect_uri
        conn = sqlite3.connect(_db.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("""
                UPDATE oauth_states 
                SET redirect_uri = ?
                WHERE state = ?
            """, (redirect_uri, state))
            conn.commit()
        except Exception as e:
            logger.warning(f"Failed to update OAuth state with redirect_uri: {e}")
        finally:
            conn.close()
    
    # Get the server's domain from request
    host = request.headers.get("host", "localhost")
    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    callback_uri = Config.get_github_oauth_redirect_uri(host, is_https)
    
    # Build GitHub OAuth URL
    github_oauth_url = "https://github.com/login/oauth/authorize"
    params = {
        "client_id": Config.GITHUB_CLIENT_ID,
        "redirect_uri": callback_uri,
        "scope": "read:user user:email",
        "state": state,
        "allow_signup": "true"
    }
    
    authorization_url = f"{github_oauth_url}?{urlencode(params)}"
    return RedirectResponse(url=authorization_url, status_code=302)


@auth_router.get("/github/authorize")
async def github_oauth_authorize_proxy(
    request: Request,
    redirect_uri: str,
    state: Optional[str] = None
):
    """OAuth proxy endpoint for third-party integrations like Terrateam setup wizard"""
    if not Config.has_github_oauth():
        raise HTTPException(status_code=503, detail="GitHub OAuth not configured")
    
    # Log the incoming request
    logger.info(f"OAuth proxy request: redirect_uri={redirect_uri}, external_state={state}")
    
    # Store the external redirect URI and state for later
    internal_state = generate_state(provider="github")
    logger.info(f"Generated internal state: {internal_state}")
    
    # Store both the external redirect_uri and external state
    store_state_data(
        internal_state, 
        provider="github",
        external_redirect_uri=redirect_uri,
        external_state=state
    )
    
    # Get the server's domain from request
    host = request.headers.get("host", "localhost")
    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    callback_uri = Config.get_github_oauth_redirect_uri(host, is_https)
    
    # Build GitHub OAuth URL with our internal callback
    github_oauth_url = "https://github.com/login/oauth/authorize"
    params = {
        "client_id": Config.GITHUB_CLIENT_ID,
        "redirect_uri": callback_uri,
        "scope": "read:user user:email",
        "state": internal_state,
        "allow_signup": "true"
    }
    
    from urllib.parse import urlencode
    authorization_url = f"{github_oauth_url}?{urlencode(params)}"
    return RedirectResponse(url=authorization_url, status_code=302)


@auth_router.get("/github/callback")
async def github_oauth_callback(
    request: Request,
    code: str = None,
    state: str = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None
):
    """Handle OAuth callback from GitHub"""
    # Log the callback
    logger.info(f"OAuth callback received: code={code[:10] if code else None}..., state={state}, error={error}")
    
    # Check for OAuth errors first
    if error:
        # Check if this is an external OAuth proxy request that had an error
        state_data = get_state_data(state, delete=False) if state else None
        external_redirect_uri = state_data.get('external_redirect_uri') if state_data else None
        if external_redirect_uri:
            # Get external state before cleaning up
            external_state_err = state_data.get('external_state') if state_data else None
            
            # Clean up state from database
            if state:
                get_state_data(state, delete=True)
            
            # Redirect back to external service with error
            from urllib.parse import urlencode
            error_params = {
                "error": error,
                "error_description": error_description or "OAuth authorization failed"
            }
            if external_state_err:
                error_params["state"] = external_state_err
            
            redirect_url = f"{external_redirect_uri}?{urlencode(error_params)}"
            return RedirectResponse(url=redirect_url, status_code=302)
        else:
            # Normal error handling - restart OAuth flow
            logger.warning(f"OAuth error received: {error}. Restarting flow.")
            return RedirectResponse(url="/auth/login", status_code=302)
    
    # Check if we have required parameters
    if not code or not state:
        logger.warning("OAuth callback missing code or state. Restarting flow.")
        return RedirectResponse(url="/auth/login", status_code=302)
    
    # Verify state parameter exists (don't delete yet, we need it for external redirect info)
    state_data = get_state_data(state, delete=False)
    if not state_data:
        # State is invalid/expired - restart OAuth flow seamlessly
        logger.warning(f"Invalid/expired state parameter: {state}. Restarting OAuth flow.")
        
        # Try to preserve the original intent
        # Check for referer or default to home
        redirect_after_auth = request.headers.get("referer")
        if redirect_after_auth and "github.com" not in redirect_after_auth:
            return RedirectResponse(
                url=f"/auth/github?redirect_uri={redirect_after_auth}",
                status_code=302
            )
        else:
            return RedirectResponse(url="/auth/github", status_code=302)
    
    # Exchange code for access token
    token_url = "https://github.com/login/oauth/access_token"
    
    # Get the callback URI that was used
    host = request.headers.get("host", "localhost")
    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    callback_uri = Config.get_github_oauth_redirect_uri(host, is_https)
    
    data = {
        "client_id": Config.GITHUB_CLIENT_ID,
        "client_secret": Config.GITHUB_CLIENT_SECRET,
        "code": code,
        "redirect_uri": callback_uri
    }
    
    headers = {
        "Accept": "application/json"
    }
    
    async with httpx.AsyncClient() as client:
        # Get access token
        token_response = await client.post(token_url, data=data, headers=headers)
        token_data = token_response.json()
        
        if "error" in token_data:
            raise HTTPException(
                status_code=400, 
                detail=f"Failed to get access token: {token_data.get('error_description', token_data['error'])}"
            )
        
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="No access token received")
        
        # Get user info from GitHub
        user_response = await client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json"
            }
        )
        
        if user_response.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to get user info from GitHub")
        
        github_user = user_response.json()
    
    # Create or update user in database
    db = get_database()
    user_id = db.create_or_update_user(
        auth_provider="github",
        provider_user_id=str(github_user["id"]),
        provider_username=github_user["login"],
        email=github_user.get("email"),
        name=github_user.get("name"),
        avatar_url=github_user.get("avatar_url")
    )
    
    # Create JWT token
    user = db.get_user_by_id(user_id)
    jwt_payload = {
        "sub": str(user_id),
        "provider": "github",
        "username": user["provider_username"],
        "exp": datetime.now(timezone.utc) + timedelta(hours=Config.JWT_EXPIRATION_HOURS),
        "iat": datetime.now(timezone.utc)
    }
    
    jwt_token = jwt.encode(jwt_payload, Config.JWT_SECRET, algorithm=Config.JWT_ALGORITHM)
    
    # Get state data from database and delete it (one-time use)
    # We already have state_data from above, just need to delete it now
    get_state_data(state, delete=True)
    
    # Check if there was a redirect URI stored (normal auth flow)
    redirect_uri = state_data.get('redirect_uri') if state_data else None
    
    # Check if this is an external OAuth proxy request
    external_redirect_uri = state_data.get('external_redirect_uri') if state_data else None
    external_state = state_data.get('external_state') if state_data else None
    
    if external_redirect_uri:
        # This is from the OAuth proxy - create tunnel and redirect back with all data
        try:
            # Check if user is admin
            is_admin = Config.is_admin_user("github", user["provider_username"])
            
            # Create API key for the user (this will also create a tunnel if needed)
            api_key = db.create_api_key(
                user_id=user_id,
                name=f"Terrateam Setup Wizard - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                is_admin=is_admin,
                provider="github",  # We know this is GitHub from the OAuth flow
                username=user["provider_username"]
            )
            
            # Get the actual tunnel that was created/used for this API key
            api_key_info = db.validate_api_key(api_key)
            if api_key_info and api_key_info.get("tunnel_subdomain"):
                tunnel_subdomain = api_key_info["tunnel_subdomain"]
            else:
                # Fallback - this shouldn't happen
                logger.error(f"Could not get tunnel subdomain for API key")
                raise Exception("Failed to get tunnel information")
            
            # Get the tunnel URL - extract domain from the Host header
            host = request.headers.get("host", "localhost")
            # Remove port if present
            domain = host.split(':')[0]
            tunnel_url = f"https://{tunnel_subdomain}.{domain}"
            
            # Log tunnel creation details
            logger.info(f"Created tunnel for user {github_user['login']}: {tunnel_url} (API key: {api_key[:8]}...)")
            
            # Build redirect URL with all required parameters
            from urllib.parse import urlencode
            params = {
                "access_token": access_token,  # GitHub access token
                "user_login": github_user["login"],
                "user_id": str(github_user["id"]),
                "tunnel_id": tunnel_subdomain,
                "tunnel_url": tunnel_url,
                "api_key": api_key
            }
            
            # Add the original state if it was provided
            if external_state:
                params["state"] = external_state
            
            redirect_url = f"{external_redirect_uri}?{urlencode(params)}"
            
            # Log the callback for debugging
            logger.info(f"OAuth proxy callback: redirecting to {redirect_url}")
            
            return RedirectResponse(url=redirect_url, status_code=302)
            
        except Exception as e:
            # Handle errors by redirecting back with error parameters
            from urllib.parse import urlencode
            error_params = {
                "error": "tunnel_creation_failed",
                "error_description": f"Failed to create tunnel: {str(e)}"
            }
            if external_state:
                error_params["state"] = external_state
            
            redirect_url = f"{external_redirect_uri}?{urlencode(error_params)}"
            return RedirectResponse(url=redirect_url, status_code=302)
    
    elif redirect_uri:
        # Normal browser-based auth - set cookie and redirect
        response = RedirectResponse(url=redirect_uri, status_code=302)
        response.set_cookie(
            key="auth_token",
            value=jwt_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=Config.JWT_EXPIRATION_HOURS * 3600
        )
        return response
    else:
        # No redirect URI - redirect to home page (dashboard)
        response = RedirectResponse(url="/", status_code=302)
        response.set_cookie(
            key="auth_token",
            value=jwt_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=Config.JWT_EXPIRATION_HOURS * 3600
        )
        return response


@auth_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, redirect_uri: Optional[str] = None):
    """Simple login page with OAuth provider buttons"""
    if not Config.has_any_oauth():
        return HTMLResponse("<h1>OAuth not configured</h1><p>No OAuth provider is configured on this server.</p>", status_code=503)
    
    # Build OAuth URLs
    oauth_buttons = []
    
    if Config.has_github_oauth():
        github_url = str(request.url_for("github_oauth_redirect"))
        if redirect_uri:
            github_url += f"?redirect_uri={redirect_uri}"
        oauth_buttons.append(f'''
            <a href="{github_url}" class="oauth-button github-button">
                <svg width="20" height="20" viewBox="0 0 16 16" fill="currentColor">
                    <path fill-rule="evenodd" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"></path>
                </svg>
                Sign in with GitHub
            </a>
        ''')
    
    if Config.has_gitlab_oauth():
        gitlab_url = str(request.url_for("gitlab_oauth_redirect"))
        if redirect_uri:
            gitlab_url += f"?redirect_uri={redirect_uri}"
        oauth_buttons.append(f'''
            <a href="{gitlab_url}" class="oauth-button gitlab-button">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M23.6 9.593l-.033-.086L20.3.98a.851.851 0 00-.336-.405.875.875 0 00-1.073.174.875.875 0 00-.2.395l-2.18 6.7H7.495l-2.18-6.7a.875.875 0 00-.2-.395.875.875 0 00-1.073-.174.851.851 0 00-.336.405L.437 9.506l-.032.086a6.066 6.066 0 002.27 7.202l.004.003.01.008 4.988 3.73 2.462 1.862 1.5 1.134a1.008 1.008 0 001.22 0l1.5-1.134 2.462-1.862 4.997-3.739.01-.008a6.068 6.068 0 002.263-7.196z"/>
                </svg>
                Sign in with GitLab
            </a>
        ''')
    
    buttons_html = '\n'.join(oauth_buttons)
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Terratunnel Login</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
                background: #f5f5f5;
            }}
            .login-container {{
                background: white;
                padding: 40px;
                border-radius: 8px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                text-align: center;
                max-width: 400px;
            }}
            h1 {{
                margin: 0 0 10px 0;
                color: #333;
            }}
            p {{
                color: #666;
                margin-bottom: 30px;
            }}
            .oauth-button {{
                display: inline-flex;
                align-items: center;
                padding: 12px 24px;
                color: white;
                text-decoration: none;
                border-radius: 6px;
                font-weight: 500;
                transition: background 0.2s;
                margin: 5px 0;
                width: 100%;
                justify-content: center;
                box-sizing: border-box;
            }}
            .github-button {{
                background: #24292e;
            }}
            .github-button:hover {{
                background: #1a1e22;
            }}
            .gitlab-button {{
                background: #FC6D26;
            }}
            .gitlab-button:hover {{
                background: #E24329;
            }}
            .oauth-button svg {{
                margin-right: 8px;
            }}
        </style>
    </head>
    <body>
        <div class="login-container">
            <h1>🚇 Terratunnel</h1>
            <p>Sign in to manage your tunnels</p>
            {buttons_html}
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@auth_router.get("/gitlab")
async def gitlab_oauth_redirect(request: Request, redirect_uri: Optional[str] = None):
    """Redirect to GitLab OAuth authorization page"""
    if not Config.has_gitlab_oauth():
        raise HTTPException(status_code=503, detail="GitLab OAuth not configured")
    
    # Generate state for CSRF protection
    state = generate_state(provider="gitlab")
    
    # Update state data with redirect URI if provided (state already stored in generate_state)
    if redirect_uri and _db:
        # Update the existing state record with redirect_uri
        conn = sqlite3.connect(_db.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("""
                UPDATE oauth_states 
                SET redirect_uri = ?
                WHERE state = ?
            """, (redirect_uri, state))
            conn.commit()
        except Exception as e:
            logger.warning(f"Failed to update OAuth state with redirect_uri: {e}")
        finally:
            conn.close()
    
    # Get the server's domain from request
    host = request.headers.get("host", "localhost")
    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    callback_uri = Config.get_gitlab_oauth_redirect_uri(host, is_https)
    
    # Build GitLab OAuth URL
    gitlab_oauth_url = "https://gitlab.com/oauth/authorize"
    params = {
        "client_id": Config.GITLAB_CLIENT_ID,
        "redirect_uri": callback_uri,
        "response_type": "code",
        "scope": "read_user",
        "state": state
    }
    
    authorization_url = f"{gitlab_oauth_url}?{urlencode(params)}"
    return RedirectResponse(url=authorization_url, status_code=302)


@auth_router.get("/gitlab/authorize")
async def gitlab_oauth_authorize_proxy(
    request: Request,
    redirect_uri: str,
    state: Optional[str] = None
):
    """OAuth proxy endpoint for third-party integrations"""
    if not Config.has_gitlab_oauth():
        raise HTTPException(status_code=503, detail="GitLab OAuth not configured")
    
    # Log the incoming request
    logger.info(f"GitLab OAuth proxy request: redirect_uri={redirect_uri}, external_state={state}")
    
    # Store the external redirect URI and state for later
    internal_state = generate_state(provider="gitlab")
    logger.info(f"Generated internal state: {internal_state}")
    
    # Store both the external redirect_uri and external state
    store_state_data(
        internal_state, 
        provider="gitlab",
        external_redirect_uri=redirect_uri,
        external_state=state
    )
    
    # Get the server's domain from request
    host = request.headers.get("host", "localhost")
    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    callback_uri = Config.get_gitlab_oauth_redirect_uri(host, is_https)
    
    # Build GitLab OAuth URL with our internal callback
    gitlab_oauth_url = "https://gitlab.com/oauth/authorize"
    params = {
        "client_id": Config.GITLAB_CLIENT_ID,
        "redirect_uri": callback_uri,
        "response_type": "code",
        "scope": "read_user",
        "state": internal_state
    }
    
    authorization_url = f"{gitlab_oauth_url}?{urlencode(params)}"
    return RedirectResponse(url=authorization_url, status_code=302)


@auth_router.get("/gitlab/callback")
async def gitlab_oauth_callback(
    request: Request,
    code: str = None,
    state: str = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None
):
    """Handle OAuth callback from GitLab"""
    # Log the callback
    logger.info(f"GitLab OAuth callback received: code={code[:10] if code else None}..., state={state}, error={error}")
    
    # Check for OAuth errors first
    if error:
        # Check if this is an external OAuth proxy request that had an error
        state_data = get_state_data(state, delete=False) if state else None
        external_redirect_uri = state_data.get('external_redirect_uri') if state_data else None
        if external_redirect_uri:
            # Get external state before cleaning up
            external_state_err = state_data.get('external_state') if state_data else None
            
            # Clean up state from database
            if state:
                get_state_data(state, delete=True)
            
            # Redirect back to external service with error
            error_params = {
                "error": error,
                "error_description": error_description or "OAuth authorization failed"
            }
            if external_state_err:
                error_params["state"] = external_state_err
            
            redirect_url = f"{external_redirect_uri}?{urlencode(error_params)}"
            return RedirectResponse(url=redirect_url, status_code=302)
        else:
            # Normal error handling - restart OAuth flow
            logger.warning(f"OAuth error received: {error}. Restarting flow.")
            return RedirectResponse(url="/auth/login", status_code=302)
    
    # Check if we have required parameters
    if not code or not state:
        logger.warning("OAuth callback missing code or state. Restarting flow.")
        return RedirectResponse(url="/auth/login", status_code=302)
    
    # Verify state parameter exists (don't delete yet, we need it for external redirect info)
    state_data = get_state_data(state, delete=False)
    if not state_data:
        # State is invalid/expired - restart OAuth flow seamlessly
        logger.warning(f"Invalid/expired state parameter: {state}. Restarting OAuth flow.")
        
        # Try to preserve the original intent
        # Check for referer or default to home
        redirect_after_auth = request.headers.get("referer")
        if redirect_after_auth and "gitlab.com" not in redirect_after_auth:
            return RedirectResponse(
                url=f"/auth/gitlab?redirect_uri={redirect_after_auth}",
                status_code=302
            )
        else:
            return RedirectResponse(url="/auth/gitlab", status_code=302)
    
    # Exchange code for access token
    token_url = "https://gitlab.com/oauth/token"
    
    # Get the callback URI that was used
    host = request.headers.get("host", "localhost")
    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    callback_uri = Config.get_gitlab_oauth_redirect_uri(host, is_https)
    
    data = {
        "client_id": Config.GITLAB_CLIENT_ID,
        "client_secret": Config.GITLAB_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": callback_uri
    }
    
    headers = {
        "Accept": "application/json"
    }
    
    async with httpx.AsyncClient() as client:
        # Get access token
        token_response = await client.post(token_url, data=data, headers=headers)
        token_data = token_response.json()
        
        if "error" in token_data:
            raise HTTPException(
                status_code=400, 
                detail=f"Failed to get access token: {token_data.get('error_description', token_data['error'])}"
            )
        
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="No access token received")
        
        # Get user info from GitLab
        user_response = await client.get(
            "https://gitlab.com/api/v4/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json"
            }
        )
        
        if user_response.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to get user info from GitLab")
        
        gitlab_user = user_response.json()
    
    # Create or update user in database
    db = get_database()
    user_id = db.create_or_update_user(
        auth_provider="gitlab",
        provider_user_id=str(gitlab_user["id"]),
        provider_username=gitlab_user["username"],
        email=gitlab_user.get("email"),
        name=gitlab_user.get("name"),
        avatar_url=gitlab_user.get("avatar_url")
    )
    
    # Create JWT token
    user = db.get_user_by_id(user_id)
    jwt_payload = {
        "sub": str(user_id),
        "provider": "gitlab",
        "username": user["provider_username"],
        "exp": datetime.now(timezone.utc) + timedelta(hours=Config.JWT_EXPIRATION_HOURS),
        "iat": datetime.now(timezone.utc)
    }
    
    jwt_token = jwt.encode(jwt_payload, Config.JWT_SECRET, algorithm=Config.JWT_ALGORITHM)
    
    # Get state data from database and delete it (one-time use)
    # We already have state_data from above, just need to delete it now
    get_state_data(state, delete=True)
    
    # Check if there was a redirect URI stored (normal auth flow)
    redirect_uri = state_data.get('redirect_uri') if state_data else None
    
    # Check if this is an external OAuth proxy request
    external_redirect_uri = state_data.get('external_redirect_uri') if state_data else None
    external_state = state_data.get('external_state') if state_data else None
    
    if external_redirect_uri:
        # This is from the OAuth proxy - create tunnel and redirect back with all data
        try:
            # Check if user is admin
            is_admin = Config.is_admin_user("gitlab", user["provider_username"])
            
            # Create API key for the user (this will also create a tunnel if needed)
            api_key = db.create_api_key(
                user_id=user_id,
                name=f"Terrateam Setup Wizard - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                is_admin=is_admin,
                provider="gitlab",
                username=user["provider_username"]
            )
            
            # Get the actual tunnel that was created/used for this API key
            api_key_info = db.validate_api_key(api_key)
            if api_key_info and api_key_info.get("tunnel_subdomain"):
                tunnel_subdomain = api_key_info["tunnel_subdomain"]
            else:
                # Fallback - this shouldn't happen
                logger.error(f"Could not get tunnel subdomain for API key")
                raise Exception("Failed to get tunnel information")
            
            # Get the tunnel URL - extract domain from the Host header
            host = request.headers.get("host", "localhost")
            # Remove port if present
            domain = host.split(':')[0]
            tunnel_url = f"https://{tunnel_subdomain}.{domain}"
            
            # Log tunnel creation details
            logger.info(f"Created tunnel for user {gitlab_user['username']}: {tunnel_url} (API key: {api_key[:8]}...)")
            
            # Build redirect URL with all required parameters
            params = {
                "access_token": access_token,  # GitLab access token
                "user_login": gitlab_user["username"],
                "user_id": str(gitlab_user["id"]),
                "tunnel_id": tunnel_subdomain,
                "tunnel_url": tunnel_url,
                "api_key": api_key
            }
            
            # Add the original state if it was provided
            if external_state:
                params["state"] = external_state
            
            redirect_url = f"{external_redirect_uri}?{urlencode(params)}"
            
            # Log the callback for debugging
            logger.info(f"GitLab OAuth proxy callback: redirecting to {redirect_url}")
            
            return RedirectResponse(url=redirect_url, status_code=302)
            
        except Exception as e:
            # Handle errors by redirecting back with error parameters
            error_params = {
                "error": "tunnel_creation_failed",
                "error_description": f"Failed to create tunnel: {str(e)}"
            }
            if external_state:
                error_params["state"] = external_state
            
            redirect_url = f"{external_redirect_uri}?{urlencode(error_params)}"
            return RedirectResponse(url=redirect_url, status_code=302)
    
    elif redirect_uri:
        # Normal browser-based auth - set cookie and redirect
        response = RedirectResponse(url=redirect_uri, status_code=302)
        response.set_cookie(
            key="auth_token",
            value=jwt_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=Config.JWT_EXPIRATION_HOURS * 3600
        )
        return response
    else:
        # No redirect URI - redirect to home page (dashboard)
        response = RedirectResponse(url="/", status_code=302)
        response.set_cookie(
            key="auth_token",
            value=jwt_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=Config.JWT_EXPIRATION_HOURS * 3600
        )
        return response


@auth_router.get("/logout")
async def logout(redirect_uri: Optional[str] = None):
    """Logout by clearing the auth cookie"""
    response = RedirectResponse(
        url=redirect_uri or "/",
        status_code=302
    )
    response.delete_cookie("auth_token")
    return response


@auth_router.get("/me")
async def get_current_user(request: Request):
    """Get current user info from JWT token"""
    # Get token from Authorization header
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")
    
    token = auth_header[7:]  # Remove "Bearer " prefix
    
    try:
        # Decode and verify JWT
        payload = jwt.decode(token, Config.JWT_SECRET, algorithms=[Config.JWT_ALGORITHM])
        user_id = int(payload["sub"])
        
        # Get user from database
        db = get_database()
        user = db.get_user_by_id(user_id)
        
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        
        return {
            "id": user["id"],
            "username": user["provider_username"],
            "email": user["email"],
            "avatar_url": user["avatar_url"],
            "provider": user["auth_provider"]
        }
        
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user_from_cookie(request: Request, auth_token: Optional[str] = Cookie(None)):
    """Get current user from JWT cookie (for web pages)"""
    if not auth_token:
        return None
    
    try:
        # Decode and verify JWT
        payload = jwt.decode(auth_token, Config.JWT_SECRET, algorithms=[Config.JWT_ALGORITHM])
        user_id = int(payload["sub"])
        
        # Get user from database
        db = get_database()
        user = db.get_user_by_id(user_id)
        
        if not user:
            return None
        
        return {
            "id": user["id"],
            "username": user["provider_username"],
            "email": user["email"],
            "provider": user["auth_provider"]
        }
        
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, KeyError, ValueError):
        return None


async def require_admin_user(request: Request, auth_token: Optional[str] = Cookie(None)):
    """Require the current user to be an admin"""
    user = await get_current_user_from_cookie(request, auth_token)
    
    if not user:
        # Redirect to login page
        login_url = str(request.url_for("login_page"))
        # Build redirect URL with proper scheme
        is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        host = request.headers.get("host", "localhost")
        path = request.url.path
        query = f"?{request.url.query}" if request.url.query else ""
        redirect_url = f"{'https' if is_https else 'http'}://{host}{path}{query}"
        return RedirectResponse(url=f"{login_url}?redirect_uri={redirect_url}", status_code=302)
    
    # Check if user is admin
    if not Config.is_admin_user(user["provider"], user["username"]):
        # Return an HTML response for browser requests
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Access Denied - Terratunnel</title>
            <style>
                body {
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    margin: 0;
                    padding: 20px;
                    background: #f5f5f5;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    min-height: 100vh;
                }
                .container {
                    max-width: 500px;
                    background: white;
                    padding: 40px;
                    border-radius: 8px;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                    text-align: center;
                }
                h1 {
                    color: #d73a49;
                    margin-top: 0;
                }
                p {
                    color: #666;
                    line-height: 1.6;
                    margin: 20px 0;
                }
                .user-info {
                    background: #f8f9fa;
                    padding: 15px;
                    border-radius: 6px;
                    margin: 20px 0;
                    font-family: monospace;
                    font-size: 0.9em;
                }
                a {
                    color: #0066cc;
                    text-decoration: none;
                }
                a:hover {
                    text-decoration: underline;
                }
                .button {
                    display: inline-block;
                    padding: 10px 20px;
                    background: #0066cc;
                    color: white;
                    border-radius: 6px;
                    margin-top: 20px;
                }
                .button:hover {
                    background: #0052a3;
                    text-decoration: none;
                }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>🚫 Access Denied</h1>
                <p>You must be an administrator to access this page.</p>
                <div class="user-info">
                    Logged in as: <strong>""" + user["username"] + """ (""" + user["provider"] + """)</strong>
                </div>
                <p>If you believe you should have access, please contact your administrator.</p>
                <a href="/" class="button">Go Back</a>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=html, status_code=403)
    
    return user