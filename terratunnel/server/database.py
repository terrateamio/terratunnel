import sqlite3
import logging
from datetime import datetime
from typing import Optional, Dict, List
import os
import hashlib
import secrets

logger = logging.getLogger("terratunnel-server")

import string
import random
from coolname import generate_slug

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
        
        # Check if users table exists and needs tunnel_subdomain column
        cursor.execute("PRAGMA table_info(users)")
        user_columns = cursor.fetchall()
        user_column_names = [col[1] for col in user_columns]
        
        # Add tunnel_subdomain column if it doesn't exist
        if user_columns and 'tunnel_subdomain' not in user_column_names:
            logger.info("     Migrating users table: adding tunnel_subdomain column")
            cursor.execute("ALTER TABLE users ADD COLUMN tunnel_subdomain TEXT UNIQUE")
            conn.commit()
        
        # Check if api_keys table exists and needs tunnel_id column
        cursor.execute("PRAGMA table_info(api_keys)")
        api_key_columns = cursor.fetchall()
        api_key_column_names = [col[1] for col in api_key_columns]
        
        if api_key_columns and 'tunnel_id' not in api_key_column_names:
            logger.info("     Migrating api_keys table: adding tunnel_id column")
            cursor.execute("ALTER TABLE api_keys ADD COLUMN tunnel_id INTEGER REFERENCES tunnels(id)")
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
                tunnel_subdomain TEXT UNIQUE,
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
        
        # Create tunnels table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tunnels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                subdomain TEXT UNIQUE NOT NULL,
                name TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        
        # Create oauth_states table for persistent state storage
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS oauth_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                state TEXT UNIQUE NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                redirect_uri TEXT,
                external_redirect_uri TEXT,
                external_state TEXT,
                provider TEXT,
                additional_data TEXT
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
        
        # Migrate existing tunnel_subdomain data to tunnels table
        cursor.execute("""
            SELECT COUNT(*) FROM tunnels
        """)
        tunnel_count = cursor.fetchone()[0]
        
        if tunnel_count == 0:
            # Check if we have users with tunnel_subdomain to migrate
            cursor.execute("""
                SELECT id, tunnel_subdomain, provider_username 
                FROM users 
                WHERE tunnel_subdomain IS NOT NULL
            """)
            users_to_migrate = cursor.fetchall()
            
            if users_to_migrate:
                logger.info(f"     Migrating {len(users_to_migrate)} existing user tunnels to tunnels table")
                for user_id, subdomain, username in users_to_migrate:
                    # Create tunnel for this user
                    cursor.execute("""
                        INSERT INTO tunnels (user_id, subdomain, name)
                        VALUES (?, ?, ?)
                    """, (user_id, subdomain, f"Default tunnel"))
                    
                    tunnel_id = cursor.lastrowid
                    
                    # Update all API keys for this user to point to this tunnel
                    cursor.execute("""
                        UPDATE api_keys 
                        SET tunnel_id = ?
                        WHERE user_id = ? AND tunnel_id IS NULL
                    """, (tunnel_id, user_id))
                    
                    logger.info(f"     Migrated tunnel for user {username}: {subdomain}")
        
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
                # Create new user with a static subdomain
                # Generate a unique subdomain for this user
                tunnel_subdomain = self._generate_unique_subdomain(cursor)
                
                cursor.execute("""
                    INSERT INTO users (auth_provider, provider_user_id, provider_username, 
                                     email, name, avatar_url, tunnel_subdomain)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (auth_provider, provider_user_id, provider_username, email, name, avatar_url, tunnel_subdomain))
                user_id = cursor.lastrowid
                logger.info(f"     Created new user: {provider_username} [{auth_provider}] (ID: {user_id}) with subdomain: {tunnel_subdomain}")
                
                # Also create an entry in the tunnels table for new users
                # We need to check if they're admin to ensure proper handling
                from .config import Config
                is_admin = Config.is_admin_user(auth_provider, provider_username)
                
                # Check if this is a non-admin user who somehow already has a tunnel (shouldn't happen for new users)
                if not is_admin:
                    cursor.execute("""
                        SELECT COUNT(*) FROM tunnels 
                        WHERE user_id = ? AND is_active = 1
                    """, (user_id,))
                    existing_tunnel_count = cursor.fetchone()[0]
                    
                    if existing_tunnel_count > 0:
                        logger.warning(f"     New user {provider_username} already has {existing_tunnel_count} tunnels - skipping tunnel creation")
                        conn.commit()
                        return user_id
                
                # Create the tunnel entry
                cursor.execute("""
                    INSERT INTO tunnels (user_id, subdomain, name)
                    VALUES (?, ?, ?)
                """, (user_id, tunnel_subdomain, f"Default tunnel"))
                logger.info(f"     Created tunnel entry for new user {provider_username} (admin: {is_admin})")
            
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
    
    # Tunnel management methods
    def create_tunnel(self, user_id: int, provider: Optional[str] = None, username: Optional[str] = None) -> Dict:
        """Create a new tunnel for a user
        
        Args:
            user_id: The user's ID
            provider: Auth provider (github, gitlab, google) - needed to check admin status
            username: Username - needed to check admin status
            
        For non-admin users, this enforces a single tunnel limit.
        Tunnel names are auto-generated and cannot be customized by users.
        """
        conn = sqlite3.connect(self.db_path)
        # Enable WAL mode for better concurrency
        conn.execute("PRAGMA journal_mode=WAL")
        # Use EXCLUSIVE transaction to prevent race conditions
        conn.isolation_level = 'EXCLUSIVE'
        cursor = conn.cursor()
        
        try:
            # Begin exclusive transaction
            cursor.execute("BEGIN EXCLUSIVE")
            
            # Check if user is admin (if provider and username are provided)
            is_admin = False
            if provider and username:
                from .config import Config
                is_admin = Config.is_admin_user(provider, username)
            
            # For non-admin users, check if they already have a tunnel
            if not is_admin:
                cursor.execute("""
                    SELECT COUNT(*) FROM tunnels 
                    WHERE user_id = ? AND is_active = 1
                """, (user_id,))
                tunnel_count = cursor.fetchone()[0]
                
                if tunnel_count > 0:
                    # Non-admin user already has a tunnel, return the existing one
                    cursor.execute("""
                        SELECT id, subdomain, name 
                        FROM tunnels 
                        WHERE user_id = ? AND is_active = 1
                        ORDER BY created_at ASC
                        LIMIT 1
                    """, (user_id,))
                    existing_tunnel = cursor.fetchone()
                    
                    logger.info(f"     Non-admin user {user_id} already has a tunnel, returning existing tunnel {existing_tunnel[0]}")
                    
                    return {
                        "id": existing_tunnel[0],
                        "subdomain": existing_tunnel[1],
                        "name": existing_tunnel[2]
                    }
            
            # Generate unique subdomain
            subdomain = self._generate_unique_subdomain(cursor)
            
            # Auto-generate name based on subdomain
            tunnel_name = f"Tunnel {subdomain}"
            
            cursor.execute("""
                INSERT INTO tunnels (user_id, subdomain, name)
                VALUES (?, ?, ?)
            """, (user_id, subdomain, tunnel_name))
            
            tunnel_id = cursor.lastrowid
            conn.commit()
            
            logger.info(f"     Created tunnel {tunnel_id} for user {user_id}: {subdomain} (admin: {is_admin})")
            
            return {
                "id": tunnel_id,
                "subdomain": subdomain,
                "name": tunnel_name
            }
        except Exception as e:
            conn.rollback()
            logger.error(f"     Failed to create tunnel: {e}")
            raise
        finally:
            conn.close()
    
    def list_user_tunnels(self, user_id: int) -> List[Dict]:
        """List all tunnels for a user"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                SELECT t.*, 
                       (SELECT COUNT(*) FROM api_keys WHERE tunnel_id = t.id AND is_active = 1) as active_keys
                FROM tunnels t
                WHERE t.user_id = ? AND t.is_active = 1
                ORDER BY t.created_at DESC
            """, (user_id,))
            
            tunnels = []
            for row in cursor.fetchall():
                tunnels.append(dict(row))
            
            return tunnels
        finally:
            conn.close()
    
    def get_tunnel_by_subdomain(self, subdomain: str) -> Optional[Dict]:
        """Get tunnel info by subdomain"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                SELECT t.*, u.provider_username, u.auth_provider
                FROM tunnels t
                JOIN users u ON t.user_id = u.id
                WHERE t.subdomain = ? AND t.is_active = 1
            """, (subdomain,))
            
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    
    # API key management methods
    def create_api_key_for_tunnel(self, tunnel_id: int, name: Optional[str] = None, 
                      expires_at: Optional[datetime] = None, scopes: str = "tunnel:create") -> str:
        """Create a new API key for a tunnel and return the raw key
        
        Each tunnel can only have one active API key at a time.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            # Get tunnel info to find user_id
            cursor.execute("SELECT user_id FROM tunnels WHERE id = ?", (tunnel_id,))
            result = cursor.fetchone()
            if not result:
                raise ValueError(f"Tunnel {tunnel_id} not found")
            user_id = result[0]
            
            # Generate a secure random API key
            raw_key = secrets.token_urlsafe(32)
            key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
            key_prefix = raw_key[:8]  # Store prefix for identification
            
            # Deactivate any existing keys for this tunnel
            cursor.execute("""
                UPDATE api_keys 
                SET is_active = 0
                WHERE tunnel_id = ? AND is_active = 1
            """, (tunnel_id,))
            
            # Now create the new API key
            cursor.execute("""
                INSERT INTO api_keys (user_id, tunnel_id, key_hash, key_prefix, name, expires_at, scopes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (user_id, tunnel_id, key_hash, key_prefix, name, expires_at, scopes))
            
            conn.commit()
            logger.info(f"     Created API key for tunnel {tunnel_id}: {key_prefix}...")
            return raw_key
        except Exception as e:
            logger.error(f"     Failed to create API key: {e}")
            raise
        finally:
            conn.close()
    
    def create_api_key(self, user_id: int, name: Optional[str] = None, 
                      expires_at: Optional[datetime] = None, scopes: str = "tunnel:create", 
                      is_admin: bool = False, provider: Optional[str] = None, username: Optional[str] = None) -> str:
        """Legacy method - creates API key for user's default tunnel
        
        Args:
            user_id: The user's ID
            name: Optional name for the API key
            expires_at: Optional expiration datetime
            scopes: API key scopes
            is_admin: Whether the user is an admin (deprecated, use provider/username)
            provider: Auth provider for admin check
            username: Username for admin check
        """
        # Get user's tunnels
        tunnels = self.list_user_tunnels(user_id)
        
        if not tunnels:
            # No tunnel exists, create one
            tunnel = self.create_tunnel(user_id, provider=provider, username=username)
            tunnel_id = tunnel["id"]
        else:
            # Use the first (default) tunnel
            tunnel_id = tunnels[0]["id"]
        
        return self.create_api_key_for_tunnel(tunnel_id, name, expires_at, scopes)
    
    def validate_api_key(self, raw_key: str) -> Optional[Dict]:
        """Validate an API key and return user info if valid"""
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        try:
            # Get API key and user/tunnel info
            cursor.execute("""
                SELECT k.*, 
                       u.id as user_id, u.provider_username, u.email, u.auth_provider,
                       t.id as tunnel_id, t.subdomain as tunnel_subdomain, t.name as tunnel_name
                FROM api_keys k
                JOIN users u ON k.user_id = u.id
                LEFT JOIN tunnels t ON k.tunnel_id = t.id
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
    
    def _generate_unique_subdomain(self, cursor) -> str:
        """Generate a unique, human-readable subdomain for a user"""
        max_attempts = 100
        
        for _ in range(max_attempts):
            # Generate a readable subdomain like "happy-panda-42" or "blue-river-dancing"
            # Using 3 words for good uniqueness while keeping it memorable
            subdomain = generate_slug(3)
            
            # Check if it's already taken
            cursor.execute("SELECT id FROM users WHERE tunnel_subdomain = ?", (subdomain,))
            if not cursor.fetchone():
                return subdomain
        
        # Fallback to random string if we can't find a unique readable name
        # This is very unlikely with 3-word combinations
        while True:
            characters = string.ascii_lowercase + string.digits
            subdomain = ''.join(random.choices(characters, k=12))
            cursor.execute("SELECT id FROM users WHERE tunnel_subdomain = ?", (subdomain,))
            if not cursor.fetchone():
                logger.warning(f"Had to use random subdomain after {max_attempts} attempts")
                return subdomain
    
    def store_oauth_state(self, state: str, provider: str = None, redirect_uri: str = None,
                         external_redirect_uri: str = None, external_state: str = None,
                         additional_data: str = None) -> bool:
        """Store OAuth state in database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            # Clean up states older than 10 minutes
            cursor.execute("""
                DELETE FROM oauth_states 
                WHERE datetime(created_at) < datetime('now', '-10 minutes')
            """)
            
            # Insert new state
            cursor.execute("""
                INSERT INTO oauth_states (state, provider, redirect_uri, 
                                        external_redirect_uri, external_state, additional_data)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (state, provider, redirect_uri, external_redirect_uri, external_state, additional_data))
            
            conn.commit()
            logger.debug(f"Stored OAuth state in database: {state}")
            return True
        except Exception as e:
            logger.error(f"Failed to store OAuth state: {e}")
            return False
        finally:
            conn.close()
    
    def verify_oauth_state(self, state: str, delete: bool = True) -> Dict:
        """Verify OAuth state from database"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        try:
            # Check if state exists and is not expired
            cursor.execute("""
                SELECT * FROM oauth_states 
                WHERE state = ? 
                AND datetime(created_at) >= datetime('now', '-10 minutes')
            """, (state,))
            
            row = cursor.fetchone()
            if not row:
                logger.warning(f"OAuth state not found or expired: {state}")
                
                # Log available states for debugging
                cursor.execute("SELECT state, created_at FROM oauth_states ORDER BY created_at DESC LIMIT 10")
                available_states = cursor.fetchall()
                if available_states:
                    logger.debug(f"Recent states in database: {[dict(s) for s in available_states]}")
                else:
                    logger.debug("No states found in database")
                
                return None
            
            result = dict(row)
            
            if delete:
                cursor.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
                conn.commit()
                logger.debug(f"Deleted OAuth state after verification: {state}")
            
            return result
        except Exception as e:
            logger.error(f"Failed to verify OAuth state: {e}")
            return None
        finally:
            conn.close()
    
    def cleanup_oauth_states(self) -> int:
        """Clean up expired OAuth states"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                DELETE FROM oauth_states 
                WHERE datetime(created_at) < datetime('now', '-10 minutes')
            """)
            
            deleted = cursor.rowcount
            conn.commit()
            
            if deleted > 0:
                logger.info(f"Cleaned up {deleted} expired OAuth states")
            
            return deleted
        except Exception as e:
            logger.error(f"Failed to cleanup OAuth states: {e}")
            return 0
        finally:
            conn.close()