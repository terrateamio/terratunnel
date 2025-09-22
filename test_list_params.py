#!/usr/bin/env python3
"""
Test app to validate list query parameter support in FastAPI and Terratunnel
"""
from fastapi import FastAPI, Query, Request
from typing import List, Optional
import uvicorn
import httpx
import asyncio

app = FastAPI()

@app.get("/api/test/list-params")
async def test_list_params(
    request: Request,
    page: Optional[List[str]] = Query(None, description="List of pages"),
    tag: Optional[List[str]] = Query(None, description="List of tags")
):
    """Test endpoint that accepts list query parameters"""
    return {
        "received_via_fastapi": {
            "page": page,
            "tag": tag
        },
        "raw_query_params": dict(request.query_params.multi_items()),
        "query_string": str(request.url.query)
    }

@app.get("/api/test/single-params")
async def test_single_params(
    page: Optional[str] = Query(None, description="Single page param"),
    tag: Optional[str] = Query(None, description="Single tag param")
):
    """Test endpoint that accepts single query parameters"""
    return {
        "page": page,
        "tag": tag
    }

async def test_client():
    """Test client to verify httpx behavior with list query params"""
    async with httpx.AsyncClient() as client:
        # Test 1: List params as list
        print("\nTest 1: Sending params as lists to httpx")
        response = await client.get(
            "http://localhost:3000/api/test/list-params",
            params={"page": ["n", "malcolm-demo"], "tag": ["foo", "bar"]}
        )
        print(f"URL: {response.url}")
        print(f"Response: {response.json()}")

        # Test 2: Multiple same-key params
        print("\nTest 2: Manual URL with repeated keys")
        response = await client.get(
            "http://localhost:3000/api/test/list-params?page=n&page=malcolm-demo&tag=foo&tag=bar"
        )
        print(f"URL: {response.url}")
        print(f"Response: {response.json()}")

        # Test 3: Single params
        print("\nTest 3: Single params")
        response = await client.get(
            "http://localhost:3000/api/test/single-params",
            params={"page": "single", "tag": "one"}
        )
        print(f"URL: {response.url}")
        print(f"Response: {response.json()}")

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "client":
        # Run test client
        asyncio.run(test_client())
    else:
        # Run server
        print("Starting test server on http://localhost:3000")
        print("Test endpoints:")
        print("  - http://localhost:3000/api/test/list-params?page=n&page=malcolm-demo")
        print("  - http://localhost:3000/api/test/single-params?page=single")
        print("\nRun 'python test_list_params.py client' in another terminal to test")
        uvicorn.run(app, host="0.0.0.0", port=3000)