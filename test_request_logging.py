#!/usr/bin/env python3
"""Test the new request logging system."""

import sys
import os
import time
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from terratunnel.client.request_logger import RequestLogger


def test_request_logger():
    """Test the request logging functionality."""
    print("Testing Request Logger with Single Log File")
    print("-" * 50)
    
    # Create logger with test file
    test_log_file = "./test_requests.log"
    logger = RequestLogger(log_file=test_log_file)
    
    # Test request 1: Simple GET
    request1 = {
        "method": "GET",
        "path": "/api/users",
        "headers": {"host": "example.com", "user-agent": "test-client"},
        "query_params": {"page": "1", "limit": "10"},
        "body": ""
    }
    
    response1 = {
        "status_code": 200,
        "headers": {"content-type": "application/json"},
        "body": '{"users": [{"id": 1, "name": "Alice"}]}'
    }
    
    print("\nTest 1: Simple GET request")
    request_id1 = logger.log_request(request1, response1)
    access_log1 = logger.format_access_log(request1, response1, request_id1, 45.3)
    print(f"Access log: {access_log1}")
    print(f"Request ID: {request_id1}")
    
    # Test request 2: POST with body
    request2 = {
        "method": "POST",
        "path": "/api/users",
        "headers": {"host": "example.com", "content-type": "application/json"},
        "query_params": {},
        "body": '{"name": "Bob", "email": "bob@example.com"}'
    }
    
    response2 = {
        "status_code": 201,
        "headers": {"content-type": "application/json", "location": "/api/users/2"},
        "body": '{"id": 2, "name": "Bob", "email": "bob@example.com"}'
    }
    
    print("\nTest 2: POST request with body")
    request_id2 = logger.log_request(request2, response2)
    access_log2 = logger.format_access_log(request2, response2, request_id2, 123.7)
    print(f"Access log: {access_log2}")
    print(f"Request ID: {request_id2}")
    
    # Test request 3: Binary response
    request3 = {
        "method": "GET",
        "path": "/images/logo.png",
        "headers": {"host": "example.com"},
        "query_params": {},
        "body": ""
    }
    
    response3 = {
        "status_code": 200,
        "headers": {"content-type": "image/png"},
        "body": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
        "is_binary": True
    }
    
    print("\nTest 3: Binary response")
    request_id3 = logger.log_request(request3, response3)
    access_log3 = logger.format_access_log(request3, response3, request_id3, 15.2)
    print(f"Access log: {access_log3}")
    print(f"Request ID: {request_id3}")
    
    # Test request 4: Error response
    request4 = {
        "method": "GET",
        "path": "/api/not-found",
        "headers": {"host": "example.com"},
        "query_params": {},
        "body": ""
    }
    
    response4 = {
        "status_code": 404,
        "headers": {"content-type": "application/json"},
        "body": '{"error": "Resource not found"}'
    }
    
    print("\nTest 4: Error response")
    request_id4 = logger.log_request(request4, response4)
    access_log4 = logger.format_access_log(request4, response4, request_id4)
    print(f"Access log: {access_log4}")
    print(f"Request ID: {request_id4}")
    
    # Test request 5: Multi-line JSON body
    request5 = {
        "method": "POST",
        "path": "/api/complex",
        "headers": {"host": "example.com", "content-type": "application/json"},
        "query_params": {},
        "body": '''{
  "name": "Complex Request",
  "items": [
    {"id": 1, "value": "First"},
    {"id": 2, "value": "Second"}
  ]
}'''
    }
    
    response5 = {
        "status_code": 200,
        "headers": {"content-type": "application/json"},
        "body": '''{
  "success": true,
  "processed": 2
}'''
    }
    
    print("\nTest 5: Multi-line JSON request/response")
    request_id5 = logger.log_request(request5, response5)
    access_log5 = logger.format_access_log(request5, response5, request_id5, 67.8)
    print(f"Access log: {access_log5}")
    print(f"Request ID: {request_id5}")
    
    # Check the log file
    print("\n" + "-" * 50)
    print(f"Checking log file: {test_log_file}")
    
    if Path(test_log_file).exists():
        with open(test_log_file, 'r') as f:
            content = f.read()
            # Show first few entries
            lines = content.split('\n')
            print(f"Total lines in log file: {len(lines)}")
            print("\nFirst 50 lines of log file:")
            print("-" * 30)
            for line in lines[:50]:
                print(line)
        
        # Clean up test file
        os.remove(test_log_file)
        print("\n✓ Test log file cleaned up")
    else:
        print("✗ Log file not created!")
    
    print("\n✓ Test completed successfully!")


if __name__ == "__main__":
    test_request_logger()