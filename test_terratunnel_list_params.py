#!/usr/bin/env python3
"""
End-to-end test for list query parameter support in Terratunnel.
This script tests that Terratunnel properly preserves repeated query parameters.
"""
import asyncio
import sys
import time
import subprocess
import os
from fastapi import FastAPI, Query, Request
from typing import List, Optional
import uvicorn
import httpx
import json

# Test FastAPI app that receives requests through Terratunnel
app = FastAPI()

# Store received requests for verification
received_requests = []

@app.get("/api/v1/github/installations/{installation_id}/repos")
async def test_endpoint(
    request: Request,
    installation_id: int,
    page: Optional[List[str]] = Query(None, description="List of pages")
):
    """Test endpoint that accepts list query parameters"""
    result = {
        "installation_id": installation_id,
        "received_via_fastapi": {
            "page": page,
        },
        "raw_query_params": dict(request.query_params.multi_items()),
        "query_string": str(request.url.query),
        "full_url": str(request.url)
    }
    received_requests.append(result)
    return result

@app.get("/api/test/check")
async def check_received():
    """Return all received requests for verification"""
    return {"requests": received_requests}

async def run_test():
    """Run the actual test"""
    print("\n" + "="*60)
    print("TERRATUNNEL LIST QUERY PARAMETER TEST")
    print("="*60)

    # Start the test FastAPI server
    print("\n1. Starting test FastAPI server on port 3000...")
    server_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "test_terratunnel_list_params:app", "--host", "0.0.0.0", "--port", "3000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    # Start Terratunnel server
    print("2. Starting Terratunnel server on port 8000...")
    tunnel_server_proc = subprocess.Popen(
        [sys.executable, "-m", "terratunnel", "server", "--port", "8000", "--domain", "localhost:8000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**dict(os.environ), "REQUIRE_AUTH_FOR_TUNNELS": "false"}
    )

    # Start Terratunnel client
    print("3. Starting Terratunnel client connecting to localhost:3000...")
    tunnel_client_proc = subprocess.Popen(
        [sys.executable, "-m", "terratunnel", "client", "--server", "ws://localhost:8000", "--local-endpoint", "http://localhost:3000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT
    )

    # Wait for everything to start
    print("4. Waiting for services to start...")
    await asyncio.sleep(5)

    # Get the tunnel URL from client output
    tunnel_url = None
    for line in tunnel_client_proc.stdout:
        line = line.decode('utf-8').strip()
        if "Tunnel created:" in line:
            tunnel_url = line.split("Tunnel created:")[1].strip()
            break

    if not tunnel_url:
        print("ERROR: Could not get tunnel URL from client")
        return False

    print(f"5. Tunnel URL: {tunnel_url}")

    # Now test with list query parameters
    print("\n6. Testing list query parameters through tunnel...")

    async with httpx.AsyncClient() as client:
        # Test URL with repeated query parameters
        test_url = f"{tunnel_url}/api/v1/github/installations/55143082/repos?page=n&page=malcolm-demo"
        print(f"   Making request to: {test_url}")

        try:
            response = await client.get(test_url, timeout=10.0)
            print(f"   Response status: {response.status_code}")

            result = response.json()
            print(f"   Response data:")
            print(json.dumps(result, indent=2))

            # Verify the response
            if result.get("received_via_fastapi", {}).get("page") == ["n", "malcolm-demo"]:
                print("\n   ✅ SUCCESS: List query parameters preserved correctly!")
                print(f"   Received page list: {result['received_via_fastapi']['page']}")
                success = True
            else:
                print("\n   ❌ FAILURE: List query parameters not preserved")
                print(f"   Expected: ['n', 'malcolm-demo']")
                print(f"   Received: {result.get('received_via_fastapi', {}).get('page')}")
                success = False

        except Exception as e:
            print(f"   ❌ ERROR: {e}")
            success = False

    # Cleanup
    print("\n7. Cleaning up...")
    server_proc.terminate()
    tunnel_server_proc.terminate()
    tunnel_client_proc.terminate()

    # Wait for processes to terminate
    server_proc.wait(timeout=5)
    tunnel_server_proc.wait(timeout=5)
    tunnel_client_proc.wait(timeout=5)

    return success

if __name__ == "__main__":
    import os

    if len(sys.argv) > 1 and sys.argv[1] == "server-only":
        # Just run the test server for manual testing
        print("Starting test server on http://localhost:3000")
        uvicorn.run(app, host="0.0.0.0", port=3000)
    else:
        # Run the full test
        success = asyncio.run(run_test())
        sys.exit(0 if success else 1)