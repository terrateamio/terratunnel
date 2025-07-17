"""Common utility functions for terratunnel."""

import magic
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# Initialize magic with MIME type detection
try:
    mime_detector = magic.Magic(mime=True)
except Exception as e:
    logger.warning(f"Failed to initialize python-magic: {e}. Falling back to basic detection.")
    mime_detector = None


def is_binary_content(content_type: Optional[str]) -> bool:
    """
    Determine if content type indicates binary data using python-magic.
    
    Args:
        content_type: The content-type header value (e.g., 'application/pdf')
    
    Returns:
        True if the content is binary, False if it's text-based
    """
    if not content_type:
        # If no content type is provided, assume binary to be safe
        return True
    
    # Normalize content type (remove parameters like charset)
    base_type = content_type.split(';')[0].strip().lower()
    
    # Text-based MIME types that should never be treated as binary
    # This acts as our primary detection method since we're working with
    # HTTP headers, not file content
    text_types = {
        # Text formats
        'text/plain', 'text/html', 'text/css', 'text/javascript',
        'text/csv', 'text/xml', 'text/markdown', 'text/x-python',
        'text/x-java', 'text/x-c', 'text/x-cpp', 'text/x-csharp',
        'text/x-ruby', 'text/x-go', 'text/x-rust', 'text/x-typescript',
        'text/yaml', 'text/x-yaml', 'text/x-sql', 'text/x-sh',
        
        # Application text formats
        'application/json', 'application/javascript', 'application/xml',
        'application/xhtml+xml', 'application/rss+xml', 'application/atom+xml',
        'application/x-www-form-urlencoded', 'application/x-javascript',
        'application/ld+json', 'application/manifest+json',
        'application/x-httpd-php', 'application/x-sh', 'application/x-csh',
        'application/x-python-code', 'application/x-ruby', 'application/sql',
        'application/graphql', 'application/x-yaml', 'application/ecmascript',
        
        # Message types
        'message/rfc822', 'message/http',
    }
    
    # First check our whitelist
    if base_type in text_types:
        return False
    
    # Check common text patterns
    if base_type.startswith('text/'):
        return False
    
    # Check for text keywords in content type
    text_keywords = ['json', 'xml', 'javascript', 'ecmascript', 'yaml', 'csv', 'text']
    for keyword in text_keywords:
        if keyword in base_type:
            return False
    
    # If we have python-magic available and the content type suggests a file type,
    # we could use it for additional validation, but for HTTP content-type headers,
    # the above checks are sufficient
    
    # Everything else is considered binary
    return True


def is_binary_file(file_path: str) -> bool:
    """
    Determine if a file contains binary data by examining its content.
    This function uses python-magic to analyze the actual file content.
    
    Args:
        file_path: Path to the file to check
    
    Returns:
        True if the file is binary, False if it's text-based
    """
    if not mime_detector:
        # Fallback if python-magic isn't available
        try:
            with open(file_path, 'rb') as f:
                chunk = f.read(512)
                # Check for null bytes (common in binary files)
                return b'\x00' in chunk
        except Exception:
            return True
    
    try:
        mime_type = mime_detector.from_file(file_path)
        return is_binary_content(mime_type)
    except Exception as e:
        logger.warning(f"Error detecting file type for {file_path}: {e}")
        # Default to binary if detection fails
        return True