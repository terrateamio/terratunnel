import sqlite3
import logging
from datetime import datetime
from typing import Optional, Dict, List
import os
import hashlib
import secrets

logger = logging.getLogger("terratunnel-server")

# Supported OAuth providers
AUTH_PROVIDERS = {
    'github': 'GitHub',
    'gitlab': 'GitLab',
    'google': 'Google',
    'bitbucket': 'Bitbucket'
}


class Database:
    def __init__(self, db_path: str = "terratunnel.db"):
        self.db_path = db_path
        self.init_database()
    
    def init_database(self):
        """Initialize database and create tables if they don't exist"""
        # Ensure directory exists
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Check if we need to migrate the tunnel_audit_log table
        cursor.execute("PRAGMA table_info(tunnel_audit_log)")
        columns = cursor.fetchall()
        column_names = [col[1] for col in columns]
        
        # Add username column if it doesn't exist
        if columns and 'username' not in column_names:
            logger.info("     Migrating tunnel_audit_log table: adding username column")
            cursor.execute("ALTER TABLE tunnel_audit_log ADD COLUMN username TEXT")
            conn.commit()
        
        # Create users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                auth_provider TEXT NOT NULL,
                provider_user_id TEXT NOT NULL,
                provider_username TEXT NOT NULL,
                email TEXT,
                name TEXT,
                avatar_url TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                UNIQUE(auth_provider, provider_user_id),
                UNIQUE(auth_provider, provider_username)
            )
        """)
        
        # Create api_keys table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                key_hash TEXT UNIQUE NOT NULL,
                key_prefix TEXT NOT NULL,
                name TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at DATETIME,
                expires_at DATETIME,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                scopes TEXT DEFAULT 'tunnel:create',
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        
        # Create tunnel_audit_log table with user_id
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tunnel_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                subdomain TEXT NOT NULL,
                hostname TEXT NOT NULL,
                local_endpoint TEXT NOT NULL,
                client_ip TEXT,
                user_agent TEXT,
                connection_id TEXT NOT NULL,
                event_type TEXT NOT NULL DEFAULT 'connect',
                user_id INTEGER,
                username TEXT,
                api_key_id INTEGER,
                additional_data TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
                FOREIGN KEY (api_key_id) REFERENCES api_keys(id) ON DELETE SET NULL
            )
        """)
        
        # Create indexes
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_provider 
            ON users(auth_provider, provider_user_id)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_username 
            ON users(auth_provider, provider_username)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_api_keys_key_hash 
            ON api_keys(key_hash)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_api_keys_user_id 
            ON api_keys(user_id)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tunnel_audit_log_timestamp 
            ON tunnel_audit_log(timestamp DESC)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tunnel_audit_log_subdomain 
            ON tunnel_audit_log(subdomain)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tunnel_audit_log_user_id 
            ON tunnel_audit_log(user_id)
        """)
        
        conn.commit()
        conn.close()
        
        logger.info(f"     Database initialized at {self.db_path}")
    
    def log_connection(self, subdomain: str, hostname: str, local_endpoint: str, 
                      client_ip: Optional[str] = None, user_agent: Optional[str] = None,
                      connection_id: Optional[str] = None, user_id: Optional[int] = None,
                      username: Optional[str] = None, api_key_id: Optional[int] = None, 
                      additional_data: Optional[str] = None):
        """Log a new tunnel connection"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                INSERT INTO tunnel_audit_log 
                (subdomain, hostname, local_endpoint, client_ip, user_agent, connection_id, 
                 event_type, user_id, username, api_key_id, additional_data)
                VALUES (?, ?, ?, ?, ?, ?, 'connect', ?, ?, ?, ?)
            """, (subdomain, hostname, local_endpoint, client_ip, user_agent, 
                  connection_id or subdomain, user_id, username, api_key_id, additional_data))
            
            conn.commit()
            if username:
                logger.info(f"     Logged connection: {hostname} -> {local_endpoint} (user: {username}, tunnel_id: {subdomain})")
            else:
                logger.info(f"     Logged connection: {hostname} -> {local_endpoint} (anonymous, tunnel_id: {subdomain})")
        except Exception as e:
            logger.error(f"     Failed to log connection: {e}")
        finally:
            conn.close()
    
    def log_disconnection(self, subdomain: str, connection_id: Optional[str] = None):
        """Log a tunnel disconnection"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                INSERT INTO tunnel_audit_log 
                (subdomain, hostname, local_endpoint, connection_id, event_type)
                VALUES (?, '', '', ?, 'disconnect')
            """, (subdomain, connection_id or subdomain))
            
            conn.commit()
            logger.info(f"     Logged disconnection: {subdomain}")
        except Exception as e:
            logger.error(f"     Failed to log disconnection: {e}")
        finally:
            conn.close()
    
    def get_recent_connections(self, limit: int = 100):
        """Get recent connections from the audit log"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                SELECT * FROM tunnel_audit_log 
                WHERE event_type = 'connect'
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (limit,))
            
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()
    
    def get_connection_history(self, subdomain: str):
        """Get connection history for a specific subdomain"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                SELECT * FROM tunnel_audit_log 
                WHERE subdomain = ?
                ORDER BY timestamp DESC
            """, (subdomain,))
            
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()
    
    # User management methods
    def create_or_update_user(self, auth_provider: str, provider_user_id: str, 
                             provider_username: str, email: Optional[str] = None, 
                             name: Optional[str] = None, avatar_url: Optional[str] = None) -> int:
        """Create or update a user based on OAuth provider data"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            # Check if user exists
            cursor.execute(
                "SELECT id FROM users WHERE auth_provider = ? AND provider_user_id = ?", 
                (auth_provider, provider_user_id)
            )
            existing = cursor.fetchone()
            
            if existing:
                # Update existing user
                cursor.execute("""
                    UPDATE users 
                    SET provider_username = ?, email = ?, name = ?, avatar_url = ?, 
                        updated_at = CURRENT_TIMESTAMP
                    WHERE auth_provider = ? AND provider_user_id = ?
                """, (provider_username, email, name, avatar_url, auth_provider, provider_user_id))
                user_id = existing[0]
                logger.info(f"     Updated user: {provider_username} [{auth_provider}] (ID: {user_id})")
            else:
                # Create new user
                cursor.execute("""
                    INSERT INTO users (auth_provider, provider_user_id, provider_username, 
                                     email, name, avatar_url)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (auth_provider, provider_user_id, provider_username, email, name, avatar_url))
                user_id = cursor.lastrowid
                logger.info(f"     Created new user: {provider_username} [{auth_provider}] (ID: {user_id})")
            
            conn.commit()
            return user_id
        except Exception as e:
            logger.error(f"     Failed to create/update user: {e}")
            raise
        finally:
            conn.close()
    
    def get_user_by_provider_id(self, auth_provider: str, provider_user_id: str) -> Optional[Dict]:
        """Get user by provider and provider user ID"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                "SELECT * FROM users WHERE auth_provider = ? AND provider_user_id = ? AND is_active = 1", 
                (auth_provider, provider_user_id)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    
    def get_user_by_provider_username(self, auth_provider: str, provider_username: str) -> Optional[Dict]:
        """Get user by provider and username"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                "SELECT * FROM users WHERE auth_provider = ? AND provider_username = ? AND is_active = 1", 
                (auth_provider, provider_username)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    
    def get_user_by_id(self, user_id: int) -> Optional[Dict]:
        """Get user by ID"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        try:
            cursor.execute("SELECT * FROM users WHERE id = ? AND is_active = 1", (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    
    # API key management methods
    def create_api_key(self, user_id: int, name: Optional[str] = None, 
                      expires_at: Optional[datetime] = None, scopes: str = "tunnel:create") -> str:
        """Create a new API key for a user and return the raw key"""
        # Generate a secure random API key
        raw_key = secrets.token_urlsafe(32)
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        key_prefix = raw_key[:8]  # Store prefix for identification
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                INSERT INTO api_keys (user_id, key_hash, key_prefix, name, expires_at, scopes)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, key_hash, key_prefix, name, expires_at, scopes))
            
            conn.commit()
            logger.info(f"     Created API key for user {user_id}: {key_prefix}...")
            return raw_key
        except Exception as e:
            logger.error(f"     Failed to create API key: {e}")
            raise
        finally:
            conn.close()
    
    def validate_api_key(self, raw_key: str) -> Optional[Dict]:
        """Validate an API key and return user info if valid"""
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        try:
            # Get API key and user info
            cursor.execute("""
                SELECT k.*, u.id as user_id, u.provider_username, u.email, u.auth_provider
                FROM api_keys k
                JOIN users u ON k.user_id = u.id
                WHERE k.key_hash = ? AND k.is_active = 1 AND u.is_active = 1
                AND (k.expires_at IS NULL OR k.expires_at > CURRENT_TIMESTAMP)
            """, (key_hash,))
            
            row = cursor.fetchone()
            if row:
                # Update last_used_at
                cursor.execute("""
                    UPDATE api_keys SET last_used_at = CURRENT_TIMESTAMP 
                    WHERE key_hash = ?
                """, (key_hash,))
                conn.commit()
                
                return dict(row)
            return None
        finally:
            conn.close()
    
    def list_user_api_keys(self, user_id: int) -> List[Dict]:
        """List all API keys for a user (without exposing the actual keys)"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                SELECT id, key_prefix, name, created_at, last_used_at, 
                       expires_at, is_active, scopes
                FROM api_keys 
                WHERE user_id = ?
                ORDER BY created_at DESC
            """, (user_id,))
            
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()
    
    def revoke_api_key(self, user_id: int, key_id: int) -> bool:
        """Revoke an API key"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                UPDATE api_keys 
                SET is_active = 0 
                WHERE id = ? AND user_id = ?
            """, (key_id, user_id))
            
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"     Failed to revoke API key: {e}")
            return False
        finally:
            conn.close()