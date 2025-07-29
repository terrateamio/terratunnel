"""
Streaming protocol for handling large files over WebSocket.

Protocol Design:
1. For responses larger than threshold, client sends initial response with streaming flag
2. Client then sends file data in chunks with metadata
3. Server reassembles chunks and streams to HTTP client
4. Supports checksums for integrity verification
"""

import asyncio
import base64
import hashlib
import json
import uuid
from typing import AsyncIterator, Optional, Dict, Any, Callable
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)

# Default chunk size (512KB - small enough to avoid memory issues, large enough to be efficient)
DEFAULT_CHUNK_SIZE = 512 * 1024

# Threshold for when to use streaming (files larger than 10MB)
STREAMING_THRESHOLD = 10 * 1024 * 1024


@dataclass
class StreamMetadata:
    """Metadata for a streaming session."""
    stream_id: str
    request_id: str
    total_size: int
    total_chunks: int
    content_type: str
    filename: Optional[str] = None
    checksum: Optional[str] = None


class StreamMessage:
    """Factory for creating streaming protocol messages."""
    
    @staticmethod
    def response_init(request_id: str, status_code: int, headers: dict, 
                     stream_id: str, total_size: int, content_type: str) -> dict:
        """Initial response indicating streaming will follow."""
        return {
            "request_id": request_id,
            "status_code": status_code,
            "headers": headers,
            "body": "",  # Empty body
            "is_binary": True,
            "is_streaming": True,
            "stream": {
                "id": stream_id,
                "total_size": total_size,
                "content_type": content_type
            }
        }
    
    @staticmethod
    def chunk(stream_id: str, chunk_index: int, total_chunks: int, 
              data: bytes, checksum: str) -> dict:
        """Create a chunk message."""
        return {
            "type": "stream_chunk",
            "stream_id": stream_id,
            "chunk": {
                "index": chunk_index,
                "total": total_chunks,
                "data": base64.b64encode(data).decode('ascii'),
                "checksum": checksum,  # SHA256 of this chunk
                "size": len(data)
            }
        }
    
    @staticmethod
    def complete(stream_id: str, final_checksum: str) -> dict:
        """Signal stream completion."""
        return {
            "type": "stream_complete",
            "stream_id": stream_id,
            "checksum": final_checksum  # SHA256 of entire file
        }
    
    @staticmethod
    def error(stream_id: str, error: str) -> dict:
        """Signal stream error."""
        return {
            "type": "stream_error",
            "stream_id": stream_id,
            "error": error
        }


class ChunkedStreamer:
    """Handles chunking data for streaming."""
    
    def __init__(self, chunk_size: int = DEFAULT_CHUNK_SIZE):
        self.chunk_size = chunk_size
    
    async def stream_bytes(self, data: bytes, stream_id: str) -> AsyncIterator[dict]:
        """Stream bytes data as chunks."""
        total_chunks = (len(data) + self.chunk_size - 1) // self.chunk_size
        file_hasher = hashlib.sha256()
        
        
        for i in range(0, len(data), self.chunk_size):
            chunk = data[i:i + self.chunk_size]
            chunk_index = i // self.chunk_size
            
            # Calculate chunk checksum
            chunk_hasher = hashlib.sha256(chunk)
            chunk_checksum = chunk_hasher.hexdigest()
            
            # Update file checksum
            file_hasher.update(chunk)
            
            
            # Yield chunk message
            chunk_msg = StreamMessage.chunk(
                stream_id=stream_id,
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                data=chunk,
                checksum=chunk_checksum
            )
            
            
            yield chunk_msg
            
            # Allow other tasks to run
            await asyncio.sleep(0)
        
        final_checksum = file_hasher.hexdigest()
        
        # Yield completion message
        complete_msg = StreamMessage.complete(stream_id, final_checksum)
        yield complete_msg
    
    async def stream_response(self, response, stream_id: str) -> AsyncIterator[dict]:
        """Stream an httpx response."""
        total_chunks = 0
        chunk_index = 0
        file_hasher = hashlib.sha256()
        chunks_buffer = []
        
        # First pass: collect chunks to know total count
        async for chunk in response.aiter_bytes(chunk_size=self.chunk_size):
            chunks_buffer.append(chunk)
            total_chunks += 1
        
        # Second pass: yield chunks with proper metadata
        for chunk in chunks_buffer:
            chunk_hasher = hashlib.sha256(chunk)
            chunk_checksum = chunk_hasher.hexdigest()
            file_hasher.update(chunk)
            
            yield StreamMessage.chunk(
                stream_id=stream_id,
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                data=chunk,
                checksum=chunk_checksum
            )
            
            chunk_index += 1
            await asyncio.sleep(0)
        
        # Yield completion
        yield StreamMessage.complete(stream_id, file_hasher.hexdigest())


class StreamReceiver:
    """Handles receiving and reassembling streamed chunks."""
    
    def __init__(self):
        self.active_streams: Dict[str, Dict[str, Any]] = {}
        self._chunk_handlers: Dict[str, Callable] = {}
    
    def start_stream(self, metadata: StreamMetadata, chunk_handler: Optional[Callable] = None):
        """Initialize a new incoming stream."""
        
        self.active_streams[metadata.stream_id] = {
            "metadata": metadata,
            "chunks": {},
            "received": 0,
            "file_hasher": hashlib.sha256(),
            "completed": False,
            "error": None
        }
        
        
        if chunk_handler:
            self._chunk_handlers[metadata.stream_id] = chunk_handler
    
    async def handle_chunk(self, message: dict) -> Optional[bytes]:
        """Process a chunk message."""
        stream_id = message.get("stream_id")
        
        if stream_id not in self.active_streams:
            logger.error(f"[RECEIVER] Received chunk for unknown stream: {stream_id}. Active streams: {list(self.active_streams.keys())}")
            return None
        
        stream = self.active_streams[stream_id]
        chunk_data = message.get("chunk", {})
        
        # Log chunk details
        chunk_index = chunk_data.get("index", 0)
        chunk_total = chunk_data.get("total", 0)
        chunk_size = chunk_data.get("size", 0)
        chunk_checksum = chunk_data.get("checksum", "")
        encoded_data = chunk_data.get("data", "")
        
        
        # Decode chunk data
        chunk_bytes = base64.b64decode(encoded_data)
        
        # Verify chunk checksum
        expected_checksum = chunk_checksum
        actual_checksum = hashlib.sha256(chunk_bytes).hexdigest()
        
        if expected_checksum != actual_checksum:
            stream["error"] = f"Chunk {chunk_index} checksum mismatch: expected={expected_checksum}, actual={actual_checksum}"
            logger.error(f"[RECEIVER] {stream['error']}")
            return None
        
        # Store chunk
        stream["chunks"][chunk_index] = chunk_bytes
        stream["received"] += 1
        stream["file_hasher"].update(chunk_bytes)
        
        
        # Update metadata if we didn't know total chunks before
        if stream["metadata"].total_chunks == 0:
            stream["metadata"].total_chunks = chunk_total
        
        # Call chunk handler if registered
        if stream_id in self._chunk_handlers:
            await self._chunk_handlers[stream_id](chunk_bytes, chunk_index)
        
        return chunk_bytes
    
    def handle_complete(self, message: dict) -> bool:
        """Handle stream completion message."""
        stream_id = message.get("stream_id")
        
        if stream_id not in self.active_streams:
            logger.error(f"[RECEIVER] Completion for unknown stream: {stream_id}. Active streams: {list(self.active_streams.keys())}")
            return False
        
        stream = self.active_streams[stream_id]
        expected_checksum = message.get("checksum")
        actual_checksum = stream["file_hasher"].hexdigest()
        
        
        if expected_checksum != actual_checksum:
            stream["error"] = f"File checksum mismatch: expected {expected_checksum}, got {actual_checksum}"
            logger.error(f"[RECEIVER] {stream['error']}")
            return False
        
        stream["completed"] = True
        logger.info(f"Stream {stream_id} completed successfully with {stream['received']} chunks")
        return True
    
    def handle_error(self, message: dict):
        """Handle stream error message."""
        stream_id = message.get("stream_id")
        if stream_id in self.active_streams:
            self.active_streams[stream_id]["error"] = message.get("error", "Unknown error")
    
    def get_assembled_data(self, stream_id: str) -> Optional[bytes]:
        """Get the complete assembled data for a stream."""
        
        if stream_id not in self.active_streams:
            logger.error(f"[RECEIVER] Stream {stream_id} not found in active streams")
            return None
        
        stream = self.active_streams[stream_id]
        if not stream["completed"]:
            logger.error(f"[RECEIVER] Stream {stream_id} is not completed yet")
            return None
        
        # Get total chunks from metadata
        metadata = stream["metadata"]
        
        total_chunks = metadata.total_chunks
        
        # If total_chunks is 0, use the number of chunks we actually received
        if total_chunks == 0:
            logger.warning(f"[RECEIVER] total_chunks is 0, using actual chunk count: {len(stream['chunks'])}")
            total_chunks = len(stream['chunks'])
        
        # Assemble chunks in order
        result = bytearray()
        
        for i in range(total_chunks):
            if i not in stream["chunks"]:
                logger.error(f"[RECEIVER] Missing chunk {i} in stream {stream_id}")
                logger.error(f"[RECEIVER] Available chunks: {sorted(stream['chunks'].keys())}")
                return None
            
            chunk_data = stream["chunks"][i]
            result.extend(chunk_data)
        
        assembled_data = bytes(result)
        
        return assembled_data
    
    def cleanup_stream(self, stream_id: str):
        """Remove completed or errored stream."""
        if stream_id in self.active_streams:
            del self.active_streams[stream_id]
        if stream_id in self._chunk_handlers:
            del self._chunk_handlers[stream_id]