import os
from typing import Optional

class Config:
    """Server configuration from environment variables"""
    
    # OAuth Configuration
    GITHUB_CLIENT_ID: Optional[str] = os.getenv("GITHUB_CLIENT_ID")
    GITHUB_CLIENT_SECRET: Optional[str] = os.getenv("GITHUB_CLIENT_SECRET")
    
    GITLAB_CLIENT_ID: Optional[str] = os.getenv("GITLAB_CLIENT_ID")
    GITLAB_CLIENT_SECRET: Optional[str] = os.getenv("GITLAB_CLIENT_SECRET")
    
    # JWT Configuration
    JWT_SECRET: str = os.getenv("JWT_SECRET", "change-me-in-production")
    JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")
    JWT_EXPIRATION_HOURS: int = int(os.getenv("JWT_EXPIRATION_HOURS", "24"))
    
    # Authentication Configuration
    REQUIRE_AUTH_FOR_TUNNELS: bool = os.getenv("REQUIRE_AUTH_FOR_TUNNELS", "true").lower() == "true"
    
    # Admin Users Configuration (comma-separated list of usernames per provider)
    GITHUB_ADMIN_USERS: list[str] = [u.strip() for u in os.getenv("TERRATUNNEL_GITHUB_ADMIN_USERS", "").split(",") if u.strip()]
    GITLAB_ADMIN_USERS: list[str] = [u.strip() for u in os.getenv("TERRATUNNEL_GITLAB_ADMIN_USERS", "").split(",") if u.strip()]
    GOOGLE_ADMIN_USERS: list[str] = [u.strip() for u in os.getenv("TERRATUNNEL_GOOGLE_ADMIN_USERS", "").split(",") if u.strip()]
    
    # OAuth Redirect URLs (to be set based on server domain)
    GITHUB_OAUTH_REDIRECT_PATH: str = "/auth/github/callback"
    GITLAB_OAUTH_REDIRECT_PATH: str = "/auth/gitlab/callback"
    
    @classmethod
    def validate(cls):
        """Validate required configuration"""
        import logging
        logger = logging.getLogger("terratunnel-server")
        
        if cls.JWT_SECRET == "change-me-in-production":
            logger.warning("     JWT_SECRET not set - using insecure default. Set JWT_SECRET environment variable!")
        
        if cls.REQUIRE_AUTH_FOR_TUNNELS and not cls.has_any_oauth():
            logger.warning("     REQUIRE_AUTH_FOR_TUNNELS is enabled but no OAuth provider is configured!")
    
    @classmethod
    def has_github_oauth(cls) -> bool:
        """Check if GitHub OAuth is configured"""
        return bool(cls.GITHUB_CLIENT_ID and cls.GITHUB_CLIENT_SECRET)
    
    @classmethod
    def has_gitlab_oauth(cls) -> bool:
        """Check if GitLab OAuth is configured"""
        return bool(cls.GITLAB_CLIENT_ID and cls.GITLAB_CLIENT_SECRET)
    
    @classmethod
    def has_any_oauth(cls) -> bool:
        """Check if any OAuth provider is configured"""
        return cls.has_github_oauth() or cls.has_gitlab_oauth()
    
    @classmethod
    def get_github_oauth_redirect_uri(cls, domain: str, use_https: bool = True) -> str:
        """Get GitHub OAuth redirect URI for the given domain"""
        protocol = "https" if use_https else "http"
        return f"{protocol}://{domain}{cls.GITHUB_OAUTH_REDIRECT_PATH}"
    
    @classmethod
    def get_gitlab_oauth_redirect_uri(cls, domain: str, use_https: bool = True) -> str:
        """Get GitLab OAuth redirect URI for the given domain"""
        protocol = "https" if use_https else "http"
        return f"{protocol}://{domain}{cls.GITLAB_OAUTH_REDIRECT_PATH}"
    
    @classmethod
    def is_admin_user(cls, provider: str, username: str) -> bool:
        """Check if a user is an admin for the given provider"""
        if not provider or not username:
            return False
        
        admin_users = []
        if provider.lower() == "github":
            admin_users = cls.GITHUB_ADMIN_USERS
        elif provider.lower() == "gitlab":
            admin_users = cls.GITLAB_ADMIN_USERS
        elif provider.lower() == "google":
            admin_users = cls.GOOGLE_ADMIN_USERS
        
        return username in admin_users
    
    @classmethod
    def get_all_admin_users(cls) -> dict[str, list[str]]:
        """Get all configured admin users by provider"""
        return {
            "github": cls.GITHUB_ADMIN_USERS,
            "gitlab": cls.GITLAB_ADMIN_USERS,
            "google": cls.GOOGLE_ADMIN_USERS
        }