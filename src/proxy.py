"""
HTTP proxy logic for forwarding requests to Anthropic API.
"""
import json
import logging
from typing import AsyncIterator, Optional

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from .tracker import SubscriptionTracker, SubscriptionState

logger = logging.getLogger(__name__)

ANTHROPIC_BASE_URL = "https://api.anthropic.com"

# Max retries for 429 on non-streaming requests
MAX_429_RETRIES = 2

# Headers to pass through (not modify)
PASSTHROUGH_HEADERS = {
    "anthropic-version",
    "content-type",
    "accept",
    "anthropic-beta",
}

# Headers we add/modify
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
        # OAuth keys use Bearer token, regular keys use x-api-key
        if subscription.api_key.startswith("sk-ant-"):
            # Regular API key
            headers["x-api-key"] = subscription.api_key
            headers.pop("authorization", None)
        else:
            # OAuth token - use Bearer auth
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
    ) -> AsyncIterator[bytes]:
        """Stream response chunks to the client."""
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        except Exception as e:
            logger.error(f"Error streaming response from {subscription.name}: {e}")
            raise
        finally:
            await response.aclose()
    
    async def _try_request(
        self,
        method: str,
        url: str,
        body: bytes,
        is_streaming: bool,
        excluded_subscriptions: Optional[set[str]] = None,
    ) -> tuple[Optional[SubscriptionState], Optional[Response]]:
        """
        Attempt a single request to an available subscription.
        
        Returns:
            (subscription, response) tuple. If no subscription available, both are None.
        """
        excluded = excluded_subscriptions or set()
        
        # Select a subscription (excluding any that already 429'd)
        subscription = await self.tracker.select_subscription()
        
        # Skip if we already tried this one
        while subscription and subscription.name in excluded:
            # Mark temporarily at capacity to get next one
            subscription = await self.tracker.select_subscription()
        
        if subscription is None:
            return None, None
        
        headers = {}
        # We need to rebuild headers for each attempt with the right API key
        headers["x-api-key"] = subscription.api_key
        
        logger.info(
            f"Routing request to {subscription.name}: {method} {url} "
            f"(streaming: {is_streaming})"
        )
        
        async with self.tracker.acquire_connection(subscription):
            try:
                if is_streaming:
                    response = await self.client.send(
                        self.client.build_request(
                            method=method,
                            url=url,
                            headers=headers,
                            content=body,
                        ),
                        stream=True,
                    )
                    
                    if response.status_code == 429:
                        await response.aclose()
                        await self.tracker.record_429(subscription)
                        return subscription, Response(
                            content='{"error": {"type": "rate_limit", "message": "Rate limited. Please retry."}}',
                            status_code=429,
                            media_type="application/json",
                        )
                    
                    return subscription, StreamingResponse(
                        self._stream_response(response, subscription),
                        status_code=response.status_code,
                        headers=self._filter_response_headers(response.headers),
                        media_type=response.headers.get("content-type", "text/event-stream"),
                    )
                else:
                    response = await self.client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        content=body,
                    )
                    
                    if response.status_code == 429:
                        await self.tracker.record_429(subscription)
                        # Return the subscription so caller knows to retry
                        return subscription, None  # None response = should retry
                    
                    if response.status_code >= 500:
                        await self.tracker.record_error(subscription)
                    
                    return subscription, Response(
                        content=response.content,
                        status_code=response.status_code,
                        headers=self._filter_response_headers(response.headers),
                        media_type=response.headers.get("content-type", "application/json"),
                    )
                    
            except httpx.TimeoutException as e:
                logger.error(f"Timeout proxying to {subscription.name}: {e}")
                await self.tracker.record_error(subscription)
                return subscription, Response(
                    content='{"error": {"type": "timeout", "message": "Request timed out."}}',
                    status_code=504,
                    media_type="application/json",
                )
            except httpx.RequestError as e:
                logger.error(f"Error proxying to {subscription.name}: {e}")
                await self.tracker.record_error(subscription)
                return subscription, Response(
                    content='{"error": {"type": "proxy_error", "message": "Failed to connect to upstream."}}',
                    status_code=502,
                    media_type="application/json",
                )
    
    async def proxy_request(self, request: Request, path: str) -> Response:
        """
        Proxy a request to Anthropic API with automatic retry on 429.
        
        Args:
            request: The incoming FastAPI request.
            path: The API path (e.g., "/v1/messages").
            
        Returns:
            Response to send to the client.
        """
        # Build the upstream URL
        url = f"{ANTHROPIC_BASE_URL}{path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        
        # Get request body
        body = await request.body()
        
        # Check if streaming
        is_streaming = self._is_streaming_request(body)
        
        # For streaming requests, no retry (can't replay partial streams)
        if is_streaming:
            subscription = await self.tracker.select_subscription()
            if subscription is None:
                logger.error("No subscriptions available for request")
                return Response(
                    content='{"error": {"type": "overloaded", "message": "All API subscriptions are currently at capacity. Please retry."}}',
                    status_code=503,
                    media_type="application/json",
                )
            
            # Build headers
            headers = self._build_headers(request, subscription)
            
            logger.info(
                f"Routing streaming request to {subscription.name}: {request.method} {path}"
            )
            
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
                        return Response(
                            content='{"error": {"type": "rate_limit", "message": "Rate limited. Please retry."}}',
                            status_code=429,
                            media_type="application/json",
                        )
                    
                    return StreamingResponse(
                        self._stream_response(response, subscription),
                        status_code=response.status_code,
                        headers=self._filter_response_headers(response.headers),
                        media_type=response.headers.get("content-type", "text/event-stream"),
                    )
                    
                except httpx.TimeoutException as e:
                    logger.error(f"Timeout proxying to {subscription.name}: {e}")
                    await self.tracker.record_error(subscription)
                    return Response(
                        content='{"error": {"type": "timeout", "message": "Request timed out."}}',
                        status_code=504,
                        media_type="application/json",
                    )
                except httpx.RequestError as e:
                    logger.error(f"Error proxying to {subscription.name}: {e}")
                    await self.tracker.record_error(subscription)
                    return Response(
                        content='{"error": {"type": "proxy_error", "message": "Failed to connect to upstream."}}',
                        status_code=502,
                        media_type="application/json",
                    )
        
        # Non-streaming: retry on 429
        excluded: set[str] = set()
        
        for attempt in range(MAX_429_RETRIES + 1):
            subscription = await self.tracker.select_subscription()
            
            if subscription is None:
                logger.error("No subscriptions available for request")
                return Response(
                    content='{"error": {"type": "overloaded", "message": "All API subscriptions are currently at capacity. Please retry."}}',
                    status_code=503,
                    media_type="application/json",
                )
            
            # Skip already-tried subscriptions
            if subscription.name in excluded:
                # All available have been tried
                break
            
            headers = self._build_headers(request, subscription)
            
            logger.info(
                f"Routing request to {subscription.name}: {request.method} {path} "
                f"(attempt {attempt + 1}/{MAX_429_RETRIES + 1})"
            )
            
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
                        logger.warning(
                            f"429 from {subscription.name}, retrying with different subscription"
                        )
                        continue  # Try next subscription
                    
                    if response.status_code >= 500:
                        await self.tracker.record_error(subscription)
                    
                    return Response(
                        content=response.content,
                        status_code=response.status_code,
                        headers=self._filter_response_headers(response.headers),
                        media_type=response.headers.get("content-type", "application/json"),
                    )
                    
                except httpx.TimeoutException as e:
                    logger.error(f"Timeout proxying to {subscription.name}: {e}")
                    await self.tracker.record_error(subscription)
                    return Response(
                        content='{"error": {"type": "timeout", "message": "Request timed out."}}',
                        status_code=504,
                        media_type="application/json",
                    )
                except httpx.RequestError as e:
                    logger.error(f"Error proxying to {subscription.name}: {e}")
                    await self.tracker.record_error(subscription)
                    return Response(
                        content='{"error": {"type": "proxy_error", "message": "Failed to connect to upstream."}}',
                        status_code=502,
                        media_type="application/json",
                    )
        
        # All retries exhausted
        return Response(
            content='{"error": {"type": "rate_limit", "message": "All subscriptions rate limited. Please retry later."}}',
            status_code=429,
            media_type="application/json",
        )
