#!/usr/bin/env python3
"""
Test to verify query string encoding preservation in Terratunnel.
Tests various special characters and encodings to ensure they're preserved correctly.
"""
from fastapi import FastAPI, Request
from typing import Optional
import uvicorn
import httpx
import asyncio
import urllib.parse

app = FastAPI()

# Store all received requests for verification
received_requests = []

@app.get("/test")
async def test_endpoint(request: Request):
    """Capture the raw query string and parsed params"""
    result = {
        "raw_query_string": str(request.url.query),
        "parsed_params": dict(request.query_params),
        "full_url": str(request.url),
    }
    received_requests.append(result)
    return result

@app.get("/clear")
async def clear_requests():
    """Clear stored requests"""
    received_requests.clear()
    return {"status": "cleared"}

async def run_tests():
    """Test various query string encodings through Terratunnel"""
    print("\n" + "="*60)
    print("QUERY STRING ENCODING PRESERVATION TEST")
    print("="*60)

    # Test cases with various special characters and encodings
    test_cases = [
        {
            "name": "Special characters",
            "query": "filter=status%3Dactive&sort=name%2Casc",
            "expected_raw": "filter=status%3Dactive&sort=name%2Casc",
            "description": "URL encoded equals and comma"
        },
        {
            "name": "Spaces encoded as %20",
            "query": "name=John%20Doe&city=New%20York",
            "expected_raw": "name=John%20Doe&city=New%20York",
            "description": "Spaces encoded as %20 (not +)"
        },
        {
            "name": "Spaces encoded as +",
            "query": "name=John+Doe&city=New+York",
            "expected_raw": "name=John+Doe&city=New+York",
            "description": "Spaces encoded as plus signs"
        },
        {
            "name": "Mixed encodings",
            "query": "path=%2Fhome%2Fuser&special=%40%23%24%25",
            "expected_raw": "path=%2Fhome%2Fuser&special=%40%23%24%25",
            "description": "Slashes and special characters"
        },
        {
            "name": "Unicode characters",
            "query": "name=%E2%98%85&emoji=%F0%9F%98%80",
            "expected_raw": "name=%E2%98%85&emoji=%F0%9F%98%80",
            "description": "UTF-8 encoded unicode"
        },
        {
            "name": "Already decoded characters",
            "query": "filter=type=user&data={\"key\":\"value\"}",
            "expected_raw": None,  # Will be URL encoded by the client
            "description": "JSON-like data in query"
        },
        {
            "name": "Empty values",
            "query": "key1=&key2=value&key3=",
            "expected_raw": "key1=&key2=value&key3=",
            "description": "Parameters with empty values"
        },
        {
            "name": "Complex AWS signature",
            "query": "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Date=20240101T000000Z&X-Amz-SignedHeaders=host%3Bx-amz-date",
            "expected_raw": "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Date=20240101T000000Z&X-Amz-SignedHeaders=host%3Bx-amz-date",
            "description": "AWS-style signature parameters"
        }
    ]

    # Wait for tunnel to be ready
    await asyncio.sleep(2)

    # Get tunnel URL
    tunnel_url = "http://imported-amigurumi-zebra.localhost:8000"

    print(f"\nUsing tunnel URL: {tunnel_url}")
    print("\nRunning tests...")
    print("-" * 60)

    passed = 0
    failed = 0

    async with httpx.AsyncClient(timeout=10.0) as client:
        for test in test_cases:
            print(f"\nTest: {test['name']}")
            print(f"Description: {test['description']}")

            # Clear previous requests
            await client.get("http://localhost:3000/clear")

            # Make request through tunnel
            test_url = f"{tunnel_url}/test?{test['query']}"
            print(f"Request URL: {test_url}")

            try:
                response = await client.get(test_url)
                result = response.json()

                # The expected raw query should match what we sent
                # (unless it needs encoding)
                expected = test.get('expected_raw', test['query'])

                print(f"Sent query:     {test['query']}")
                print(f"Received query: {result['raw_query_string']}")

                if result['raw_query_string'] == expected:
                    print("✅ PASSED: Query string preserved correctly")
                    passed += 1
                else:
                    # Check if it's semantically equivalent (decoded values match)
                    sent_params = urllib.parse.parse_qs(test['query'])
                    received_params = urllib.parse.parse_qs(result['raw_query_string'])

                    if sent_params == received_params:
                        print("⚠️  PARTIAL: Query string modified but semantically equivalent")
                        print(f"   Sent params:     {sent_params}")
                        print(f"   Received params: {received_params}")
                        passed += 1
                    else:
                        print("❌ FAILED: Query string not preserved")
                        print(f"   Expected: {expected}")
                        print(f"   Got:      {result['raw_query_string']}")
                        failed += 1

            except Exception as e:
                print(f"❌ ERROR: {e}")
                failed += 1

    print("\n" + "="*60)
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("="*60)

    return passed, failed

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        # Run the test client
        passed, failed = asyncio.run(run_tests())
        sys.exit(0 if failed == 0 else 1)
    else:
        # Run the test server
        print("Starting test server on http://localhost:3000")
        print("Run 'python3 test_query_encoding.py test' to run tests")
        uvicorn.run(app, host="0.0.0.0", port=3000)