#!/usr/bin/env python3
"""
Test to verify what encoding is ACTUALLY received by the local server.
This will help diagnose if httpx is re-encoding the URL.
"""
from fastapi import FastAPI, Request
import uvicorn
import httpx
import asyncio
import sys

app = FastAPI()

# Store the actual request details
last_request = {}

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def capture_all(request: Request, path: str = ""):
    """Capture ALL details about the incoming request"""
    global last_request

    # Get the raw URL components
    last_request = {
        "method": request.method,
        "path": path,
        "raw_path": request.url.path,
        "raw_query": str(request.url.query) if request.url.query else "",
        "full_url": str(request.url),
        "headers": dict(request.headers),
        "scope_path": request.scope.get("path", ""),
        "scope_query_string": request.scope.get("query_string", b"").decode('utf-8'),
    }

    print(f"\n{'='*60}")
    print(f"RECEIVED REQUEST AT LOCAL SERVER:")
    print(f"{'='*60}")
    print(f"Method: {last_request['method']}")
    print(f"Path: {last_request['path']}")
    print(f"Raw Path: {last_request['raw_path']}")
    print(f"Raw Query: {last_request['raw_query']}")
    print(f"Scope Query String: {last_request['scope_query_string']}")
    print(f"Full URL: {last_request['full_url']}")
    print(f"{'='*60}\n")

    return last_request

async def test_through_tunnel():
    """Test various encodings through the tunnel"""
    print("\n" + "="*70)
    print("TESTING ACTUAL HTTP ENCODING THROUGH TERRATUNNEL")
    print("="*70)

    tunnel_url = "http://imported-amigurumi-zebra.localhost:8000"

    test_cases = [
        {
            "name": "URL encoded equals sign",
            "path": "/api/test",
            "query": "filter=status%3Dactive",
            "description": "Should preserve %3D (encoded equals)"
        },
        {
            "name": "Multiple params with encoded values",
            "path": "/api/test",
            "query": "filter=status%3Dactive&sort=name%2Casc",
            "description": "Should preserve %3D and %2C"
        },
        {
            "name": "Space as %20",
            "path": "/api/test",
            "query": "name=John%20Doe",
            "description": "Should preserve %20 for space"
        },
        {
            "name": "Space as plus",
            "path": "/api/test",
            "query": "name=John+Doe",
            "description": "Should preserve + for space"
        },
        {
            "name": "Already decoded special chars",
            "path": "/api/test",
            "query": "data=value=123",
            "description": "Unencoded equals in value"
        },
    ]

    async with httpx.AsyncClient(timeout=10.0) as client:
        for test in test_cases:
            print(f"\nTest: {test['name']}")
            print(f"Description: {test['description']}")

            url = f"{tunnel_url}{test['path']}?{test['query']}"
            print(f"Sending to tunnel: {url}")
            print(f"Expected query: {test['query']}")

            try:
                response = await client.get(url)
                result = response.json()

                received_query = result.get('scope_query_string', '')

                if received_query == test['query']:
                    print(f"✅ PASSED: Query preserved exactly")
                else:
                    print(f"❌ FAILED: Query was modified")
                    print(f"  Sent:     {test['query']}")
                    print(f"  Received: {received_query}")

                    # Check each encoding
                    if '%3D' in test['query']:
                        if '%3D' in received_query:
                            print(f"  ✓ %3D preserved")
                        elif '=' in received_query and '%3D' not in received_query:
                            print(f"  ✗ %3D was decoded to =")

                    if '%2C' in test['query']:
                        if '%2C' in received_query:
                            print(f"  ✓ %2C preserved")
                        elif ',' in received_query and '%2C' not in received_query:
                            print(f"  ✗ %2C was decoded to ,")

                    if '%20' in test['query']:
                        if '%20' in received_query:
                            print(f"  ✓ %20 preserved")
                        elif ' ' in received_query:
                            print(f"  ✗ %20 was decoded to space")
                        elif '+' in received_query:
                            print(f"  ✗ %20 was converted to +")

                    if '+' in test['query']:
                        if '+' in received_query:
                            print(f"  ✓ + preserved")
                        elif ' ' in received_query:
                            print(f"  ✗ + was decoded to space")
                        elif '%20' in received_query:
                            print(f"  ✗ + was converted to %20")

            except Exception as e:
                print(f"❌ ERROR: {e}")

async def test_direct_httpx():
    """Test httpx behavior directly without tunnel"""
    print("\n" + "="*70)
    print("TESTING HTTPX BEHAVIOR DIRECTLY (NO TUNNEL)")
    print("="*70)

    test_query = "filter=status%3Dactive&sort=name%2Casc"

    print(f"\nTest: Direct httpx call with encoded query")
    print(f"Query: {test_query}")

    # Method 1: URL with query string
    print("\nMethod 1: Full URL with query")
    url = f"http://localhost:3000/api/test?{test_query}"
    print(f"URL: {url}")

    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        result = response.json()
        print(f"Received query: {result.get('scope_query_string', '')}")

        # Method 2: Using params parameter (for comparison)
        print("\nMethod 2: Using params dict")
        params = {"filter": "status=active", "sort": "name,asc"}
        response = await client.get("http://localhost:3000/api/test", params=params)
        result = response.json()
        print(f"Received query: {result.get('scope_query_string', '')}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test-tunnel":
        asyncio.run(test_through_tunnel())
    elif len(sys.argv) > 1 and sys.argv[1] == "test-direct":
        asyncio.run(test_direct_httpx())
    else:
        print("Starting test server on http://localhost:3000")
        print("Run:")
        print("  python3 test_actual_encoding.py test-direct    # Test httpx directly")
        print("  python3 test_actual_encoding.py test-tunnel    # Test through tunnel")
        uvicorn.run(app, host="0.0.0.0", port=3000)