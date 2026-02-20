"""
HTTP proxy logic for forwarding requests to Anthropic API.
"""
import json
import logging
import secrets
from typing import AsyncIterator

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from .tracker import SubscriptionTracker, SubscriptionState

logger = logging.getLogger(__name__)

ANTHROPIC_BASE_URL = "https://api.anthropic.com"

# Max retries for 429 on non-streaming requests
MAX_429_RETRIES = 2

# Max request body size (10MB - Anthropic's limit is ~1MB for most requests)
MAX_REQUEST_BODY_SIZE = 10 * 1024 * 1024

# Headers we add/modify (skip from incoming request)
SKIP_REQUEST_HEADERS = {
    "host",
    "authorization",
    "x-api-key",
    "content-length",  # httpx handles this
    "transfer-encoding",
}

# Response headers to skip (hop-by-hop or handled by FastAPI)
SKIP_RESPONSE_HEADERS = {
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
}


def _generate_request_id() -> str:
    """Generate a short request ID for tracing."""
    return secrets.token_hex(4)


class AnthropicProxy:
    """
    Proxies requests to Anthropic API with load balancing.
    """
    
    def __init__(self, tracker: SubscriptionTracker, timeout: float = 300.0):
        self.tracker = tracker
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
    
    async def startup(self):
        """Initialize the HTTP client."""
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
        )
        logger.info("Proxy HTTP client initialized")
    
    async def shutdown(self):
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            logger.info("Proxy HTTP client closed")
    
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Proxy not initialized. Call startup() first.")
        return self._client
    
    def _build_headers(self, request: Request, subscription: SubscriptionState) -> dict:
        """Build headers for the upstream request."""
        headers = {}
        
        # Pass through relevant headers
        for key, value in request.headers.items():
            if key.lower() not in SKIP_REQUEST_HEADERS:
                headers[key] = value
        
        # Set auth for the selected subscription
        # OAuth tokens (sk-ant-oat*) use Bearer, regular API keys use x-api-key
        if subscription.api_key.startswith("sk-ant-oat"):
            # OAuth token - use Bearer auth
            headers["authorization"] = f"Bearer {subscription.api_key}"
            headers.pop("x-api-key", None)
        elif subscription.api_key.startswith("sk-ant-"):
            # Regular API key
            headers["x-api-key"] = subscription.api_key
            headers.pop("authorization", None)
        else:
            # Unknown format - try Bearer (safer default for OAuth)
            headers["authorization"] = f"Bearer {subscription.api_key}"
            headers.pop("x-api-key", None)
        
        return headers
    
    def _filter_response_headers(self, headers: httpx.Headers) -> dict:
        """Filter response headers for the client."""
        return {
            key: value
            for key, value in headers.items()
            if key.lower() not in SKIP_RESPONSE_HEADERS
        }
    
    def _is_streaming_request(self, body: bytes) -> bool:
        """Check if request body indicates streaming."""
        try:
            payload = json.loads(body)
            return payload.get("stream", False) is True
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False
    
    async def _stream_response(
        self,
        response: httpx.Response,
        subscription: SubscriptionState,
        request_id: str,
    ) -> AsyncIterator[bytes]:
        """Stream response chunks to the client."""
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        except httpx.StreamClosed:
            # Client disconnected - this is normal
            logger.debug(f"[{request_id}] Stream closed by client")
        except Exception as e:
            logger.error(f"[{request_id}] Error streaming from {subscription.name}: {e}")
            raise
        finally:
            await response.aclose()
    
    async def proxy_request(self, request: Request, path: str) -> Response:
        """
        Proxy a request to Anthropic API with automatic retry on 429.
        
        Args:
            request: The incoming FastAPI request.
            path: The API path (e.g., "/v1/messages").
            
        Returns:
            Response to send to the client.
        """
        request_id = _generate_request_id()
        
        # Build the upstream URL
        url = f"{ANTHROPIC_BASE_URL}{path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        
        # Check content-length before reading body
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_REQUEST_BODY_SIZE:
                    logger.warning(f"[{request_id}] Request body too large: {content_length} bytes")
                    return Response(
                        content='{"error": {"type": "request_too_large", "message": "Request body exceeds maximum size."}}',
                        status_code=413,
                        media_type="application/json",
                    )
            except ValueError:
                pass  # Invalid content-length header, let it through
        
        # Get request body
        body = await request.body()
        
        # Double-check actual body size
        if len(body) > MAX_REQUEST_BODY_SIZE:
            logger.warning(f"[{request_id}] Request body too large: {len(body)} bytes")
            return Response(
                content='{"error": {"type": "request_too_large", "message": "Request body exceeds maximum size."}}',
                status_code=413,
                media_type="application/json",
            )
        
        # Check if streaming
        is_streaming = self._is_streaming_request(body)
        
        # For streaming requests, no retry (can't replay partial streams)
        if is_streaming:
            subscription = await self.tracker.select_subscription()
            if subscription is None:
                logger.warning(f"[{request_id}] No subscriptions available")
                return Response(
                    content='{"error": {"type": "overloaded", "message": "All API subscriptions are currently at capacity. Please retry."}}',
                    status_code=503,
                    media_type="application/json",
                )
            
            # Build headers
            headers = self._build_headers(request, subscription)
            
            logger.info(f"[{request_id}] -> {subscription.name} (streaming) {request.method} {path}")
            
            async with self.tracker.acquire_connection(subscription):
                try:
                    response = await self.client.send(
                        self.client.build_request(
                            method=request.method,
                            url=url,
                            headers=headers,
                            content=body,
                        ),
                        stream=True,
                    )
                    
                    if response.status_code == 429:
                        await response.aclose()
                        await self.tracker.record_429(subscription)
                        logger.warning(f"[{request_id}] 429 from {subscription.name}")
                        return Response(
                            content='{"error": {"type": "rate_limit", "message": "Rate limited. Please retry."}}',
                            status_code=429,
                            media_type="application/json",
                        )
                    
                    logger.info(f"[{request_id}] <- {subscription.name} {response.status_code}")
                    
                    return StreamingResponse(
                        self._stream_response(response, subscription, request_id),
                        status_code=response.status_code,
                        headers=self._filter_response_headers(response.headers),
                        media_type=response.headers.get("content-type", "text/event-stream"),
                    )
                    
                except httpx.TimeoutException as e:
                    logger.error(f"[{request_id}] Timeout -> {subscription.name}: {e}")
                    await self.tracker.record_error(subscription)
                    return Response(
                        content='{"error": {"type": "timeout", "message": "Request timed out."}}',
                        status_code=504,
                        media_type="application/json",
                    )
                except httpx.RequestError as e:
                    logger.error(f"[{request_id}] Error -> {subscription.name}: {e}")
                    await self.tracker.record_error(subscription)
                    return Response(
                        content='{"error": {"type": "proxy_error", "message": "Failed to connect to upstream."}}',
                        status_code=502,
                        media_type="application/json",
                    )
        
        # Non-streaming: retry on 429
        excluded: set[str] = set()
        last_error_response: Response | None = None
        
        for attempt in range(MAX_429_RETRIES + 1):
            subscription = await self.tracker.select_subscription()
            
            if subscription is None:
                logger.warning(f"[{request_id}] No subscriptions available (attempt {attempt + 1})")
                return Response(
                    content='{"error": {"type": "overloaded", "message": "All API subscriptions are currently at capacity. Please retry."}}',
                    status_code=503,
                    media_type="application/json",
                )
            
            # Skip already-tried subscriptions
            if subscription.name in excluded:
                # All available have been tried
                logger.warning(f"[{request_id}] All subscriptions exhausted after {len(excluded)} attempts")
                break
            
            headers = self._build_headers(request, subscription)
            
            logger.info(f"[{request_id}] -> {subscription.name} (attempt {attempt + 1}) {request.method} {path}")
            
            async with self.tracker.acquire_connection(subscription):
                try:
                    response = await self.client.request(
                        method=request.method,
                        url=url,
                        headers=headers,
                        content=body,
                    )
                    
                    if response.status_code == 429:
                        await self.tracker.record_429(subscription)
                        excluded.add(subscription.name)
                        logger.warning(f"[{request_id}] 429 from {subscription.name}, will retry")
                        continue  # Try next subscription
                    
                    if response.status_code >= 500:
                        await self.tracker.record_error(subscription)
                    
                    logger.info(f"[{request_id}] <- {subscription.name} {response.status_code}")
                    
                    return Response(
                        content=response.content,
                        status_code=response.status_code,
                        headers=self._filter_response_headers(response.headers),
                        media_type=response.headers.get("content-type", "application/json"),
                    )
                    
                except httpx.TimeoutException as e:
                    logger.error(f"[{request_id}] Timeout -> {subscription.name}: {e}")
                    await self.tracker.record_error(subscription)
                    last_error_response = Response(
                        content='{"error": {"type": "timeout", "message": "Request timed out."}}',
                        status_code=504,
                        media_type="application/json",
                    )
                    # Don't retry on timeout - just return
                    return last_error_response
                    
                except httpx.RequestError as e:
                    logger.error(f"[{request_id}] Error -> {subscription.name}: {e}")
                    await self.tracker.record_error(subscription)
                    last_error_response = Response(
                        content='{"error": {"type": "proxy_error", "message": "Failed to connect to upstream."}}',
                        status_code=502,
                        media_type="application/json",
                    )
                    # Don't retry on connection error - just return
                    return last_error_response
        
        # All retries exhausted (only 429s)
        logger.warning(f"[{request_id}] All {len(excluded)} subscriptions rate limited")
        return Response(
            content='{"error": {"type": "rate_limit", "message": "All subscriptions rate limited. Please retry later."}}',
            status_code=429,
            media_type="application/json",
        )
